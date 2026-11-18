"""Verification orchestration (Phase 4).

One call produces an immutable verification attempt: built-in gates, declared
command/invariant checks with baseline and candidate runs, AST test-integrity
analysis, an advisory independent review, an evidence graph and a report with an
explicit decision.

Design constraints honoured here:

* a check that did not run is never reported as passed;
* a baseline is a fresh worktree at the pinned base commit, never the user's
  checkout;
* command checks require a consumed ``COMMAND_EXECUTION`` approval whose policy
  hash matches the current contract;
* the reviewer cannot change a deterministic result, and ungrounded findings are
  rejected;
* attempts are append-only, so a later re-run cannot rewrite earlier evidence.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ApprovalAction,
    ApprovalStatus,
    ArtifactKind,
    CheckKind,
    CheckOutcome,
    EventType,
    FindingSeverity,
    VerificationGate,
    VerificationMethod,
)
from mayaworkbench.domain.models import (
    CheckSpec,
    Task,
    TaskContract,
    TaskDiff,
    TaskPlan,
    VerificationCheck,
    VerificationFinding,
    VerificationReport,
    VerificationRun,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import (
    BudgetError,
    MayaError,
    NotFoundError,
    VerificationError,
)
from mayaworkbench.ids import new_ulid
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    ApprovalStore,
    ContractStore,
    DiffStore,
    EventStore,
    FileStore,
    InvocationStore,
    PlanStore,
    RepositoryStore,
    ReviewStore,
    RunStore,
    SnapshotStore,
    TaskStore,
    VerificationStore,
    WorkspaceStore,
)
from mayaworkbench.tasks.approvals import expected_command_sha, expected_write_sha
from mayaworkbench.tasks.budget import BudgetController
from mayaworkbench.tasks.review import ReviewService, is_test_path
from mayaworkbench.verification import execution, integrity, registry, report as report_mod
from mayaworkbench.verification.bundle import BundleResult, write_bundle
from mayaworkbench.verification.reviewer import ReviewResult, run_independent_review
from mayaworkbench.workspace.runner import CommandResult

VERIFICATION_STATES = {"REVIEWING", "COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "BLOCKED", "CANCELLED", "NEEDS_HUMAN_DECISION"}
REVIEWER_KEY = "review.independent"


@dataclass
class _Outcome:
    outcome: CheckOutcome
    summary: str
    findings: list[VerificationFinding] | None = None
    details: dict[str, Any] | None = None
    artifact_id: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    command: list[str] | None = None


class VerificationService:
    def __init__(
        self,
        *,
        home: Path,
        db: Database,
        settings: Settings,
        artifacts: ArtifactStore,
        events: EventStore,
        runs: RunStore,
        tasks: TaskStore,
        contracts: ContractStore,
        plans: PlanStore,
        diffs: DiffStore,
        reviews: ReviewStore,
        review_service: ReviewService,
        workspaces: WorkspaceStore,
        repositories: RepositoryStore,
        snapshots: SnapshotStore,
        files: FileStore,
        approvals: ApprovalStore,
        invocations: InvocationStore,
        store: VerificationStore,
        gateway: ModelGateway,
    ):
        self.home = Path(home)
        self.db = db
        self.settings = settings
        self.artifacts = artifacts
        self.events = events
        self.runs = runs
        self.tasks = tasks
        self.contracts = contracts
        self.plans = plans
        self.diffs = diffs
        self.reviews = reviews
        self.review_service = review_service
        self.workspaces = workspaces
        self.repositories = repositories
        self.snapshots = snapshots
        self.files = files
        self.approvals = approvals
        self.invocations = invocations
        self.store = store
        self.gateway = gateway

    # ------------------------------------------------------------------ public
    def latest_report(self, run_id: str) -> VerificationReport | None:
        return self.store.latest_report(run_id)

    def report_checks(self, report: VerificationReport) -> list[VerificationCheck]:
        return self.store.list_checks(report.verification_id)

    def declared_checks(self, run_id: str) -> list[CheckSpec]:
        run = self.runs.get(run_id)
        return list(self.contracts.get(run.contract_id).verification_checks)

    def proposals(self, run_id: str) -> list[CheckSpec]:
        run = self.runs.get(run_id)
        snapshot = self.snapshots.get(run.snapshot_id)
        indexed = self.files.iter_files(snapshot.id)
        has_tests = any(item.is_test for item in indexed)
        has_python = any(item.language == "python" for item in indexed)
        import importlib.util

        pytest_available = importlib.util.find_spec("pytest") is not None
        return registry.proposed_checks(
            has_tests=has_tests,
            has_python=has_python,
            pytest_available=pytest_available,
        )

    def verify(
        self,
        run_id: str,
        *,
        extra_checks: list[CheckSpec] | None = None,
        include_review: bool = True,
        actor: str = "local-user",
    ) -> tuple[VerificationReport, list[VerificationCheck]]:
        run = self.runs.get(run_id)
        if run.state.value not in VERIFICATION_STATES:
            raise VerificationError(
                f"Run is {run.state.value}; verification requires a finished implementation",
                hint="Wait for the worker to reach REVIEWING (`maya task status <run>`).",
            )
        task = self.tasks.get(run.task_id)
        contract = self.contracts.get(run.contract_id)
        plan = self.plans.get(run.plan_id) if run.plan_id else None
        diff = self.diffs.get_by_run(run.id)
        if diff is None:
            raise VerificationError(
                "Run has no captured patch to verify",
                hint=f"Run `maya task diff {run.id}` first.",
            )
        workspace = self.workspaces.get_by_run(run.id)
        criteria = report_mod.criteria_from_contract(contract)
        declared = self._build_plan(contract, extra_checks or [], criteria)
        plan_payload = {
            "run_id": run.id,
            "contract_sha256": contract.content_sha256,
            "declared": [item.model_dump(mode="json") for item in declared],
            "builtin": [item.key for item in registry.BUILTIN_CHECKS],
            "reviewer_requested": include_review,
            "criteria": [{"id": cid, "text": text} for cid, text in criteria],
            "extra_checks": len(extra_checks or []),
        }
        plan_sha = report_mod.canonical_sha256(plan_payload)
        plan_artifact = self.artifacts.put_text(
            json.dumps(plan_payload, indent=2, sort_keys=True),
            kind=ArtifactKind.VERIFICATION_PLAN,
            media_type="application/json",
            created_by="verification",
        )
        attempt = VerificationRun(
            id="ver-" + new_ulid(),
            run_id=run.id,
            task_id=run.task_id,
            project_id=run.project_id,
            attempt=self.store.next_attempt(run.id),
            plan_sha256=plan_sha,
            status="RUNNING",
            details={
                "plan_artifact_id": plan_artifact.id,
                "extra_checks": len(extra_checks or []),
                "include_review": include_review,
                "actor": actor,
            },
        )
        self.store.create_attempt(attempt)
        self._event(
            EventType.VERIFICATION_STARTED,
            run=run,
            entity_type="verification",
            entity_id=attempt.id,
            payload={
                "attempt": attempt.attempt,
                "plan_sha256": plan_sha,
                "declared_checks": [item.key for item in declared],
                "plan_artifact_id": plan_artifact.id,
            },
        )
        checks: list[VerificationCheck] = []
        baseline: execution.BaselineWorkspace | None = None
        try:
            checks.extend(self._builtin_scope(run, task, contract, plan, diff, attempt))
            patch_text = self._patch_text(diff)

            if declared:
                baseline, baseline_error = self._create_baseline(
                    run, attempt, workspace
                )
            else:
                baseline, baseline_error = None, None

            if declared:
                checks.extend(
                    self._declared_checks(
                        run=run,
                        task=task,
                        contract=contract,
                        attempt=attempt,
                        declared=declared,
                        workspace=workspace,
                        baseline=baseline,
                        baseline_error=baseline_error,
                    )
                )

            checks.append(
                self._patch_reproducible(
                    run, contract, diff, patch_text, workspace, attempt
                )
            )
            checks.append(self._test_integrity(run, diff, workspace, attempt))
            review_result = self._review(
                run=run,
                task=task,
                contract=contract,
                diff=diff,
                patch=patch_text,
                checks=checks,
                criteria=criteria,
                include_review=include_review,
            )
            if review_result is not None:
                checks.append(
                    self._review_check(run, task, attempt, review_result)
                )
            checks.append(
                self._report_integrity(
                    run, contract, diff, checks, declared, criteria, attempt
                )
            )
            criteria_assessments = report_mod.assess_criteria(criteria, checks)
            reviewer_details = review_result.details if review_result else None
            if review_result is not None:
                reviewer_details = {
                    **review_result.details,
                    "check_key": REVIEWER_KEY,
                    "findings": [
                        item.model_dump(mode="json") for item in review_result.findings
                    ],
                    "skipped_reason": review_result.details.get("skipped_reason"),
                }
            decision, blocking, limitations = report_mod.decide(
                run=run,
                checks=checks,
                criteria=criteria_assessments,
                reviewer=reviewer_details,
                declared_specs=declared,
            )
            report_id = "vreport_" + new_ulid()
            payload = report_mod.build_report_payload(
                attempt_id=attempt.id,
                report_id=report_id,
                run=run,
                task=task,
                contract=contract,
                diff=diff,
                checks=checks,
                criteria=criteria_assessments,
                decision=decision,
                summary=self._summary(decision, checks, criteria_assessments),
                blocking=blocking,
                limitations=limitations,
                reviewer=reviewer_details,
                declared_specs=declared,
            )
            report_json = json.dumps(
                payload, indent=2, sort_keys=True, ensure_ascii=False
            )
            markdown = report_mod.render_markdown(
                payload, task=task, run=run, contract=contract
            )
            report_artifact = self.artifacts.put_text(
                report_json,
                kind=ArtifactKind.VERIFICATION_REPORT,
                media_type="application/json",
                created_by="verification",
            )
            markdown_artifact = self.artifacts.put_text(
                markdown,
                kind=ArtifactKind.VERIFICATION_REPORT,
                media_type="text/markdown; charset=utf-8",
                created_by="verification",
            )
            links = report_mod.build_evidence_links(
                verification_id=attempt.id,
                run_id=run.id,
                report_id=report_id,
                checks=checks,
                criteria=criteria_assessments,
                reviewer=reviewer_details,
                patch_sha256=diff.patch_sha256,
            )
            report = VerificationReport(
                id=report_id,
                verification_id=attempt.id,
                run_id=run.id,
                task_id=run.task_id,
                project_id=run.project_id,
                decision=decision,
                summary=payload["summary"],
                criteria=criteria_assessments,
                check_ids=[item.id for item in checks],
                blocking=blocking,
                limitations=limitations,
                report_artifact_id=report_artifact.id,
                json_sha256=report_mod.canonical_sha256(payload),
                reviewer_invocation_id=(
                    review_result.invocation_id if review_result else None
                ),
                details={
                    "markdown_artifact_id": markdown_artifact.id,
                    "plan_artifact_id": plan_artifact.id,
                    "attempt": attempt.attempt,
                    "declared_checks": [item.key for item in declared],
                },
            )
            self.store.create_report(report, links)
            self.store.finish_attempt(
                attempt.id,
                status="COMPLETED",
                details={
                    "decision": decision.value,
                    "report_id": report.id,
                    "checks": len(checks),
                    "blocking": len(blocking),
                    "limitations": len(limitations),
                },
            )
            self.runs.update_progress(
                run.id,
                details={
                    **self.runs.get(run.id).details,
                    "verification_id": attempt.id,
                    "verification_report_id": report.id,
                    "verification_decision": decision.value,
                    "verification_json_sha256": report.json_sha256,
                },
            )
            self._event(
                EventType.VERIFICATION_COMPLETED,
                run=run,
                entity_type="verification",
                entity_id=attempt.id,
                payload={
                    "attempt": attempt.attempt,
                    "report_id": report.id,
                    "decision": decision.value,
                    "checks": len(checks),
                    "blocking": blocking,
                    "limitations": limitations[:20],
                    "report_artifact_id": report_artifact.id,
                    "json_sha256": report.json_sha256,
                },
            )
            return report, checks
        except BaseException as exc:
            message = f"{type(exc).__name__}: {exc}"
            try:
                self.store.finish_attempt(
                    attempt.id, status="FAILED", details={"error": message[:2000]}
                )
                self._event(
                    EventType.VERIFICATION_FAILED,
                    run=run,
                    entity_type="verification",
                    entity_id=attempt.id,
                    payload={"error": message[:2000]},
                )
            except Exception:  # pragma: no cover - never mask the original failure
                pass
            if isinstance(exc, MayaError):
                raise
            raise VerificationError(
                f"Verification failed for run {run_id}: {message}",
                hint="Recorded checks remain inspectable; re-run to create a new attempt.",
            ) from exc
        finally:
            if baseline is not None:
                baseline.remove()

    # ------------------------------------------------------------------- plan
    def _build_plan(
        self, contract: TaskContract, extra: list[CheckSpec], criteria: list[tuple[str, str]]
    ) -> list[CheckSpec]:
        declared = list(contract.verification_checks) + list(extra)
        keys: set[str] = set()
        valid_criteria = {item[0] for item in criteria}
        for spec in declared:
            if spec.key in keys:
                raise VerificationError(
                    f"Duplicate verification check key: {spec.key}",
                    hint="Each check needs a unique key within an attempt.",
                )
            keys.add(spec.key)
            unknown = sorted(set(spec.criteria) - valid_criteria)
            if unknown:
                raise VerificationError(
                    f"Check {spec.key} references unknown criteria: {', '.join(unknown)}",
                    hint=f"Available criteria: {', '.join(sorted(valid_criteria))}.",
                )
            if spec.key in registry.BUILTIN_KEYS:
                raise VerificationError(
                    f"Check key {spec.key} is reserved for a built-in check"
                )
        return declared

    # --------------------------------------------------------------- built-ins
    def _builtin_scope(
        self,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        plan: TaskPlan | None,
        diff: TaskDiff,
        attempt: VerificationRun,
    ) -> list[VerificationCheck]:
        out: list[VerificationCheck] = []

        # Gate A — scope
        review = self.reviews.get_by_run(run.id)
        if review is None:
            review = self.review_service.review(
                run=run, task=task, contract=contract, diff=diff
            )
        blocking_codes = {
            "SCOPE_VIOLATION",
            "EXCLUDED_PATH",
            "SECRET_PATH",
            "ORIGINAL_CHECKOUT_CHANGED",
        }
        errors = [
            item
            for item in review.findings
            if item.severity == "ERROR" or item.code in blocking_codes
        ]
        findings = [
            VerificationFinding(
                severity=FindingSeverity.ERROR,
                code=self._safe_deterministic_code(item.code),
                message=item.message,
                path=item.path,
                line=item.line,
                source="deterministic",
            )
            for item in errors
        ]
        out.append(
            self._record(
                run=run,
                task=task,
                attempt=attempt,
                key="scope.integrity",
                gate=VerificationGate.SCOPE,
                kind=CheckKind.DETERMINISTIC,
                title="Changes stay inside the approved scope",
                method=VerificationMethod.SOURCE_INSPECTION,
                outcome=CheckOutcome.FAILED if errors else CheckOutcome.PASSED,
                summary=(
                    "out-of-scope or unsafe changes were found"
                    if errors
                    else "diff is inside the approved scope; the original checkout is unchanged"
                ),
                findings=findings,
                details={
                    "scope_violations": diff.scope_violations,
                    "original_checkout_unchanged": diff.original_checkout_unchanged,
                    "review_errors": len(errors),
                    "changed_files": len(diff.files),
                },
            )
        )

        # Gate A — approvals
        consumed = self.approvals.list(run_id=run.id, status=ApprovalStatus.CONSUMED)
        expected: dict[str, str] = {}
        if ApprovalAction.WRITE_ACCESS in contract.required_approvals:
            expected[ApprovalAction.WRITE_ACCESS.value] = expected_write_sha(
                run=run, contract=contract, plan=plan
            )
        if ApprovalAction.COMMAND_EXECUTION in contract.required_approvals:
            expected[ApprovalAction.COMMAND_EXECUTION.value] = expected_command_sha(
                run=run, contract=contract
            )
        approval_findings: list[VerificationFinding] = []
        approval_details: dict[str, Any] = {}
        for action, sha in expected.items():
            match = next(
                (
                    item
                    for item in consumed
                    if item.action.value == action and item.spec_sha256 == sha
                ),
                None,
            )
            approval_details[action] = {
                "expected_sha256": sha,
                "approval_id": match.id if match else None,
                "matched": match is not None,
            }
            if match is None:
                approval_findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="APPROVAL_MISSING",
                        message=(
                            f"No consumed {action} approval matches the current contract "
                            "and plan specification."
                        ),
                    )
                )
        out.append(
            self._record(
                run=run,
                task=task,
                attempt=attempt,
                key="approvals.integrity",
                gate=VerificationGate.SCOPE,
                kind=CheckKind.DETERMINISTIC,
                title="Required approvals were consumed for this exact contract",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=(
                    CheckOutcome.NOT_APPLICABLE
                    if not expected
                    else (CheckOutcome.FAILED if approval_findings else CheckOutcome.PASSED)
                ),
                summary=(
                    "no approvals were required"
                    if not expected
                    else (
                        "one or more required approvals are missing or mismatched"
                        if approval_findings
                        else "all required approvals match the current specification"
                    )
                ),
                findings=approval_findings,
                details=approval_details,
            )
        )

        # Gate A — budgets
        constraints = contract.constraints
        budget_findings: list[VerificationFinding] = []
        elapsed = 0
        if run.started_at is not None:
            finished = run.finished_at or utcnow()
            elapsed = max(0, int((finished - run.started_at).total_seconds()))
        limits = [
            ("steps", run.step_count, constraints.max_steps),
            ("tool_calls", run.tool_call_count, constraints.max_tool_calls),
            ("model_tokens", run.model_tokens, constraints.max_model_tokens),
            ("runtime_seconds", elapsed, constraints.max_runtime_seconds),
        ]
        for name, used, limit in limits:
            if used > limit:
                budget_findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="BUDGET_EXCEEDED",
                        message=f"{name} used {used} against a limit of {limit}.",
                        details={"limit": name, "used": used, "allowed": limit},
                    )
                )
        if run.model_cost_usd > constraints.max_model_cost_usd:
            budget_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="BUDGET_EXCEEDED",
                    message=(
                        f"model_cost_usd is {run.model_cost_usd:.6f} against a limit of "
                        f"{constraints.max_model_cost_usd:.6f}."
                    ),
                    details={
                        "limit": "model_cost_usd",
                        "used": run.model_cost_usd,
                        "allowed": constraints.max_model_cost_usd,
                    },
                )
            )
        out.append(
            self._record(
                run=run,
                task=task,
                attempt=attempt,
                key="budget.integrity",
                gate=VerificationGate.SCOPE,
                kind=CheckKind.DETERMINISTIC,
                title="Run budgets were not exceeded",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=CheckOutcome.FAILED if budget_findings else CheckOutcome.PASSED,
                summary=(
                    "a contract budget was exceeded"
                    if budget_findings
                    else "recorded usage is within the contract budgets"
                ),
                findings=budget_findings,
                details={
                    "steps": [run.step_count, constraints.max_steps],
                    "tool_calls": [run.tool_call_count, constraints.max_tool_calls],
                    "model_tokens": [run.model_tokens, constraints.max_model_tokens],
                    "model_cost_usd": [run.model_cost_usd, constraints.max_model_cost_usd],
                    "runtime_seconds": [elapsed, constraints.max_runtime_seconds],
                },
            )
        )

        # Gate A — snapshot identity
        snapshot = self.snapshots.get(run.snapshot_id)
        identity_findings: list[VerificationFinding] = []
        if contract.snapshot_id != run.snapshot_id:
            identity_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="SNAPSHOT_MISMATCH",
                    message="The contract and run name different snapshots.",
                )
            )
        if contract.scope.base_commit_sha != run.base_commit_sha:
            identity_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="BASE_COMMIT_MISMATCH",
                    message="The contract base commit and run base commit differ.",
                )
            )
        if diff.base_commit_sha != run.base_commit_sha:
            identity_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="BASE_COMMIT_MISMATCH",
                    message="The captured diff was produced against a different base commit.",
                )
            )
        if snapshot.indexed_at is None:
            identity_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="SNAPSHOT_NOT_INDEXED",
                    message="The pinned snapshot has no completed index.",
                )
            )
        out.append(
            self._record(
                run=run,
                task=task,
                attempt=attempt,
                key="snapshot.integrity",
                gate=VerificationGate.SCOPE,
                kind=CheckKind.DETERMINISTIC,
                title="Evidence names the pinned snapshot and base commit",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=(
                    CheckOutcome.FAILED if identity_findings else CheckOutcome.PASSED
                ),
                summary=(
                    "snapshot/base identity is inconsistent"
                    if identity_findings
                    else f"snapshot {snapshot.identity_label} at commit {run.base_commit_sha[:12]}"
                ),
                findings=identity_findings,
                details={
                    "snapshot_id": snapshot.id,
                    "snapshot_identity": snapshot.identity_label,
                    "base_commit_sha": run.base_commit_sha,
                    "indexed_at": to_iso(snapshot.indexed_at) if snapshot.indexed_at else None,
                },
            )
        )

        # Gate B — diff integrity
        diff_findings: list[VerificationFinding] = []
        patch_artifact = self.artifacts.get(diff.patch_artifact_id)
        if patch_artifact.sha256 != diff.patch_sha256:
            diff_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="PATCH_HASH_MISMATCH",
                    message="The stored patch artifact hash does not match the recorded diff hash.",
                )
            )
        if not diff.files:
            diff_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR
                    if task.task_type.value == "ENGINEERING_CHANGE"
                    else FindingSeverity.WARNING,
                    code="EMPTY_PATCH",
                    message="The captured patch changes no files.",
                )
            )
        if diff.scope_violations:
            for path in diff.scope_violations:
                diff_findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="SCOPE_VIOLATION",
                        message=f"Changed path is outside the approved scope: {path}",
                        path=path[:1000],
                    )
                )
        for item in review.findings:
            if item.severity != "ERROR":
                continue
            if item.code in blocking_codes:
                continue  # already recorded by scope.integrity
            diff_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code=self._safe_deterministic_code(item.code),
                    message=item.message,
                    path=item.path,
                )
            )
        for file in diff.files:
            if file.status == "deleted" and is_test_path(file.path):
                diff_findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="TEST_FILE_DELETED",
                        message="A test file was deleted by the candidate patch.",
                        path=file.path,
                    )
                )
            if file.binary:
                diff_findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.WARNING,
                        code="BINARY_CHANGE",
                        message="A binary file changed and cannot be reviewed as text.",
                        path=file.path,
                    )
                )
        for path in review.dependency_changes:
            diff_findings.append(
                VerificationFinding(
                    severity=FindingSeverity.WARNING,
                    code="DEPENDENCY_CHANGE",
                    message="A dependency or packaging file changed; justify it explicitly.",
                    path=path,
                )
            )
        diff_errors = [
            item for item in diff_findings if item.severity == FindingSeverity.ERROR
        ]
        out.append(
            self._record(
                run=run,
                task=task,
                attempt=attempt,
                key="diff.integrity",
                gate=VerificationGate.CHANGE,
                kind=CheckKind.DETERMINISTIC,
                title="The candidate patch is reviewable and contains no blocking findings",
                method=VerificationMethod.SOURCE_INSPECTION,
                outcome=CheckOutcome.FAILED if diff_errors else CheckOutcome.PASSED,
                summary=(
                    f"{len(diff_errors)} blocking diff finding(s)"
                    if diff_errors
                    else f"{len(diff.files)} file(s), +{diff.additions}/-{diff.deletions}"
                ),
                findings=diff_findings,
                details={
                    "patch_sha256": diff.patch_sha256,
                    "files": [item.path for item in diff.files],
                    "tests_modified": review.tests_modified,
                    "dependency_changes": review.dependency_changes,
                    "warnings": diff.warnings,
                },
            )
        )
        return out

    # -------------------------------------------------------------- declarations
    def _create_baseline(
        self,
        run: WorkflowRun,
        attempt: VerificationRun,
        workspace: Any,
    ) -> tuple[execution.BaselineWorkspace | None, str | None]:
        repository = self.repositories.get(run.repository_id)
        snapshot = self.snapshots.get(run.snapshot_id)
        patch_text: str | None = None
        try:
            if snapshot.includes_worktree_patch and snapshot.patch_artifact_id:
                patch_text = self.artifacts.read_text(snapshot.patch_artifact_id)
        except MayaError as exc:
            return None, f"the snapshot patch could not be read: {exc}"
        destination = (
            self.home
            / "verification"
            / run.id
            / f"attempt-{attempt.attempt}"
            / "baseline"
        )
        try:
            baseline = execution.BaselineWorkspace.create(
                repository_root=Path(repository.root_path),
                base_commit=run.base_commit_sha,
                patch_text=patch_text,
                destination=destination,
            )
        except (MayaError, subprocess.SubprocessError, OSError) as exc:
            return None, str(exc)
        return baseline, None

    def _declared_checks(
        self,
        *,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        attempt: VerificationRun,
        declared: list[CheckSpec],
        workspace: Any,
        baseline: execution.BaselineWorkspace | None,
        baseline_error: str | None,
    ) -> list[VerificationCheck]:
        out: list[VerificationCheck] = []
        workspace_path = Path(workspace.path) if workspace is not None else None
        workspace_removed = workspace is None or workspace.status.value == "REMOVED"
        command_approval = self._command_approval(run, contract)
        for spec in declared:
            if workspace_removed or workspace_path is None:
                out.append(
                    self._check_from_spec(
                        run, task, contract, attempt, spec,
                        _Outcome(
                            outcome=CheckOutcome.BLOCKED,
                            summary=(
                                "the execution workspace is missing or removed, so checks "
                                "cannot run against the candidate tree"
                            ),
                            details={"workspace_available": False},
                        ),
                    )
                )
                continue
            if spec.kind == CheckKind.COMMAND and command_approval is None:
                out.append(
                    self._check_from_spec(
                        run, task, contract, attempt, spec,
                        _Outcome(
                            outcome=CheckOutcome.BLOCKED,
                            summary=(
                                "command execution was not authorized by a consumed "
                                "COMMAND_EXECUTION approval for this contract"
                            ),
                            command=list(spec.argv or []),
                            details={"authorization": "missing"},
                        ),
                    )
                )
                continue
            script: str | None = None
            argv: list[str] | None = list(spec.argv or [])
            if spec.kind == CheckKind.ML_INVARIANT:
                try:
                    assert spec.invariant is not None
                    script = registry.render_invariant(spec.invariant)
                    argv = ["python", "<generated invariant script>"]
                except (MayaError, ValueError) as exc:
                    out.append(
                        self._check_from_spec(
                            run, task, contract, attempt, spec,
                            _Outcome(
                                outcome=CheckOutcome.BLOCKED,
                                summary=f"the invariant script could not be generated: {exc}",
                            ),
                        )
                    )
                    continue
            run_home = (
                self.home / "verification" / run.id / f"attempt-{attempt.attempt}" / "home"
            )
            baseline_result: CommandResult | None = None
            if baseline is not None:
                try:
                    baseline_result = execution.execute_check(
                        owner="baseline",
                        workspace=baseline.path,
                        constraints=contract.constraints,
                        run_home=run_home / "baseline",
                        argv=list(spec.argv or []) if script is None else None,
                        script=script,
                        timeout_seconds=spec.timeout_seconds,
                    )
                except (MayaError, OSError) as exc:
                    baseline_result = None
                    baseline_error = baseline_error or str(exc)
            try:
                candidate_result = execution.execute_check(
                    owner="candidate",
                    workspace=workspace_path,
                    constraints=contract.constraints,
                    run_home=run_home / "candidate",
                    argv=list(spec.argv or []) if script is None else None,
                    script=script,
                    timeout_seconds=spec.timeout_seconds,
                )
            except (MayaError, OSError) as exc:
                out.append(
                    self._check_from_spec(
                        run, task, contract, attempt, spec,
                        _Outcome(
                            outcome=CheckOutcome.BLOCKED,
                            summary=f"the check could not be started: {exc}",
                            command=argv,
                        ),
                    )
                )
                continue
            invariant_script = script is not None
            outcome = (
                execution.invariant_outcome(candidate_result)
                if invariant_script
                else execution.command_outcome(candidate_result)
            )
            details: dict[str, Any] = {
                "network_enforcement": candidate_result.network_enforcement,
                "limits_enforcement": candidate_result.limits_enforcement,
                "output_truncated": candidate_result.output_truncated,
                "cleanup_succeeded": candidate_result.cleanup_succeeded,
                "baseline_available": baseline_result is not None,
                "baseline_error": baseline_error,
            }
            findings: list[VerificationFinding] = []
            summary = (
                f"ML invariant {spec.invariant.name if spec.invariant else spec.key}: "
                f"{execution.invariant_reason(candidate_result)}"
                if invariant_script
                else execution.outcome_reason(candidate_result)
            )
            if invariant_script:
                marker = execution.invariant_marker(candidate_result)
                details["invariant_status"] = marker.get("status") if marker else None
                details["invariant_reason"] = (
                    marker.get("reason") if marker else "the script printed no verdict marker"
                )
            if candidate_result.blocked:
                details["blocked_reason"] = candidate_result.blocked_reason
            if baseline_result is not None:
                base_outcome = (
                    execution.invariant_outcome(baseline_result)
                    if invariant_script
                    else execution.command_outcome(baseline_result)
                )
                details["baseline_outcome"] = base_outcome.value
                details["baseline_exit_code"] = baseline_result.exit_code
                details["baseline_duration_ms"] = baseline_result.duration_ms
                if outcome == CheckOutcome.PASSED and base_outcome == CheckOutcome.FAILED:
                    summary += "; the same check failed on the base revision"
                    details["improved"] = True
                elif outcome == CheckOutcome.FAILED and base_outcome == CheckOutcome.PASSED:
                    summary += "; regression: the check passed on the base revision"
                    details["regression"] = True
                    findings.append(
                        VerificationFinding(
                            severity=FindingSeverity.ERROR,
                            code="REGRESSION",
                            message="A check that passed on the base revision now fails.",
                        )
                    )
                elif outcome == CheckOutcome.FAILED and base_outcome == CheckOutcome.FAILED:
                    summary += "; this failure is pre-existing on the base revision"
                    details["preexisting_failure"] = True
                    findings.append(
                        VerificationFinding(
                            severity=FindingSeverity.WARNING,
                            code="PREEXISTING_FAILURE",
                            message=(
                                "The check failed identically before the change; the patch "
                                "is not implicated, but the check provides no verification."
                            ),
                        )
                    )
                if baseline_result.output_truncated:
                    details["baseline_output_truncated"] = True
            else:
                details["baseline_outcome"] = "UNAVAILABLE"
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.WARNING,
                        code="BASELINE_UNAVAILABLE",
                        message=(
                            "The base revision could not be executed, so this result "
                            "cannot be compared with the pre-change behavior"
                            + (f": {baseline_error}" if baseline_error else ".")
                        ),
                    )
                )
            artifact = self._store_command_artifact(
                spec.key, "candidate", candidate_result, summary
            )
            baseline_artifact = None
            if baseline_result is not None:
                baseline_artifact = self._store_command_artifact(
                    spec.key, "baseline", baseline_result, execution.outcome_reason(baseline_result)
                )
            details["baseline_artifact_id"] = (
                baseline_artifact.id if baseline_artifact else None
            )
            out.append(
                self._check_from_spec(
                    run, task, contract, attempt, spec,
                    _Outcome(
                        outcome=outcome,
                        summary=f"{spec.title}: {summary}",
                        findings=findings,
                        details=details,
                        artifact_id=artifact.id,
                        exit_code=candidate_result.exit_code,
                        duration_ms=candidate_result.duration_ms,
                        command=argv,
                    ),
                )
            )
        return out

    def _command_approval(self, run: WorkflowRun, contract: TaskContract):
        if ApprovalAction.COMMAND_EXECUTION not in contract.required_approvals:
            return None
        expected = expected_command_sha(run=run, contract=contract)
        consumed = self.approvals.list(run_id=run.id, status=ApprovalStatus.CONSUMED)
        return next(
            (
                item
                for item in consumed
                if item.action == ApprovalAction.COMMAND_EXECUTION
                and item.spec_sha256 == expected
            ),
            None,
        )

    # ----------------------------------------------------------- patch integrity
    def _patch_reproducible(
        self,
        run: WorkflowRun,
        contract: TaskContract,
        diff: TaskDiff,
        patch_text: str,
        workspace: Any,
        attempt: VerificationRun,
    ) -> VerificationCheck:
        if not diff.files:
            return self._record(
                run=run,
                task=self.tasks.get(run.task_id),
                attempt=attempt,
                key="patch.reproducible",
                gate=VerificationGate.CHANGE,
                kind=CheckKind.DETERMINISTIC,
                title="The captured patch reproduces the candidate workspace",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=CheckOutcome.NOT_APPLICABLE,
                summary="the patch is empty, so there is nothing to reproduce",
                details={"patch_sha256": diff.patch_sha256},
            )
        if workspace is None or workspace.status.value == "REMOVED":
            return self._record(
                run=run,
                task=self.tasks.get(run.task_id),
                attempt=attempt,
                key="patch.reproducible",
                gate=VerificationGate.CHANGE,
                kind=CheckKind.DETERMINISTIC,
                title="The captured patch reproduces the candidate workspace",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=CheckOutcome.BLOCKED,
                summary="the candidate workspace is missing or removed",
                details={"patch_sha256": diff.patch_sha256},
            )
        repository = self.repositories.get(run.repository_id)
        destination = (
            self.home
            / "verification"
            / run.id
            / f"attempt-{attempt.attempt}"
            / f"reproduce-{new_ulid()}"
        )
        try:
            result = execution.verify_patch_reproduces(
                repository_root=Path(repository.root_path),
                base_commit=run.base_commit_sha,
                patch_text=patch_text,
                candidate_workspace=Path(workspace.path),
                paths=[item.path for item in diff.files],
                destination=destination,
            )
        except (MayaError, OSError, subprocess.SubprocessError) as exc:
            return self._record(
                run=run,
                task=self.tasks.get(run.task_id),
                attempt=attempt,
                key="patch.reproducible",
                gate=VerificationGate.CHANGE,
                kind=CheckKind.DETERMINISTIC,
                title="The captured patch reproduces the candidate workspace",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=CheckOutcome.BLOCKED,
                summary=f"reproduction could not be performed: {exc}",
                details={"patch_sha256": diff.patch_sha256},
            )
        findings: list[VerificationFinding] = []
        for item in result.get("mismatches", []):
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="PATCH_DRIFT",
                    message=(
                        "The workspace file differs from the file produced by applying the "
                        "captured patch to the base commit."
                    ),
                    path=item["path"],
                    details=item,
                )
            )
        for path in result.get("missing", []):
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="PATCH_DRIFT",
                    message="The patch does not create a file that exists in the workspace.",
                    path=path,
                )
            )
        if result.get("error"):
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="PATCH_NOT_APPLICABLE",
                    message=str(result["error"]),
                )
            )
        ok = bool(result.get("ok"))
        return self._record(
            run=run,
            task=self.tasks.get(run.task_id),
            attempt=attempt,
            key="patch.reproducible",
            gate=VerificationGate.CHANGE,
            kind=CheckKind.DETERMINISTIC,
            title="The captured patch reproduces the candidate workspace",
            method=VerificationMethod.STATIC_ANALYSIS,
            outcome=CheckOutcome.PASSED if ok else CheckOutcome.FAILED,
            summary=(
                f"the patch reproduces {result.get('files_compared', 0)} changed file(s)"
                if ok
                else "the patch does not reproduce the candidate workspace"
            ),
            findings=findings,
            details={
                "patch_sha256": diff.patch_sha256,
                "files_compared": result.get("files_compared", 0),
                "mismatches": result.get("mismatches", []),
                "missing": result.get("missing", []),
            },
        )

    # ---------------------------------------------------------- test integrity
    def _test_integrity(
        self,
        run: WorkflowRun,
        diff: TaskDiff,
        workspace: Any,
        attempt: VerificationRun,
    ) -> VerificationCheck:
        task = self.tasks.get(run.task_id)
        paths = sorted(
            {
                item.path
                for item in diff.files
                if is_test_path(item.path)
                or item.path.rsplit("/", 1)[-1] in integrity.CONFIG_PATHS
            }
        )
        if not paths:
            return self._record(
                run=run,
                task=task,
                attempt=attempt,
                key="test.integrity",
                gate=VerificationGate.REVIEW,
                kind=CheckKind.TEST_INTEGRITY,
                title="Tests were not silently weakened",
                method=VerificationMethod.STATIC_ANALYSIS,
                outcome=CheckOutcome.NOT_APPLICABLE,
                summary="the patch changes no test files or test configuration",
                details={"checked_paths": []},
            )
        repository = self.repositories.get(run.repository_id)
        baseline: dict[str, str] = {}
        candidate: dict[str, str] = {}
        unavailable: list[str] = []
        for path in paths:
            old = self._git_show(Path(repository.root_path), run.base_commit_sha, path)
            if old is not None:
                baseline[path] = old
            if workspace is not None and workspace.status.value != "REMOVED":
                try:
                    candidate[path] = (Path(workspace.path) / path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    pass
            else:
                new = self._git_show(Path(repository.root_path), run.base_commit_sha, path)
                if path not in baseline and new is not None:
                    candidate[path] = new
            if path not in baseline and path not in candidate:
                unavailable.append(path)
        findings = integrity.analyze_test_integrity(baseline, candidate)
        outcome = CheckOutcome.FAILED if any(
            item.severity == FindingSeverity.ERROR for item in findings
        ) else CheckOutcome.PASSED
        payload = {
            "paths": paths,
            "baseline_paths": sorted(baseline),
            "candidate_paths": sorted(candidate),
            "unavailable": unavailable,
            "findings": [item.model_dump(mode="json") for item in findings],
            "note": (
                "AST comparison detects structural weakening only. It cannot prove "
                "semantic equivalence or that no test was gamed."
            ),
        }
        artifact = self.artifacts.put_text(
            json.dumps(payload, indent=2, sort_keys=True),
            kind=ArtifactKind.TEST_INTEGRITY_REPORT,
            media_type="application/json",
            created_by="verification",
        )
        return self._record(
            run=run,
            task=task,
            attempt=attempt,
            key="test.integrity",
            gate=VerificationGate.REVIEW,
            kind=CheckKind.TEST_INTEGRITY,
            title="Tests were not silently weakened",
            method=VerificationMethod.STATIC_ANALYSIS,
            outcome=outcome,
            summary=(
                f"{len(findings)} test-integrity finding(s) across {len(paths)} file(s)"
                if findings
                else f"no detectable weakening in {len(paths)} changed test/config file(s)"
            ),
            findings=findings,
            artifact_id=artifact.id,
            details={
                "checked_paths": paths,
                "unavailable_paths": unavailable,
                "error_findings": sum(
                    1 for item in findings if item.severity == FindingSeverity.ERROR
                ),
            },
        )

    # -------------------------------------------------------------- reviewer
    def _review(
        self,
        *,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        diff: TaskDiff,
        patch: str,
        checks: list[VerificationCheck],
        criteria: list[tuple[str, str]],
        include_review: bool,
    ) -> ReviewResult | None:
        if not include_review:
            return ReviewResult(
                outcome=CheckOutcome.SKIPPED,
                summary="independent review was disabled for this attempt",
                details={"skipped_reason": "disabled by the caller"},
            )
        integrity_findings = [
            finding
            for check in checks
            if check.kind == CheckKind.TEST_INTEGRITY
            for finding in check.findings
        ]
        estimate_chars = len(patch[:60_000]) + 8_000
        controller = BudgetController(
            constraints=contract.constraints, run_id=run.id, runs=self.runs
        )
        prices_known = (
            self.settings.model.input_cost_per_mtok is not None
            and self.settings.model.output_cost_per_mtok is not None
        )
        try:
            controller.reserve(
                estimated_input_tokens=estimate_chars // 4,
                desired_output_tokens=2000,
                cost_known=prices_known,
                input_price_per_mtok=self.settings.model.input_cost_per_mtok,
                output_price_per_mtok=self.settings.model.output_cost_per_mtok,
            )
        except BudgetError as exc:
            return ReviewResult(
                outcome=CheckOutcome.SKIPPED,
                summary=f"independent review was not attempted: {exc}",
                details={"skipped_reason": str(exc)},
            )
        result = run_independent_review(
            gateway=self.gateway,
            invocations=self.invocations,
            run=run,
            task=task,
            contract=contract,
            spec=CheckSpec(
                key=REVIEWER_KEY,
                gate=VerificationGate.REVIEW,
                kind=CheckKind.COMMAND,
                title="Independent model review",
                argv=["python", "-c", "pass"],
                required=False,
            ),
            diff=diff,
            patch=patch,
            checks=checks,
            criteria=criteria,
            integrity_findings=integrity_findings,
            workspace_path=None,
        )
        if result.invocation_id:
            try:
                invocation = self.invocations.get(result.invocation_id)
                self.runs.set_flags(
                    run.id,
                    add_tokens=invocation.total_tokens or 0,
                    add_cost_usd=invocation.cost_usd or 0.0,
                )
            except MayaError:  # pragma: no cover - accounting is best effort
                pass
        return result

    def _review_check(
        self,
        run: WorkflowRun,
        task: Task,
        attempt: VerificationRun,
        result: ReviewResult,
    ) -> VerificationCheck:
        return self._record(
            run=run,
            task=task,
            attempt=attempt,
            key=REVIEWER_KEY,
            gate=VerificationGate.REVIEW,
            kind=CheckKind.MODEL_REVIEW,
            title="Independent model review",
            method=VerificationMethod.INDEPENDENT_MODEL_REVIEW,
            outcome=result.outcome,
            required=False,
            summary=result.summary,
            findings=list(result.findings),
            details=dict(result.details),
        )

    # ---------------------------------------------------------- report integrity
    def _report_integrity(
        self,
        run: WorkflowRun,
        contract: TaskContract,
        diff: TaskDiff,
        checks: list[VerificationCheck],
        declared: list[CheckSpec],
        criteria: list[tuple[str, str]],
        attempt: VerificationRun,
    ) -> VerificationCheck:
        task = self.tasks.get(run.task_id)
        findings: list[VerificationFinding] = []
        keys = [check.key for check in checks]
        if len(keys) != len(set(keys)):
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="DUPLICATE_CHECK_KEY",
                    message="Two checks share a key, so the evidence graph is ambiguous.",
                )
            )
        recorded = set(keys)
        for spec in declared:
            if spec.key not in recorded:
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="CHECK_NOT_RUN",
                        message=f"Declared check {spec.key} has no recorded result.",
                    )
                )
        valid_criteria = {item[0] for item in criteria}
        for check in checks:
            for criterion in check.criteria:
                if criterion not in valid_criteria:
                    findings.append(
                        VerificationFinding(
                            severity=FindingSeverity.ERROR,
                            code="CRITERION_UNKNOWN",
                            message=f"Check {check.key} references {criterion}, which does not exist.",
                        )
                    )
            # A functional pass must point at real output. Without an artifact
            # the report asserts a result that cannot be inspected later.
            if (
                check.kind in report_mod.FUNCTIONAL_KINDS
                and check.outcome == CheckOutcome.PASSED
                and not check.artifact_id
            ):
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="CHECK_WITHOUT_EVIDENCE",
                        message=(
                            f"Check {check.key} passed but records no command output "
                            "artifact."
                        ),
                    )
                )
        try:
            patch_text = self.artifacts.read_text(diff.patch_artifact_id)
            digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
            if digest != diff.patch_sha256:
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="PATCH_HASH_MISMATCH",
                        message="Re-hashing the patch artifact does not reproduce the recorded hash.",
                    )
                )
        except MayaError as exc:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="PATCH_UNREADABLE",
                    message=f"The patch artifact cannot be read: {exc}",
                )
            )
        if contract.snapshot_id != run.snapshot_id:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="SNAPSHOT_MISMATCH",
                    message="The contract and run disagree about the snapshot.",
                )
            )
        has_pass = any(
            check.outcome == CheckOutcome.PASSED
            and check.required
            and check.kind in report_mod.FUNCTIONAL_KINDS
            for check in checks
        )
        if not has_pass:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.WARNING,
                    code="NO_FUNCTIONAL_PASS",
                    message="No required functional check produced a passing result.",
                )
            )
        errors = [item for item in findings if item.severity == FindingSeverity.ERROR]
        return self._record(
            run=run,
            task=task,
            attempt=attempt,
            key="report.integrity",
            gate=VerificationGate.REPORT,
            kind=CheckKind.DETERMINISTIC,
            title="Every reported success links to recorded evidence",
            method=VerificationMethod.STATIC_ANALYSIS,
            outcome=CheckOutcome.FAILED if errors else CheckOutcome.PASSED,
            summary=(
                f"{len(errors)} report-integrity error(s)"
                if errors
                else "checks, criteria and patch hashes are internally consistent"
            ),
            findings=findings,
            details={
                "checks": len(checks),
                "declared": len(declared),
                "criteria": len(criteria),
                "patch_sha256": diff.patch_sha256,
            },
        )

    # ------------------------------------------------------------------ report
    def _summary(
        self,
        decision: Any,
        checks: list[VerificationCheck],
        criteria: list[Any],
    ) -> str:
        passed = sum(1 for check in checks if check.outcome == CheckOutcome.PASSED)
        failed = sum(1 for check in checks if check.outcome == CheckOutcome.FAILED)
        no_evidence = sum(
            1
            for check in checks
            if check.outcome
            in {
                CheckOutcome.SKIPPED,
                CheckOutcome.BLOCKED,
                CheckOutcome.INCONCLUSIVE,
                CheckOutcome.UNVERIFIED,
            }
        )
        verified = sum(1 for item in criteria if item.status.value == "VERIFIED")
        return (
            f"Decision {decision.value}: {passed} check(s) passed, {failed} failed, "
            f"{no_evidence} produced no evidence; {verified}/{len(criteria)} acceptance "
            "criteria are backed by a passing check. A passing check describes only "
            "what it actually exercised."
        )

    # ----------------------------------------------------------------- helpers
    def _record(
        self,
        *,
        run: WorkflowRun,
        task: Task,
        attempt: VerificationRun | None,
        key: str,
        gate: VerificationGate,
        kind: CheckKind,
        title: str,
        method: VerificationMethod,
        outcome: CheckOutcome,
        summary: str,
        required: bool = True,
        criteria: list[str] | None = None,
        command: list[str] | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        artifact_id: str | None = None,
        findings: list[VerificationFinding] | None = None,
        details: dict[str, Any] | None = None,
    ) -> VerificationCheck:
        return self._record_inner(
            run=run,
            task=task,
            attempt=attempt,
            key=key,
            gate=gate,
            kind=kind,
            title=title,
            method=method,
            outcome=outcome,
            summary=summary,
            required=required,
            criteria=criteria or [],
            command=command or [],
            exit_code=exit_code,
            duration_ms=duration_ms,
            artifact_id=artifact_id,
            findings=findings or [],
            details=details or {},
        )

    def _record_inner(self, **kwargs: Any) -> VerificationCheck:
        run = kwargs["run"]
        attempt = kwargs["attempt"]
        if attempt is None:
            raise VerificationError(
                f"Internal error: check {kwargs['key']} has no verification attempt"
            )
        findings = kwargs["findings"]
        check = VerificationCheck(
            id="vchk_" + new_ulid(),
            verification_id=attempt.id,
            run_id=run.id,
            task_id=run.task_id,
            project_id=run.project_id,
            key=kwargs["key"],
            gate=kwargs["gate"],
            kind=kwargs["kind"],
            title=kwargs["title"],
            method=kwargs["method"],
            outcome=kwargs["outcome"],
            required=kwargs["required"],
            criteria=kwargs["criteria"],
            command=kwargs["command"],
            exit_code=kwargs["exit_code"],
            duration_ms=kwargs["duration_ms"],
            artifact_id=kwargs["artifact_id"],
            summary=kwargs["summary"],
            details=kwargs["details"],
            findings=findings,
            completed_at=utcnow(),
        )
        self.store.add_check(check)
        self._event(
            EventType.VERIFICATION_CHECK_COMPLETED,
            run=run,
            entity_type="verification_check",
            entity_id=check.id,
            payload={
                "key": check.key,
                "gate": check.gate.value,
                "outcome": check.outcome.value,
                "required": check.required,
                "findings": len(check.findings),
            },
        )
        return check

    def verify_attempt(self, verification_id: str) -> tuple[VerificationRun, list[VerificationCheck]]:
        attempt = self.store.get_attempt(verification_id)
        return attempt, self.store.list_checks(verification_id)

    def _check_from_spec(
        self,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        attempt: VerificationRun,
        spec: CheckSpec,
        outcome: _Outcome,
    ) -> VerificationCheck:
        method = (
            VerificationMethod.DETERMINISTIC_TEST
            if spec.kind == CheckKind.COMMAND
            else VerificationMethod.NUMERICAL_INVARIANT
        )
        return self._record(
            run=run,
            task=task,
            attempt=attempt,
            key=spec.key,
            gate=spec.gate,
            kind=spec.kind,
            title=spec.title,
            method=method,
            outcome=outcome.outcome,
            required=spec.required,
            criteria=list(spec.criteria),
            command=list(outcome.command or spec.argv or []),
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            artifact_id=outcome.artifact_id,
            summary=outcome.summary,
            findings=outcome.findings or [],
            details={
                **(outcome.details or {}),
                "spec": spec.model_dump(mode="json"),
            },
        )

    def _store_command_artifact(
        self, key: str, tree: str, result: CommandResult, summary: str
    ):
        payload = {
            "check_key": key,
            "tree": tree,
            "argv": result.argv,
            "cwd": result.cwd,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "blocked": result.blocked,
            "blocked_reason": result.blocked_reason,
            "output_truncated": result.output_truncated,
            "network_isolated": result.network_isolated,
            "network_enforcement": result.network_enforcement,
            "limits_enforcement": result.limits_enforcement,
            "cleanup_required": result.cleanup_required,
            "cleanup_succeeded": result.cleanup_succeeded,
            "summary": summary,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        return self.artifacts.put_text(
            json.dumps(payload, indent=2, sort_keys=True),
            kind=ArtifactKind.VERIFICATION_CHECK,
            media_type="application/json",
            created_by="verification",
        )

    def _patch_text(self, diff: TaskDiff) -> str:
        artifact = self.artifacts.get(diff.patch_artifact_id)
        if artifact.sha256 != diff.patch_sha256:
            raise VerificationError(
                "The patch artifact does not match the recorded patch hash",
                hint="Evidence is corrupted; re-run the task rather than verifying it.",
            )
        return self.artifacts.read_text(diff.patch_artifact_id)

    def _git_show(self, root: Path, commit: str, path: str) -> str | None:
        if not path or path.startswith("/") or ".." in Path(path).parts or "\x00" in path:
            raise VerificationError(f"Refusing unsafe path in baseline read: {path!r}")
        if not (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)):
            raise VerificationError("Baseline commit is not a full 40-character SHA")
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{path}"],
            check=False,
            capture_output=True,
            timeout=30,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        if result.returncode != 0:
            return None
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _safe_deterministic_code(code: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in code).upper()
        if not cleaned or not cleaned[0].isalpha():
            cleaned = "CHECK_" + cleaned
        return cleaned[:80]

    def _event(
        self,
        event_type: EventType,
        *,
        run: WorkflowRun,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
        actor_kind: ActorKind = ActorKind.SYSTEM,
    ) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=run.project_id,
                repository_id=run.repository_id,
                snapshot_id=run.snapshot_id,
                task_id=run.task_id,
                run_id=run.id,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_kind=actor_kind,
                actor_name="verification",
                payload=payload,
            )
        )

    # ------------------------------------------------------------------ bundle
    def export_bundle(self, run_id: str, destination: str | Path) -> BundleResult:
        report = self.store.latest_report(run_id)
        if report is None:
            raise NotFoundError(
                f"Run {run_id} has no verification report",
                hint=f"Run `maya verify run {run_id}` first.",
            )
        run = self.runs.get(run_id)
        task = self.tasks.get(run.task_id)
        contract = self.contracts.get(run.contract_id)
        plan = self.plans.get(run.plan_id) if run.plan_id else None
        diff = self.diffs.get_by_run(run.id)
        attempt = self.store.get_attempt(report.verification_id)
        checks = self.store.list_checks(attempt.id)
        links = self.store.list_links(report.id)
        payloads: dict[str, bytes] = {}
        payloads["report.json"] = self.artifacts.read_bytes(report.report_artifact_id)
        markdown_id = report.details.get("markdown_artifact_id")
        payloads["report.md"] = (
            self.artifacts.read_bytes(markdown_id)
            if markdown_id
            else b"# Verification report\n\nMarkdown artifact unavailable.\n"
        )
        payloads["task.json"] = json.dumps(
            task.model_dump(mode="json"), indent=2, sort_keys=True
        ).encode()
        payloads["contract.json"] = json.dumps(
            contract.model_dump(mode="json"), indent=2, sort_keys=True
        ).encode()
        payloads["run.json"] = json.dumps(
            run.model_dump(mode="json"), indent=2, sort_keys=True
        ).encode()
        if plan is not None:
            payloads["implementation-plan.json"] = json.dumps(
                plan.model_dump(mode="json"), indent=2, sort_keys=True
            ).encode()
        payloads["verification.json"] = json.dumps(
            {
                "attempt": attempt.model_dump(mode="json"),
                "checks": [item.model_dump(mode="json") for item in checks],
                "evidence_links": [item.model_dump(mode="json") for item in links],
            },
            indent=2,
            sort_keys=True,
        ).encode()
        payloads["checks.json"] = json.dumps(
            [item.model_dump(mode="json") for item in checks], indent=2, sort_keys=True
        ).encode()
        payloads["criteria.json"] = json.dumps(
            [item.model_dump(mode="json") for item in report.criteria],
            indent=2,
            sort_keys=True,
        ).encode()
        if diff is not None:
            payloads["patch.diff"] = self.artifacts.read_bytes(diff.patch_artifact_id)
            payloads["diff.json"] = json.dumps(
                diff.model_dump(mode="json"), indent=2, sort_keys=True
            ).encode()
        review = self.reviews.get_by_run(run.id)
        if review is not None:
            payloads["review.json"] = json.dumps(
                review.model_dump(mode="json"), indent=2, sort_keys=True
            ).encode()
        payloads["approvals.json"] = json.dumps(
            [
                item.model_dump(mode="json")
                for item in self.approvals.list(run_id=run.id)
            ],
            indent=2,
            sort_keys=True,
        ).encode()
        payloads["tool-calls.json"] = json.dumps(
            [
                item.model_dump(mode="json")
                for item in self._tool_calls(run.id)
            ],
            indent=2,
            sort_keys=True,
        ).encode()
        payloads["events.json"] = json.dumps(
            [
                item.model_dump(mode="json")
                for item in self.events.list(run_id=run.id, limit=10_000)
            ],
            indent=2,
            sort_keys=True,
        ).encode()
        plan_artifact_id = attempt.details.get("plan_artifact_id")
        if plan_artifact_id:
            payloads["verification-plan.json"] = self.artifacts.read_bytes(plan_artifact_id)
        artifact_ids = {
            diff.patch_artifact_id if diff is not None else None,
            *[check.artifact_id for check in checks if check.artifact_id],
            *[
                check.details.get("baseline_artifact_id")
                for check in checks
                if check.details.get("baseline_artifact_id")
            ],
        }
        for artifact_id in sorted(item for item in artifact_ids if item):
            payloads[f"artifacts/{artifact_id}"] = self.artifacts.read_bytes(artifact_id)
        result = write_bundle(destination, payloads)
        self.artifacts.put_text(
            json.dumps(result.manifest, indent=2, sort_keys=True),
            kind=ArtifactKind.EVIDENCE_BUNDLE,
            media_type="application/json",
            created_by="verification",
        )
        self._event(
            EventType.EVIDENCE_BUNDLE_EXPORTED,
            run=run,
            entity_type="verification",
            entity_id=attempt.id,
            payload={
                "report_id": report.id,
                "destination": str(result.path),
                "root_sha256": result.manifest["root_sha256"],
                "files": len(result.manifest["files"]),
            },
            actor_kind=ActorKind.USER,
        )
        return result

    def _tool_calls(self, run_id: str):  # type: ignore[no-untyped-def]
        # Imported lazily to keep the verification package free of a hard cycle
        # with the workspace/tool modules.
        from mayaworkbench.storage.stores import ToolCallStore

        store = ToolCallStore(self.db)
        return store.list_by_run(run_id, limit=5000)


__all__ = ["VerificationService", "VERIFICATION_STATES"]

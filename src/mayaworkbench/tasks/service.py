"""Task orchestration: contracts, plans, approvals, workspaces and worker jobs.

This service is the application layer. It does not call models for edits and it
does not execute commands itself; it creates the durable records, enforces the
workflow order, consumes approvals, and hands the run to the separate worker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.config import AppPaths, Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ApprovalAction,
    ApprovalStatus,
    ArtifactKind,
    EventType,
    NetworkPolicy,
    DependencyPolicy,
    SnapshotStatus,
    TaskType,
    WorkflowState,
    WorkerJobStatus,
    WorkspaceStatus,
)
from mayaworkbench.domain.models import (
    ApprovalRequest,
    CheckSpec,
    DiffFile,
    ExecutionWorkspace,
    Repository,
    RepositorySnapshot,
    ResearchContextRef,
    Task,
    TaskConstraints,
    TaskContract,
    TaskDiff,
    TaskPlan,
    TaskReview,
    TaskScope,
    WorkerJob,
    VerificationReport,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import (
    ApprovalError,
    ConflictError,
    NotFoundError,
    TaskError,
)
from mayaworkbench.ids import new_ulid
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.repositories.snapshots import RepositoryService
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    ApprovalStore,
    ContractStore,
    DiffStore,
    EventStore,
    PlanStore,
    ProjectStore,
    RepositoryStore,
    ResearchClaimStore,
    ResearchDocumentStore,
    ReviewStore,
    RunStore,
    SnapshotStore,
    TaskStore,
    ToolCallStore,
    WorkerJobStore,
    WorkspaceStore,
)
from mayaworkbench.tasks.approvals import (
    ApprovalService,
    canonical_sha256,
    expected_command_sha,
    expected_write_sha,
)
from mayaworkbench.tasks.plans import PlanService
from mayaworkbench.tasks.review import ReviewService
from mayaworkbench.tasks.workflow import WorkflowService
from mayaworkbench.verification.registry import BUILTIN_KEYS
from mayaworkbench.verification.service import VerificationService
from mayaworkbench.workspace.policy import PathPolicy, PolicyViolation, is_secret_path
from mayaworkbench.workspace.worktree import WorktreeManager


@dataclass
class StartResult:
    task: Task
    contract: TaskContract
    run: WorkflowRun
    plan: TaskPlan
    approvals: list[ApprovalRequest]


@dataclass
class RunResult:
    run: WorkflowRun
    job: WorkerJob
    log_path: str


class TaskService:
    def __init__(
        self,
        *,
        paths: AppPaths,
        settings: Settings,
        db: Database,
        projects: ProjectStore,
        repositories: RepositoryStore,
        snapshots: SnapshotStore,
        repository_service: RepositoryService,
        tasks: TaskStore,
        contracts: ContractStore,
        plans: PlanStore,
        runs: RunStore,
        approvals: ApprovalStore,
        workspaces: WorkspaceStore,
        calls: ToolCallStore,
        diffs: DiffStore,
        reviews: ReviewStore,
        jobs: WorkerJobStore,
        artifacts: ArtifactStore,
        events: EventStore,
        gateway: ModelGateway,
        plan_service: PlanService,
        approval_service: ApprovalService,
        workflow: WorkflowService,
        review_service: ReviewService,
        worktrees: WorktreeManager,
        documents: ResearchDocumentStore,
        claims: ResearchClaimStore,
        verification: VerificationService,
    ):
        self.paths = paths
        self.settings = settings
        self.db = db
        self.projects = projects
        self.repositories = repositories
        self.snapshots = snapshots
        self.repository_service = repository_service
        self.tasks = tasks
        self.contracts = contracts
        self.plans = plans
        self.runs = runs
        self.approvals = approvals
        self.workspaces = workspaces
        self.calls = calls
        self.diffs = diffs
        self.reviews = reviews
        self.jobs = jobs
        self.artifacts = artifacts
        self.events = events
        self.gateway = gateway
        self.plan_service = plan_service
        self.approval_service = approval_service
        self.workflow = workflow
        self.review_service = review_service
        self.worktrees = worktrees
        self.documents = documents
        self.claims = claims
        self.verification = verification

    # ------------------------------------------------------------------ create
    def create_task(
        self,
        *,
        project_id: str,
        title: str,
        goal: str,
        task_type: TaskType,
        snapshot_id: str,
        allowed_paths: list[str],
        excluded_paths: list[str] | None = None,
        workspace_id: str | None = None,
        resource_ids: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        constraints: dict[str, Any] | None = None,
        unknowns: list[str] | None = None,
        required_approvals: list[ApprovalAction] | None = None,
        research_document_ids: list[str] | None = None,
        claim_ids: list[str] | None = None,
        mapping_artifact_id: str | None = None,
        verification_checks: list[CheckSpec] | None = None,
        require_experiment: bool = False,
        created_by: str = "local-user",
    ) -> tuple[Task, TaskContract]:
        project = self.projects.get(project_id)
        snapshot = self.snapshots.get(snapshot_id)
        repository = self.repositories.get(snapshot.repository_id)
        if repository.project_id != project.id:
            raise TaskError("The snapshot belongs to a different project")
        if snapshot.status == SnapshotStatus.FAILED:
            raise TaskError("The pinned snapshot failed indexing and cannot be used")
        if not snapshot.commit_sha or len(snapshot.commit_sha) != 40:
            raise TaskError("The snapshot does not have a full 40-character commit SHA")
        if snapshot.commit_sha != snapshot.base_commit_sha:
            raise TaskError(
                "The snapshot's base commit and resolved commit differ; re-pin the snapshot"
            )
        constraint_values = dict(constraints or {})
        constraint_values.setdefault("allow_file_deletion", False)
        try:
            parsed_constraints = TaskConstraints.model_validate(constraint_values)
        except Exception as exc:
            raise TaskError(f"Invalid task constraints: {exc}") from exc
        if workspace_id is not None:
            from mayaworkbench.storage.stores import EngineeringWorkspaceStore

            workspace = EngineeringWorkspaceStore(self.db).get_workspace(workspace_id)
            if workspace.project_id != project.id:
                raise TaskError("The workspace belongs to a different project")
            attached = {
                item.id for item in EngineeringWorkspaceStore(self.db).resources(workspace_id)
            }
            unknown = sorted(set(resource_ids or []) - attached)
            if unknown:
                raise TaskError(f"Resource(s) are not attached to the workspace: {unknown}")
        elif resource_ids:
            raise TaskError("resource_ids require a workspace_id")
        try:
            scope = TaskScope(
                allowed_paths=allowed_paths,
                excluded_paths=excluded_paths or [],
                base_commit_sha=snapshot.commit_sha,
                include_working_tree_patch=snapshot.includes_worktree_patch,
                workspace_id=workspace_id,
                resource_ids=list(resource_ids or []),
            )
        except Exception as exc:
            raise TaskError(f"Invalid task scope: {exc}") from exc
        criteria = acceptance_criteria or [
            "The requested behavior is implemented in the isolated workspace.",
            "The final diff is reviewed and contains no out-of-scope changes.",
            "The result report distinguishes verified checks from unverified claims.",
        ]
        approvals = required_approvals or [
            ApprovalAction.WRITE_ACCESS,
            ApprovalAction.COMMAND_EXECUTION,
        ]
        if ApprovalAction.WRITE_ACCESS not in approvals:
            raise TaskError("WRITE_ACCESS approval is mandatory for an engineering task")
        if parsed_constraints.compute_profile in {"READ_ONLY", "STATIC_ANALYSIS"} and (
            ApprovalAction.COMMAND_EXECUTION in approvals
        ):
            raise TaskError(
                "READ_ONLY/STATIC_ANALYSIS profiles cannot require command execution approval"
            )

        checks = list(verification_checks or [])
        seen_keys: set[str] = set()
        criterion_ids = {f"AC{index}" for index in range(1, len(criteria) + 1)}
        for spec in checks:
            if spec.key in seen_keys:
                raise TaskError(f"Duplicate verification check key: {spec.key}")
            if spec.key in BUILTIN_KEYS:
                raise TaskError(
                    f"Verification key {spec.key} is reserved for a built-in check"
                )
            seen_keys.add(spec.key)
            unknown = sorted(set(spec.criteria) - criterion_ids)
            if unknown:
                raise TaskError(
                    f"Check {spec.key} references unknown criteria: {', '.join(unknown)}",
                    hint=f"Available criteria: {', '.join(sorted(criterion_ids))}.",
                )
            if spec.kind.value == "COMMAND" and ApprovalAction.COMMAND_EXECUTION not in approvals:
                raise TaskError(
                    f"Check {spec.key} runs a command, but the contract does not require "
                    "COMMAND_EXECUTION approval",
                    hint="Verification commands are never authorized implicitly.",
                )
            if spec.kind.value == "COMMAND" and parsed_constraints.compute_profile in {
                "READ_ONLY",
                "STATIC_ANALYSIS",
            }:
                raise TaskError(
                    f"Check {spec.key} runs a command under a profile that forbids execution"
                )
            if spec.timeout_seconds > parsed_constraints.max_runtime_seconds:
                raise TaskError(
                    f"Check {spec.key} allows {spec.timeout_seconds}s, but the task's "
                    f"max_runtime_seconds is {parsed_constraints.max_runtime_seconds}",
                    hint="A check timeout above the run budget would always be refused.",
                )

        research_context = self._build_research_context(
            project=project,
            document_ids=research_document_ids or [],
            claim_ids=claim_ids or [],
            mapping_artifact_id=mapping_artifact_id,
        )
        task = Task(
            id="task_" + new_ulid(),
            project_id=project.id,
            title=title,
            task_type=task_type,
            goal=goal,
            details={
                "snapshot_id": snapshot.id,
                "repository_id": repository.id,
                "snapshot_indexed": snapshot.status == SnapshotStatus.INDEXED,
            },
        )
        payload = {
            "task_type": task_type.value,
            "goal": goal,
            "repository_id": repository.id,
            "snapshot_id": snapshot.id,
            "scope": scope.model_dump(mode="json"),
            "constraints": parsed_constraints.model_dump(mode="json"),
            "acceptance_criteria": criteria,
            "unknowns": unknowns or [],
            "research_context": (
                research_context.model_dump(mode="json") if research_context else None
            ),
            "required_approvals": [action.value for action in approvals],
            "verification_checks": [spec.model_dump(mode="json") for spec in checks],
            "require_experiment": require_experiment,
            "contract_version": "1.0",
        }
        contract = TaskContract(
            id="ctr_" + new_ulid(),
            task_id=task.id,
            project_id=project.id,
            version=1,
            task_type=task_type,
            goal=goal,
            repository_id=repository.id,
            snapshot_id=snapshot.id,
            scope=scope,
            constraints=parsed_constraints,
            acceptance_criteria=criteria,
            unknowns=unknowns or [],
            research_context=research_context,
            verification_checks=checks,
            require_experiment=require_experiment,
            required_approvals=approvals,
            content_sha256=canonical_sha256(payload),
            created_by=created_by,
        )
        contract_artifact = self.artifacts.put_text(
            json.dumps(
                {
                    **payload,
                    "contract_id": contract.id,
                    "task_id": task.id,
                    "content_sha256": contract.content_sha256,
                },
                indent=2,
                sort_keys=True,
            ),
            kind=ArtifactKind.TASK_CONTRACT,
            media_type="application/json",
            created_by="task_create",
        )
        with self.db.transaction():
            self.tasks.create(task)
            self.contracts.create(contract)
            self.tasks.touch(task.id)
            self._event(
                EventType.TASK_CREATED,
                task=task,
                entity_type="task",
                entity_id=task.id,
                payload={
                    "title": task.title,
                    "task_type": task.task_type.value,
                    "snapshot_id": snapshot.id,
                    "contract_sha256": contract.content_sha256,
                },
            )
            self._event(
                EventType.CONTRACT_CREATED,
                task=task,
                entity_type="contract",
                entity_id=contract.id,
                payload={
                    "version": contract.version,
                    "content_sha256": contract.content_sha256,
                    "artifact_id": contract_artifact.id,
                    "allowed_paths": contract.scope.allowed_paths,
                    "excluded_paths": contract.scope.excluded_paths,
                },
            )
        return task, contract

    def _build_research_context(
        self,
        *,
        project,  # type: ignore[no-untyped-def]
        document_ids: list[str],
        claim_ids: list[str],
        mapping_artifact_id: str | None,
    ) -> ResearchContextRef | None:
        """Validate and pin the research interpretation a task implements."""
        if not document_ids and not claim_ids and not mapping_artifact_id:
            return None
        resolved_documents: list[str] = []
        for document_id in document_ids:
            try:
                document = self.documents.get(document_id)
            except NotFoundError as exc:
                raise TaskError(
                    f"Research document {document_id} was not found"
                ) from exc
            if document.project_id != project.id:
                raise TaskError(
                    f"Research document {document_id} belongs to another project"
                )
            resolved_documents.append(document.id)
        resolved_claims: list[str] = []
        for claim_id in claim_ids:
            try:
                claim = self.claims.get(claim_id)
            except NotFoundError as exc:
                raise TaskError(f"Research claim {claim_id} was not found") from exc
            if claim.project_id != project.id:
                raise TaskError(f"Research claim {claim_id} belongs to another project")
            if document_ids and claim.document_id and claim.document_id not in document_ids:
                raise TaskError(
                    f"Research claim {claim_id} comes from a document outside the pinned set"
                )
            resolved_claims.append(claim.id)
        mapping_sha256: str | None = None
        if mapping_artifact_id:
            artifact = self.artifacts.get(mapping_artifact_id)
            if artifact.kind is not ArtifactKind.CLAIM_MATRIX:
                raise TaskError(
                    f"Artifact {mapping_artifact_id} is not a claim-to-code matrix"
                )
            mapping_sha256 = artifact.sha256
        return ResearchContextRef(
            document_ids=resolved_documents,
            claim_ids=resolved_claims,
            mapping_artifact_id=mapping_artifact_id,
            mapping_sha256=mapping_sha256,
        )

    # ------------------------------------------------------------------- start
    def _reserve_run(
        self,
        *,
        task: Task,
        contract: TaskContract,
        repository: Repository,
        snapshot: RepositorySnapshot,
    ) -> WorkflowRun:
        """Create the run for a task if no other run is active.

        The active-run check and the insert share one ``BEGIN IMMEDIATE``
        transaction, so two CLI processes starting the same task cannot both
        observe "no active run" and create a second one. Holding the write lock
        only for this reservation (never across planning) keeps it short.
        """
        run = WorkflowRun(
            id="run_" + new_ulid(),
            task_id=task.id,
            contract_id=contract.id,
            project_id=task.project_id,
            repository_id=repository.id,
            snapshot_id=snapshot.id,
            state=WorkflowState.CREATED,
            base_commit_sha=contract.scope.base_commit_sha,
        )
        with self.db.transaction():
            active = [
                candidate
                for candidate in self.runs.list(task_id=task.id, limit=20)
                if not candidate.state.is_terminal
            ]
            if active:
                raise ConflictError(
                    f"Task already has an active run: {active[0].id} ({active[0].state.value})",
                    hint="Resume, cancel or review that run before starting another.",
                )
            self.runs.create(run)
            self.workflow.transition(
                run.id,
                WorkflowState.CONTRACTED,
                reason="contract attached",
                started=True,
            )
        return self.runs.get(run.id)

    def start_task(
        self,
        task_id: str,
        *,
        plan_file: str | Path | None = None,
        approval_ttl_seconds: int = 24 * 3600,
    ) -> StartResult:
        task = self.tasks.get(task_id)
        contract = self.contracts.latest(task_id)
        snapshot = self.snapshots.get(contract.snapshot_id)
        repository = self.repositories.get(contract.repository_id)
        run = self._reserve_run(
            task=task,
            contract=contract,
            repository=repository,
            snapshot=snapshot,
        )
        self.workflow.transition(
            run.id, WorkflowState.PLANNING, reason="implementation planning started"
        )
        try:
            plan = self.plan_service.create(
                run=self.runs.get(run.id),
                task=task,
                contract=contract,
                snapshot=snapshot,
                plan_file=plan_file,
            )
        except Exception as exc:
            self._fail_run(run.id, f"Planning failed: {exc}")
            raise
        self.workflow.transition(
            run.id,
            WorkflowState.AWAITING_APPROVAL,
            reason="plan ready; write and command approvals required",
            payload={"plan_id": plan.id, "plan_sha256": plan.plan_sha256},
        )
        current = self.runs.get(run.id)
        requested: list[ApprovalRequest] = []
        for action in contract.required_approvals:
            requested.append(
                self.approval_service.request(
                    run=current,
                    contract=contract,
                    action=action,
                    plan=plan,
                    ttl_seconds=approval_ttl_seconds,
                )
            )
        return StartResult(
            task=task,
            contract=contract,
            run=self.runs.get(run.id),
            plan=plan,
            approvals=requested,
        )

    # --------------------------------------------------------------- approvals
    def approve(
        self,
        approval_id: str,
        *,
        actor: str = "local-user",
        reason: str | None = None,
    ) -> ApprovalRequest:
        return self.approval_service.grant(approval_id, actor=actor, reason=reason)

    def deny(
        self, approval_id: str, *, actor: str = "local-user", reason: str
    ) -> ApprovalRequest:
        return self.approval_service.deny(approval_id, actor=actor, reason=reason)

    def approvals_for_run(self, run_id: str) -> list[ApprovalRequest]:
        return self.approval_service.list_for_run(run_id)

    # ----------------------------------------------------------------- execution
    def prepare_execution(self, run_id: str) -> RunResult:
        run = self.runs.get(run_id)
        if run.state != WorkflowState.AWAITING_APPROVAL:
            raise TaskError(
                f"Run is {run.state.value}; execution requires AWAITING_APPROVAL",
                hint="Start the task and approve its pending requests first.",
            )
        contract = self.contracts.get(run.contract_id)
        plan = self.plans.get(run.plan_id) if run.plan_id else self.plans.latest_for_run(run.id)
        approved = self.approval_service.checked_for_run(
            run=run, contract=contract, plan=plan
        )
        now = to_iso(utcnow())
        for approval in approved.values():
            if to_iso(approval.expires_at) <= now:
                raise ApprovalError(f"Approval {approval.id} expired; request a new run")
        task = self.tasks.get(run.task_id)
        snapshot = self.snapshots.get(run.snapshot_id)
        repository = self.repositories.get(run.repository_id)
        self.workflow.transition(
            run.id,
            WorkflowState.IMPLEMENTING,
            reason="approved; creating isolated workspace",
            payload={"workspace": "pending"},
        )
        try:
            workspace = self.worktrees.create(
                run=self.runs.get(run.id),
                task=task,
                contract=contract,
                repository=repository,
                snapshot=snapshot,
            )
        except Exception as exc:
            self._fail_run(run.id, f"Workspace creation failed: {exc}")
            raise
        consumed: dict[str, str] = {}
        try:
            for action, approval in approved.items():
                expected = (
                    expected_write_sha(
                        run=self.runs.get(run.id), contract=contract, plan=plan
                    )
                    if action == ApprovalAction.WRITE_ACCESS
                    else expected_command_sha(run=self.runs.get(run.id), contract=contract)
                )
                consumed_approval = self.approval_service.consume(
                    approval_id=approval.id, spec_sha256=expected
                )
                consumed[action.value] = consumed_approval.id
        except ApprovalError as exc:
            self.workflow.transition(
                run.id,
                WorkflowState.BLOCKED,
                reason=f"approval could not be consumed: {exc}",
                error=str(exc),
            )
            self._event(
                EventType.TASK_BLOCKED,
                task=task,
                run_id=run.id,
                entity_type="run",
                entity_id=run.id,
                payload={"reason": str(exc)},
            )
            raise
        self.runs.update_progress(
            run.id,
            workspace_id=workspace.id,
            details={
                **self.runs.get(run.id).details,
                "approved_actions": consumed,
                "write_spec_sha256": expected_write_sha(
                    run=self.runs.get(run.id), contract=contract, plan=plan
                ),
                "command_spec_sha256": expected_command_sha(
                    run=self.runs.get(run.id), contract=contract
                ),
            },
        )
        job = self.jobs.enqueue(
            WorkerJob(
                id="job_" + new_ulid(),
                run_id=run.id,
                task_id=run.task_id,
                project_id=run.project_id,
            )
        )
        return RunResult(
            run=self.runs.get(run.id), job=job, log_path=self._log_path(run.id)
        )

    def run_task(
        self,
        task_id: str,
        *,
        detached: bool = False,
        timeout_seconds: int | None = None,
    ) -> RunResult:
        task = self.tasks.get(task_id)
        run = self.runs.latest_for_task(task.id)
        if run is None:
            raise NotFoundError(f"Task has no run: {task.id}")
        if run.state == WorkflowState.AWAITING_APPROVAL:
            prepared = self.prepare_execution(run.id)
        elif run.state == WorkflowState.PAUSED:
            prepared = self.resume_run(run.id, spawn=False)
        elif run.state == WorkflowState.IMPLEMENTING:
            job = self.jobs.get_by_run(run.id)
            if job is None or job.status not in {WorkerJobStatus.QUEUED, WorkerJobStatus.LEASED}:
                raise TaskError(
                    "Run is IMPLEMENTING but has no queued worker job",
                    hint="Cancel the run and start a new one.",
                )
            prepared = RunResult(run=run, job=job, log_path=self._log_path(run.id))
        else:
            raise TaskError(
                f"Run is {run.state.value}; nothing to execute",
                hint="Inspect the run with `maya task status`.",
            )
        self.spawn_worker(run.id)
        if not detached:
            limit = timeout_seconds or (
                self.contracts.get(run.contract_id).constraints.max_runtime_seconds + 180
            )
            self.wait_for_run(run.id, timeout_seconds=limit)
        return RunResult(
            run=self.runs.get(run.id),
            job=prepared.job,
            log_path=self._log_path(run.id),
        )

    def resume_run(self, run_id: str, *, spawn: bool = True) -> RunResult:
        run = self.workflow.resume(run_id)
        workspace = self.workspaces.get_by_run(run.id)
        if workspace is None or workspace.status == WorkspaceStatus.REMOVED:
            raise TaskError("The workspace is gone; create a new run")
        job = self.jobs.enqueue(
            WorkerJob(
                id="job_" + new_ulid(),
                run_id=run.id,
                task_id=run.task_id,
                project_id=run.project_id,
            )
        )
        if spawn:
            self.spawn_worker(run.id)
        return RunResult(run=run, job=job, log_path=self._log_path(run.id))

    def wait_for_run(self, run_id: str, *, timeout_seconds: int = 1800) -> WorkflowRun:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            run = self.runs.get(run_id)
            if run.state.is_terminal or run.state == WorkflowState.REVIEWING:
                return run
            job = self.jobs.get_by_run(run_id)
            if job is not None and job.status in {
                WorkerJobStatus.DONE,
                WorkerJobStatus.FAILED,
                WorkerJobStatus.CANCELLED,
            }:
                return self.runs.get(run_id)
            time.sleep(0.25)
        raise TaskError(
            f"Run did not finish within {timeout_seconds} seconds; the worker may still be running",
            hint=f"Inspect {self._log_path(run_id)} and `maya task status {run_id}`.",
        )

    def pause_run(self, run_id: str) -> WorkflowRun:
        return self.workflow.pause(run_id)

    def cancel_run(self, run_id: str, *, reason: str) -> WorkflowRun:
        run = self.workflow.cancel(run_id, reason=reason)
        job = self.jobs.get_by_run(run.id)
        if job is not None and job.status == WorkerJobStatus.QUEUED:
            self.jobs.finish(job.id, status=WorkerJobStatus.CANCELLED, error=reason)
        return run

    # --------------------------------------------------------------------- diff
    def capture_diff(self, run_id: str) -> TaskDiff:
        run = self.runs.get(run_id)
        existing = self.diffs.get_by_run(run.id)
        if existing is not None:
            return existing
        workspace = self.workspaces.get_by_run(run.id)
        if workspace is None:
            raise TaskError("Run has no workspace; there is no candidate patch")
        if workspace.status == WorkspaceStatus.REMOVED:
            raise TaskError("Workspace was removed; only the stored patch remains")
        contract = self.contracts.get(run.contract_id)
        repository = self.repositories.get(run.repository_id)
        original_ok, original_head, original_status = self.worktrees.verify_original_checkout(
            workspace, repository
        )
        patch, files, warnings = self.worktrees.capture_diff(workspace)
        if not original_ok:
            warnings.append(
                "The original checkout changed while the run was active. The patch itself "
                f"was captured in the isolated workspace, but HEAD/status no longer match "
                f"the creation record (HEAD now {original_head})."
            )
        policy = PathPolicy(
            Path(workspace.path),
            allowed_paths=contract.scope.allowed_paths,
            excluded_paths=contract.scope.excluded_paths,
            allow_file_deletion=contract.constraints.allow_file_deletion,
        )
        violations: list[str] = []
        for file in files:
            try:
                policy.check_write(file.path)
            except PolicyViolation as exc:
                violations.append(file.path)
                warnings.append(f"Out-of-scope change: {file.path} ({exc})")
            if is_secret_path(file.path):
                warnings.append(f"Secret-shaped path changed: {file.path}")
        if not files:
            warnings.append("No changes were recorded in the isolated workspace.")
        artifact = self.artifacts.put_bytes(
            patch,
            kind=ArtifactKind.TASK_PATCH,
            media_type="text/x-diff; charset=utf-8",
            created_by="workspace",
        )
        diff = TaskDiff(
            id="diff_" + new_ulid(),
            run_id=run.id,
            task_id=run.task_id,
            project_id=run.project_id,
            workspace_id=workspace.id,
            base_commit_sha=workspace.base_commit_sha,
            patch_artifact_id=artifact.id,
            patch_sha256=artifact.sha256,
            files=files,
            additions=sum(item.additions for item in files),
            deletions=sum(item.deletions for item in files),
            scope_violations=sorted(set(violations)),
            warnings=warnings,
            original_checkout_unchanged=original_ok,
        )
        self.diffs.create(diff)
        self.workspaces.update(
            workspace.id,
            status=WorkspaceStatus.FINALIZED,
            patch_artifact_id=artifact.id,
            diff_sha256=artifact.sha256,
            file_count=len(files),
            finalized=True,
        )
        self.runs.update_progress(
            run.id,
            details={
                **self.runs.get(run.id).details,
                "diff_id": diff.id,
                "patch_sha256": diff.patch_sha256,
            },
        )
        self._event(
            EventType.DIFF_CAPTURED,
            task=self.tasks.get(run.task_id),
            run_id=run.id,
            entity_type="diff",
            entity_id=diff.id,
            payload={
                "patch_sha256": diff.patch_sha256,
                "files": len(files),
                "additions": diff.additions,
                "deletions": diff.deletions,
                "scope_violations": diff.scope_violations,
                "original_checkout_unchanged": diff.original_checkout_unchanged,
            },
        )
        return diff

    def review_run(self, run_id: str) -> TaskReview:
        run = self.runs.get(run_id)
        task = self.tasks.get(run.task_id)
        contract = self.contracts.get(run.contract_id)
        diff = self.capture_diff(run.id)
        review = self.review_service.review(
            run=run, task=task, contract=contract, diff=diff
        )
        self.runs.update_progress(
            run.id,
            details={
                **self.runs.get(run.id).details,
                "review_id": review.id,
                "review_artifact_id": review.report_artifact_id,
            },
        )
        return review

    def verify_run(
        self,
        run_id: str,
        *,
        extra_checks: list[CheckSpec] | None = None,
        include_review: bool = True,
        candidate_runtime: Any | None = None,
    ) -> tuple[VerificationReport, list[Any]]:
        """Capture the patch if needed and create a new verification attempt."""
        run = self.runs.get(run_id)
        if self.diffs.get_by_run(run.id) is None:
            self.capture_diff(run.id)
        return self.verification.verify(
            run.id,
            extra_checks=extra_checks,
            include_review=include_review,
            candidate_runtime=candidate_runtime,
        )

    def finish_run(
        self,
        run_id: str,
        *,
        accept: bool,
        reason: str,
        actor: str = "local-user",
        override: bool = False,
    ) -> WorkflowRun:
        run = self.runs.get(run_id)
        if run.state != WorkflowState.REVIEWING:
            raise TaskError(
                f"Run is {run.state.value}; only a REVIEWING run can be finished",
                hint="The worker must finish before a decision is recorded.",
            )
        review = self.review_run(run.id)
        if not accept:
            updated = self.workflow.transition(
                run.id,
                WorkflowState.CANCELLED,
                reason=f"user rejected the candidate patch: {reason}",
                actor_kind=ActorKind.USER,
                actor_name=actor,
                finished=True,
                error=reason,
            )
            self._event(
                EventType.TASK_CANCELLED,
                task=self.tasks.get(run.task_id),
                run_id=run.id,
                entity_type="run",
                entity_id=run.id,
                payload={"decision": "REJECTED", "reason": reason},
            )
            return updated
        errors = [finding for finding in review.findings if finding.severity == "ERROR"]
        if errors:
            raise TaskError(
                "The candidate patch has blocking review findings; it cannot be accepted",
                hint="Fix the findings in a new run, or reject this run.",
            )
        report = self.verification.latest_report(run.id)
        if report is None:
            raise TaskError(
                "Run has no verification report; acceptance requires recorded evidence",
                hint=f"Run `maya verify run {run.id}` first, or reject this run.",
            )
        verified_ok = report.decision.value in {
            "ACCEPTED",
            "COMPLETED_WITH_LIMITATIONS",
        }
        if not verified_ok and not override:
            raise TaskError(
                f"Verification decision is {report.decision.value}; acceptance is refused",
                hint=(
                    "Resolve the blocking checks, or pass --override with an explicit "
                    "reason to record a human override (the report is not changed)."
                ),
            )
        warnings = [finding for finding in review.findings if finding.severity == "WARNING"]
        target = (
            WorkflowState.COMPLETED
            if report.decision.value == "ACCEPTED"
            and not warnings
            and not run.limitations
            and not report.limitations
            else WorkflowState.COMPLETED_WITH_LIMITATIONS
        )
        limitations = list(run.limitations)
        if override and not verified_ok:
            limitations.append(
                f"The user overrode verification decision {report.decision.value}: {reason}"
            )
        # Limitations belong to the finished run even when the report's decision
        # is ACCEPTED. Otherwise a report that says "independent review did not
        # run" could be attached to a run recorded as plain COMPLETED.
        limitations.extend(
            item for item in report.limitations if item not in limitations
        )
        if limitations != run.limitations:
            self.runs.update_progress(run.id, limitations=limitations)
        updated = self.workflow.transition(
            run.id,
            target,
            reason=f"user accepted the candidate patch: {reason}",
            actor_kind=ActorKind.USER,
            actor_name=actor,
            finished=True,
            payload={
                "review_id": review.id,
                "verification_report_id": report.id,
                "verification_decision": report.decision.value,
                "override": bool(override and not verified_ok),
                "warnings": len(warnings),
            },
        )
        self.runs.update_progress(
            run.id,
            details={
                **self.runs.get(run.id).details,
                "decision": target.value,
                "decision_reason": reason,
                "verification_report_id": report.id,
                "verification_decision": report.decision.value,
                "verification_json_sha256": report.json_sha256,
                "verification_override": bool(override and not verified_ok),
            },
        )
        if override and not verified_ok:
            self._event(
                EventType.VERIFICATION_OVERRIDDEN,
                task=self.tasks.get(run.task_id),
                run_id=run.id,
                entity_type="verification",
                entity_id=report.id,
                payload={
                    "decision": report.decision.value,
                    "reason": reason,
                    "blocking": report.blocking,
                },
            )
        self._event(
            EventType.TASK_COMPLETED,
            task=self.tasks.get(run.task_id),
            run_id=run.id,
            entity_type="run",
            entity_id=run.id,
            payload={
                "decision": target.value,
                "reason": reason,
                "review_id": review.id,
                "verification_report_id": report.id,
                "verification_decision": report.decision.value,
                "override": bool(override and not verified_ok),
                "warnings": len(warnings),
            },
        )
        return self.runs.get(run.id)

    # ---------------------------------------------------------------- lifecycle
    def cleanup_run(self, run_id: str) -> ExecutionWorkspace:
        run = self.runs.get(run_id)
        if run.state == WorkflowState.IMPLEMENTING:
            raise TaskError("Cannot remove the workspace while the run is implementing")
        workspace = self.workspaces.get_by_run(run.id)
        if workspace is None:
            raise NotFoundError("Run has no workspace")
        if workspace.status == WorkspaceStatus.REMOVED:
            return workspace
        return self.worktrees.remove(workspace)

    def export_run(self, run_id: str, destination: str | Path) -> Path:
        run = self.runs.get(run_id)
        task = self.tasks.get(run.task_id)
        contract = self.contracts.get(run.contract_id)
        plan = self.plans.get(run.plan_id) if run.plan_id else None
        diff = self.diffs.get_by_run(run.id)
        review = self.reviews.get_by_run(run.id)
        verification_report = self.verification.latest_report(run.id)
        target = Path(destination).expanduser().resolve()
        if target.exists() and any(target.iterdir()):
            raise TaskError(
                f"Export directory is not empty: {target}",
                hint="Choose a new directory; Maya will not overwrite an existing bundle.",
            )
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        payloads: dict[str, str] = {
            "task.json": json.dumps(task.model_dump(mode="json"), indent=2, sort_keys=True),
            "task-contract.json": json.dumps(
                contract.model_dump(mode="json"), indent=2, sort_keys=True
            ),
            "run.json": json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True),
            "approvals.json": json.dumps(
                [item.model_dump(mode="json") for item in self.approvals.list(run_id=run.id)],
                indent=2,
                sort_keys=True,
            ),
            "tools.json": json.dumps(
                [
                    item.model_dump(mode="json")
                    for item in self.calls.list_by_run(run.id, limit=5000)
                ],
                indent=2,
                sort_keys=True,
            ),
        }
        if plan is not None:
            payloads["implementation-plan.json"] = json.dumps(
                plan.model_dump(mode="json"), indent=2, sort_keys=True
            )
        if diff is not None:
            payloads["patch.diff"] = self.artifacts.read_text(diff.patch_artifact_id)
            payloads["diff.json"] = json.dumps(
                diff.model_dump(mode="json"), indent=2, sort_keys=True
            )
        if review is not None:
            payloads["review.json"] = json.dumps(
                review.model_dump(mode="json"), indent=2, sort_keys=True
            )
        if verification_report is not None:
            payloads["verification.json"] = json.dumps(
                {
                    "report": verification_report.model_dump(mode="json"),
                    "checks": [
                        item.model_dump(mode="json")
                        for item in self.verification.report_checks(verification_report)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        payloads["report.md"] = self._report_markdown(
            run, task, contract, plan, diff, review, verification_report
        )
        hashes: list[str] = []
        for name, content in sorted(payloads.items()):
            path = target / name
            path.write_text(content, encoding="utf-8")
            import hashlib

            hashes.append(f"{hashlib.sha256(content.encode('utf-8')).hexdigest()}  {name}")
        (target / "hashes.sha256").write_text("\n".join(hashes) + "\n", encoding="utf-8")
        self._event(
            EventType.ARTIFACT_CREATED,
            task=task,
            run_id=run.id,
            entity_type="run",
            entity_id=run.id,
            payload={"export": str(target), "files": sorted(payloads)},
        )
        return target

    def _report_markdown(
        self,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        plan: TaskPlan | None,
        diff: TaskDiff | None,
        review: TaskReview | None,
        verification: VerificationReport | None = None,
    ) -> str:
        lines = [
            f"# Task report: {task.title}",
            "",
            f"- Task: `{task.id}`",
            f"- Run: `{run.id}`",
            f"- State: `{run.state.value}`",
            f"- Snapshot: `{run.snapshot_id}`",
            f"- Base commit: `{run.base_commit_sha}`",
            f"- Goal: {task.goal}",
            "",
            "## Result",
            "",
            run.finish_summary or "No agent finish summary was recorded.",
            "",
            "## Scope",
            "",
            f"- Allowed: {', '.join(contract.scope.allowed_paths)}",
            f"- Excluded: {', '.join(contract.scope.excluded_paths) or '(none)'}",
            "",
            "## Plan",
            "",
            plan.summary if plan else "No plan recorded.",
            "",
            "## Candidate patch",
            "",
            (
                f"{len(diff.files)} file(s), +{diff.additions}/-{diff.deletions}, "
                f"sha256 `{diff.patch_sha256}`."
                if diff
                else "No candidate patch was captured."
            ),
            "",
            "## Structural review",
            "",
            (
                f"{len(review.findings)} finding(s); scope_ok={review.scope_ok}; "
                f"secrets_found={review.secrets_found}."
                if review
                else "No diff review was recorded."
            ),
            "",
            "## Limitations",
            "",
            *([f"- {item}" for item in run.limitations] or ["- None recorded."]),
            "",
            "## Verification status",
            "",
            (
                f"Decision `{verification.decision.value}` from verification attempt "
                f"`{verification.verification_id}` (report `{verification.id}`, "
                f"sha256 `{verification.json_sha256}`). Exit summary: "
                + ("; ".join(verification.blocking) if verification.blocking else "no blocking reason")
                + "."
                if verification is not None
                else "No verification report exists for this run. This bundle "
                "contains a patch and a structural review only; nothing here is proof "
                "that the change works."
            ),
            "",
            (
                "## Verification limitations\n\n"
                + "\n".join(f"- {item}" for item in verification.limitations)
                if verification is not None and verification.limitations
                else ""
            ),
            "",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------- reads
    def task_view(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        contract = self.contracts.latest(task_id)
        runs = self.runs.list(task_id=task_id, limit=50)
        return {
            "task": task.model_dump(mode="json"),
            "contract": contract.model_dump(mode="json"),
            "runs": [run.model_dump(mode="json") for run in runs],
        }

    def run_view(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        task = self.tasks.get(run.task_id)
        contract = self.contracts.get(run.contract_id)
        plan = self.plans.get(run.plan_id) if run.plan_id else None
        workspace = self.workspaces.get_by_run(run.id)
        diff = self.diffs.get_by_run(run.id)
        review = self.reviews.get_by_run(run.id)
        job = self.jobs.get_by_run(run.id)
        verification_report = self.verification.latest_report(run.id)
        return {
            "run": run.model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
            "contract": contract.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json") if plan else None,
            "workspace": workspace.model_dump(mode="json") if workspace else None,
            "approvals": [
                item.model_dump(mode="json")
                for item in self.approval_service.list_for_run(run.id)
            ],
            "diff": diff.model_dump(mode="json") if diff else None,
            "review": review.model_dump(mode="json") if review else None,
            "worker_job": job.model_dump(mode="json") if job else None,
            "verification": (
                {
                    **verification_report.model_dump(mode="json"),
                    "checks": [
                        check.model_dump(mode="json")
                        for check in self.verification.report_checks(verification_report)
                    ],
                }
                if verification_report
                else None
            ),
            "tool_calls": [
                item.model_dump(mode="json")
                for item in self.calls.list_by_run(run.id, limit=200)
            ],
        }

    def list_tasks(self, *, project_id: str | None = None, limit: int = 100) -> list[Task]:
        return self.tasks.list(project_id=project_id, limit=limit)

    def latest_run_for_task(self, task_id: str) -> WorkflowRun | None:
        return self.runs.latest_for_task(task_id)

    # ----------------------------------------------------------------- helpers
    def _fail_run(self, run_id: str, reason: str) -> None:
        run = self.runs.get(run_id)
        if run.state.is_terminal:
            return
        try:
            self.workflow.transition(
                run_id,
                WorkflowState.FAILED,
                reason=reason,
                error=reason,
                finished=True,
            )
        except TaskError:
            pass
        self._event(
            EventType.TASK_FAILED,
            task=self.tasks.get(run.task_id),
            run_id=run_id,
            entity_type="run",
            entity_id=run_id,
            payload={"reason": reason[:4000]},
        )

    def _log_path(self, run_id: str) -> str:
        logs = self.paths.home / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        return str(logs / f"worker-{run_id}.log")

    def spawn_worker(self, run_id: str) -> int:
        log_path = self._log_path(run_id)
        with open(log_path, "ab") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mayaworkbench.worker",
                    "--home",
                    str(self.paths.home),
                    "--once",
                    "--run-id",
                    run_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=str(self.paths.home),
            )
        self.runs.update_progress(
            run_id,
            details={
                **self.runs.get(run_id).details,
                "worker_pid": process.pid,
                "worker_log": log_path,
            },
        )
        return process.pid

    def _event(
        self,
        event_type: EventType,
        *,
        task: Task,
        run_id: str | None = None,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=task.project_id,
                task_id=task.id,
                run_id=run_id,
                snapshot_id=str(task.details.get("snapshot_id") or "") or None,
                repository_id=str(task.details.get("repository_id") or "") or None,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_kind=ActorKind.USER,
                actor_name="local-user",
                payload=payload or {},
            )
        )


__all__ = ["TaskService", "StartResult", "RunResult"]

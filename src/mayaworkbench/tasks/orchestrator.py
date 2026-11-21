"""The single deterministic role orchestrator (WP8, audit §9.1–§9.3).

One orchestrator walks a typed ``RolePlan`` in dependency order. It never is a
swarm: roles cannot invent objectives, choose tools, or announce a workflow
state. The only model-driven step is the IMPLEMENTER, which reuses the existing
``ControlledAgent`` and its ToolGateway; every other role is a deterministic
analysis step that writes a typed ``RoleArtifact`` into the content-addressed
artifact store and a compact record into run details.

State authority stays with :class:`WorkflowService`. This module reads run state
and writes details/artifacts only; it never calls ``transition``. The worker
settles the returned ``AgentOutcome`` through the backend state machine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import Field

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
    WorkflowState,
)
from mayaworkbench.domain.models import (
    Record,
    Task,
    TaskContract,
    TaskPlan,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.ids import new_ulid
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import EventStore, RunStore
from mayaworkbench.tasks.agent import AgentOutcome
from mayaworkbench.tasks.inloop import FeedbackRound, repair_payload
from mayaworkbench.tasks.roles import (
    REVIEWER_CONTEXT,
    AgentRole,
    RoleInvocation,
    RolePlan,
    StepState,
)
from mayaworkbench.tasks.workflow import WorkflowService
from mayaworkbench.workspace.tools import ToolGateway


class RoleArtifact(Record):
    """The typed handoff emitted by one role step."""

    step_id: str
    role: AgentRole
    output_artifact: str
    artifact_id: str | None = None
    produced_by: str
    state: StepState
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    terminal_reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    context_sources: list[str] = Field(default_factory=list)
    model_label: str = "default"
    authority: str = ""


class RepairAttempt(Record):
    """A bounded repair proposal with all six fields audit §9.3 requires."""

    failing_evidence: list[dict[str, Any]] = Field(min_length=1)
    failing_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_cause_hypothesis: str = Field(min_length=8, max_length=4000)
    predicted_patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    targeted_validation: str = Field(min_length=4, max_length=2000)
    regression_validation: str = Field(min_length=4, max_length=2000)
    round: int = Field(ge=1, le=10)


@dataclass(frozen=True)
class RepairDecision:
    accepted: bool
    reason: str


class RepairGuard:
    """Rejects incomplete repairs and repeats of a deterministic failure.

    ``inloop.py`` remains the source of failing evidence: ``build_attempt``
    converts a :class:`FeedbackRound` (from the declared in-loop checks) into a
    typed attempt. A second attempt with the same failing signature *and* the
    same predicted patch hash is refused — a deterministic failure will produce
    the same evidence, so replaying the same patch cannot be a new hypothesis.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        ledger: list[dict[str, Any]] | None = None,
    ) -> None:
        self.max_attempts = max(0, int(max_attempts))
        self.entries: list[dict[str, Any]] = [dict(item) for item in (ledger or [])]

    def build_attempt(
        self,
        round_result: FeedbackRound,
        *,
        root_cause_hypothesis: str,
        predicted_patch_sha256: str,
        targeted_validation: str,
        regression_validation: str,
        round_number: int,
    ) -> RepairAttempt:
        payload = repair_payload(
            round_result,
            round_number=round_number,
            max_rounds=self.max_attempts + 1,
        )
        evidence = list(payload["checks"])
        if not evidence:
            raise ValueError("a repair attempt requires failing evidence")
        signature_source = [
            {
                "key": item.get("key"),
                "outcome": item.get("outcome"),
                "excerpt": item.get("output_excerpt", ""),
                "failing_tests": item.get("failing_tests", []),
            }
            for item in evidence
        ]
        signature = hashlib.sha256(
            json.dumps(signature_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return RepairAttempt(
            failing_evidence=evidence,
            failing_signature=signature,
            root_cause_hypothesis=root_cause_hypothesis,
            predicted_patch_sha256=predicted_patch_sha256,
            targeted_validation=targeted_validation,
            regression_validation=regression_validation,
            round=round_number,
        )

    def evaluate(self, attempt: RepairAttempt) -> RepairDecision:
        seen = [
            item
            for item in self.entries
            if item.get("failing_signature") == attempt.failing_signature
        ]
        if any(
            item.get("predicted_patch_sha256") == attempt.predicted_patch_sha256
            for item in seen
        ):
            return RepairDecision(
                False,
                "refused: the same deterministic failure was already repaired with this patch hash",
            )
        if len(seen) >= self.max_attempts:
            return RepairDecision(
                False,
                "refused: the repair budget for this failing signature is exhausted",
            )
        return RepairDecision(True, "accepted")

    def record(self, attempt: RepairAttempt) -> None:
        self.entries.append(attempt.model_dump(mode="json"))


def role_plan_enabled(run: WorkflowRun) -> bool:
    """Read the contract flag from run details (default off: old path unchanged)."""
    return bool(run.details.get("use_role_plan", False))


def set_role_plan_enabled(runs: RunStore, run_id: str, *, enabled: bool = True) -> WorkflowRun:
    """Set the flag through the store; used by ``maya task run --roles``."""
    run = runs.get(run_id)
    details = dict(run.details)
    details["use_role_plan"] = bool(enabled)
    runs.update_progress(run_id, details=details)
    return runs.get(run_id)


class RoleOrchestrator:
    """Walk a :class:`RolePlan` without ever mutating workflow state."""

    def __init__(
        self,
        *,
        runs: RunStore,
        artifacts: ArtifactStore,
        events: EventStore,
        workflow: WorkflowService,
        plan: RolePlan,
        task: Task,
        contract: TaskContract,
        run: WorkflowRun,
        orientation: dict[str, Any] | None = None,
        implementation_plan: TaskPlan | None = None,
        tool_gateway: ToolGateway | None = None,
        implementer: Callable[[], AgentOutcome] | None = None,
        patch_provider: Callable[[], Any] | None = None,
        step_observer: Callable[[str], None] | None = None,
    ) -> None:
        self.runs = runs
        self.artifacts = artifacts
        self.events = events
        # Held so the boundary is explicit: the orchestrator uses the backend's
        # transition graph as a read-only reference and never calls transition.
        self.workflow = workflow
        self.plan = plan
        self.task = task
        self.contract = contract
        self.run_id = run.id
        self.orientation = orientation or {}
        self.implementation_plan = implementation_plan
        self.tool_gateway = tool_gateway
        self.implementer = implementer
        self.patch_provider = patch_provider
        self.step_observer = step_observer
        self.patch_capture_error: str | None = None
        self.repair_guard = RepairGuard(
            max_attempts=contract.constraints.max_repair_attempts,
            ledger=list(run.details.get("role_repair_ledger") or []),
        )

    # ------------------------------------------------------------------- walk
    def run(self) -> AgentOutcome:
        current = self.runs.get(self.run_id)
        if current.cancel_requested:
            return self._control_outcome(WorkflowState.CANCELLED, "cancelled")
        if current.pause_requested:
            return self._control_outcome(WorkflowState.PAUSED, "paused")
        if current.state.is_terminal:
            return self._control_outcome(current.state, "already terminal")

        completed = list(current.details.get("role_plan_completed") or [])
        artifacts_meta = list(current.details.get("role_artifacts") or [])
        implementation_outcome: AgentOutcome | None = None

        for step in self.plan.steps:
            live = self.runs.get(self.run_id)
            if live.cancel_requested:
                return self._control_outcome(WorkflowState.CANCELLED, "cancelled")
            if live.pause_requested:
                return self._control_outcome(WorkflowState.PAUSED, "paused")
            if step.step_id in completed:
                # Idempotency: a resumed run never re-emits a finished handoff.
                continue
            unmet = [dep for dep in step.dependencies if dep not in completed]
            if unmet:
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="Role plan dependency was not satisfied.",
                    limitations=[f"Step {step.step_id} waits for: {', '.join(unmet)}."],
                    error="role_plan_dependency_unmet",
                    steps=live.step_count,
                    tool_calls=live.tool_call_count,
                    tokens=live.model_tokens,
                    cost_usd=live.model_cost_usd,
                )

            artifact, agent_outcome = self._execute_step(step, artifacts_meta)
            if agent_outcome is not None:
                implementation_outcome = agent_outcome
                if agent_outcome.state != WorkflowState.REVIEWING:
                    # Pause/cancel/failure inside the implementer must not be
                    # recorded as a completed handoff: resume re-runs it from
                    # the durable tool ledger.
                    return agent_outcome

            artifacts_meta.append(artifact.model_dump(mode="json"))
            completed.append(step.step_id)
            self._write_details(live, completed, artifacts_meta)
            self._event(step, artifact)
            if self.step_observer is not None:
                self.step_observer(step.step_id)

        if implementation_outcome is not None:
            return implementation_outcome
        return AgentOutcome(
            state=WorkflowState.REVIEWING,
            summary=(
                "All deterministic role steps completed; this task template has no "
                "model-driven implementer step."
            ),
            limitations=[
                "No model-driven implementation was requested for this task type; "
                "verification remains authoritative."
            ],
            steps=current.step_count,
            tool_calls=current.tool_call_count,
            tokens=current.model_tokens,
            cost_usd=current.model_cost_usd,
        )

    # ------------------------------------------------------------ execution
    def _execute_step(
        self,
        step: RoleInvocation,
        artifacts_meta: list[dict[str, Any]],
    ) -> tuple[RoleArtifact, AgentOutcome | None]:
        if step.role == AgentRole.IMPLEMENTER:
            if self.implementer is None:
                outcome = AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="No ControlledAgent was supplied for the implementer role.",
                    error="role_implementer_missing",
                )
                artifact = RoleArtifact(
                    step_id=step.step_id,
                    role=step.role,
                    output_artifact=step.output_artifact,
                    produced_by="orchestrator",
                    state=StepState.BLOCKED,
                    terminal_reason=outcome.error,
                    input_hashes=dict(step.input_hashes),
                    model_label=step.model_label,
                    authority=step.authority,
                )
                return artifact, outcome
            outcome = self.implementer()
            run = self.runs.get(self.run_id)
            patch_sha = str(run.details.get("patch_sha256") or "")
            evidence = {
                "agent_state": outcome.state.value,
                "steps": outcome.steps,
                "tool_calls": outcome.tool_calls,
                "in_loop_verdict": run.details.get("in_loop_verdict"),
            }
            stored = self.artifacts.put_text(
                json.dumps(
                    {
                        "role": step.role.value,
                        "output_artifact": step.output_artifact,
                        "produced_by": "ControlledAgent",
                        "summary": outcome.summary,
                        "limitations": outcome.limitations,
                        "evidence": evidence,
                    },
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                kind=ArtifactKind.OTHER,
                media_type="application/json",
                created_by="ControlledAgent",
            )
            artifact = RoleArtifact(
                step_id=step.step_id,
                role=step.role,
                output_artifact=step.output_artifact,
                artifact_id=stored.id,
                produced_by="ControlledAgent",
                state=StepState.SUCCEEDED if outcome.state == WorkflowState.REVIEWING else StepState.FAILED,
                input_hashes=dict(step.input_hashes),
                output_hashes={"patch": patch_sha} if patch_sha else {},
                summary=outcome.summary,
                evidence=evidence,
                model_label=step.model_label,
                authority=step.authority,
            )
            return artifact, outcome

        if step.role in {AgentRole.INDEPENDENT_REVIEWER, AgentRole.RELEASE_COORDINATOR}:
            self._ensure_patch_captured()
        payload = self._deterministic_payload(step, artifacts_meta)
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        stored = self.artifacts.put_text(
            text,
            kind=ArtifactKind.OTHER,
            media_type="application/json",
            created_by=f"orchestrator:{step.role.value}",
        )
        output_hashes = {"artifact": stored.sha256}
        artifact = RoleArtifact(
            step_id=step.step_id,
            role=step.role,
            output_artifact=step.output_artifact,
            artifact_id=stored.id,
            produced_by="deterministic",
            state=StepState.SUCCEEDED,
            input_hashes=dict(step.input_hashes),
            output_hashes=output_hashes,
            summary=str(payload.get("summary", "")),
            evidence=dict(payload.get("evidence", {})),
            context_sources=list(step.context_sources),
            model_label=step.model_label,
            authority=step.authority,
        )
        return artifact, None

    # -------------------------------------------------------- deterministic
    def _deterministic_payload(
        self,
        step: RoleInvocation,
        artifacts_meta: list[dict[str, Any]],
    ) -> dict[str, Any]:
        contract = self.contract
        run = self.runs.get(self.run_id)
        rounds = list(run.details.get("in_loop_check_rounds") or [])
        base: dict[str, Any] = {
            "role": step.role.value,
            "output_artifact": step.output_artifact,
            "objective": step.objective,
            "authority": step.authority,
            "model_label": step.model_label,
            "context_sources": list(step.context_sources),
            "summary": "",
            "evidence": {},
        }
        if step.role == AgentRole.TASK_ANALYST:
            base["summary"] = "Task intent validated against the immutable contract."
            base["evidence"] = {
                "acceptance_criteria": list(contract.acceptance_criteria),
                "unknowns": list(contract.unknowns),
                "missing_constraints": self._missing_constraints(),
            }
        elif step.role == AgentRole.RESEARCH_ANALYST:
            reference = contract.research_context
            base["summary"] = "Pinned research evidence collected."
            base["evidence"] = {
                "document_ids": list(reference.document_ids) if reference else [],
                "claim_ids": list(reference.claim_ids) if reference else [],
                "mapping_sha256": reference.mapping_sha256 if reference else None,
                "orientation_spans": self.orientation.get("spans", [])[:20],
            }
        elif step.role == AgentRole.WORKSPACE_ANALYST:
            base["summary"] = "Workspace structure and index limitations mapped."
            base["evidence"] = {
                "allowed_paths": list(contract.scope.allowed_paths),
                "excluded_paths": list(contract.scope.excluded_paths),
                "symbols": self.orientation.get("symbols", [])[:40],
                "index_issues": self.orientation.get("issues", [])[:20],
                "index_available": bool(self.orientation.get("index_available", False)),
            }
        elif step.role == AgentRole.ARCHITECT:
            plan = self.implementation_plan
            base["summary"] = plan.summary if plan else "No implementation plan is attached."
            base["evidence"] = {
                "plan_id": plan.id if plan else None,
                "plan_sha256": plan.plan_sha256 if plan else None,
                "steps": [item.model_dump(mode="json") for item in plan.steps] if plan else [],
                "risks": list(plan.risks) if plan else [],
            }
        elif step.role == AgentRole.ENVIRONMENT_ENGINEER:
            checks = [
                {
                    "key": item.key,
                    "kind": item.kind.value,
                    "argv": list(item.argv or []),
                    "timeout_seconds": item.timeout_seconds,
                }
                for item in contract.verification_checks
            ]
            base["summary"] = "Runtime and declared execution plan recorded; no command was run by this role."
            base["evidence"] = {
                "execution_trust": contract.constraints.execution_trust,
                "network_access": contract.constraints.network_access,
                "capabilities": list(contract.constraints.capabilities),
                "declared_checks": checks,
            }
        elif step.role == AgentRole.QA_TEST_ENGINEER:
            base["summary"] = "Declared checks and advisory in-loop results summarised."
            base["evidence"] = {
                "declared_checks": [item.key for item in contract.verification_checks],
                "in_loop_rounds": rounds,
                "verdict": run.details.get("in_loop_verdict"),
                "note": (
                    "In-loop rounds are advisory; `maya verify run` is the authoritative "
                    "evidence. This role cannot weaken or remove a declared check."
                ),
            }
        elif step.role == AgentRole.SECURITY_REVIEWER:
            findings: list[str] = []
            if contract.constraints.execution_trust == "LOCAL_TRUSTED":
                findings.append(
                    "LOCAL_TRUSTED is host-user privilege, not a sandbox; no isolation is claimed."
                )
            if contract.constraints.network_access == "ALLOW_WITH_APPROVAL":
                findings.append("Network egress was an explicit contract choice; audit command output.")
            if not contract.verification_checks:
                findings.append("No declared verification checks; functional acceptance is unverified.")
            base["summary"] = "Deterministic security observations recorded."
            base["evidence"] = {
                "allowed_paths": list(contract.scope.allowed_paths),
                "excluded_paths": list(contract.scope.excluded_paths),
                "required_approvals": [item.value for item in contract.required_approvals],
                "findings": findings,
                "read_only": True,
            }
        elif step.role == AgentRole.EXPERIMENT_ANALYST:
            base["summary"] = "Experiment context recorded; no unverified result is claimed."
            base["evidence"] = {
                "require_experiment": contract.require_experiment,
                "available_in_loop_metrics": [],
                "note": (
                    "Gate F is evaluated from attached experiment evidence by "
                    "`maya verify run`, not from an in-loop model opinion."
                ),
            }
        elif step.role == AgentRole.INDEPENDENT_REVIEWER:
            # Deliberately does NOT read outcome.summary / finish_summary. The
            # implementer's self-evaluation is not evidence for this role.
            base["summary"] = "Independent context assembled from backend-owned evidence only."
            base["evidence"] = {
                "contract": {
                    "id": contract.id,
                    "content_sha256": contract.content_sha256,
                    "allowed_paths": list(contract.scope.allowed_paths),
                    "acceptance_criteria": list(contract.acceptance_criteria),
                    "declared_checks": [item.key for item in contract.verification_checks],
                },
                "patch": {
                    "sha256": run.details.get("patch_sha256"),
                    "diff_id": run.details.get("diff_id"),
                },
                "deterministic_results": rounds,
                "evidence": [
                    item.get("artifact_id") for item in rounds if item.get("artifact_id")
                ],
                "patch_capture_error": self.patch_capture_error,
                "excluded_sources": ["implementer_self_evaluation"],
                "context_sources": list(REVIEWER_CONTEXT),
            }
        elif step.role == AgentRole.RELEASE_COORDINATOR:
            base["summary"] = "Draft delivery package assembled; publishing requires a human."
            base["evidence"] = {
                "manifest": [
                    {
                        "step_id": item.get("step_id"),
                        "role": item.get("role"),
                        "artifact_id": item.get("artifact_id"),
                        "sha256": (item.get("output_hashes") or {}).get("artifact"),
                    }
                    for item in artifacts_meta
                ],
                "draft": True,
                "publishing_requires_human_approval": True,
            }
        else:  # pragma: no cover - all selected roles are handled above
            base["summary"] = f"No deterministic payload defined for {step.role.value}."
        return base

    def _ensure_patch_captured(self) -> None:
        """Capture the candidate patch before evidence-consuming roles run.

        Patch capture is a backend action supplied by the worker, never by a
        model. ``capture_diff`` is idempotent, so the worker's later settlement
        call is harmless. A failure is recorded honestly instead of fabricating
        a patch hash for the reviewer.
        """
        if self.patch_provider is None or self.patch_capture_error is not None:
            return
        if self.runs.get(self.run_id).details.get("patch_sha256"):
            return
        try:
            self.patch_provider()
        except Exception as exc:  # noqa: BLE001 - review must degrade, not crash
            self.patch_capture_error = f"{type(exc).__name__}: {exc}"[:400]

    def _missing_constraints(self) -> list[str]:
        missing: list[str] = []
        if not self.contract.verification_checks:
            missing.append("no declared verification checks")
        if not self.contract.unknowns:
            missing.append("no explicit unknowns recorded")
        if self.contract.constraints.max_repair_attempts == 0:
            missing.append("repair budget is zero")
        return missing

    # ------------------------------------------------------------ repair
    def apply_repair_attempt(
        self,
        round_result: FeedbackRound,
        *,
        root_cause_hypothesis: str,
        predicted_patch_sha256: str,
        targeted_validation: str,
        regression_validation: str,
        round_number: int,
    ) -> tuple[RepairAttempt, RepairDecision]:
        """Validate a repair against in-loop evidence and persist accepted ones."""
        attempt = self.repair_guard.build_attempt(
            round_result,
            root_cause_hypothesis=root_cause_hypothesis,
            predicted_patch_sha256=predicted_patch_sha256,
            targeted_validation=targeted_validation,
            regression_validation=regression_validation,
            round_number=round_number,
        )
        decision = self.repair_guard.evaluate(attempt)
        if decision.accepted:
            self.repair_guard.record(attempt)
            live = self.runs.get(self.run_id)
            completed = list(live.details.get("role_plan_completed") or [])
            artifacts_meta = list(live.details.get("role_artifacts") or [])
            self._write_details(live, completed, artifacts_meta)
        return attempt, decision

    # ------------------------------------------------------------ persistence
    def _write_details(
        self,
        run: WorkflowRun,
        completed: list[str],
        artifacts_meta: list[dict[str, Any]],
    ) -> None:
        details = dict(self.runs.get(self.run_id).details)
        details["role_plan_sha256"] = self.plan.plan_sha256
        details["role_plan_completed"] = list(dict.fromkeys(completed))
        details["role_artifacts"] = artifacts_meta
        details["role_repair_ledger"] = self.repair_guard.entries
        self.runs.update_progress(self.run_id, details=details)

    def _control_outcome(self, state: WorkflowState, reason: str) -> AgentOutcome:
        run = self.runs.get(self.run_id)
        summary = {
            WorkflowState.PAUSED: "Pause was requested; the orchestrator stopped at a safe step boundary.",
            WorkflowState.CANCELLED: "Cancellation was requested; the orchestrator stopped at a safe step boundary.",
        }.get(state, "The run is already in a terminal state.")
        return AgentOutcome(
            state=state,
            summary=summary,
            error=reason,
            steps=run.step_count,
            tool_calls=run.tool_call_count,
            tokens=run.model_tokens,
            cost_usd=run.model_cost_usd,
        )

    def _event(self, step: RoleInvocation, artifact: RoleArtifact) -> None:
        run = self.runs.get(self.run_id)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.ARTIFACT_CREATED,
                project_id=run.project_id,
                repository_id=run.repository_id,
                snapshot_id=run.snapshot_id,
                task_id=run.task_id,
                run_id=run.id,
                entity_type="role_artifact",
                entity_id=artifact.artifact_id or f"{run.id}:{step.step_id}",
                actor_kind=ActorKind.SYSTEM,
                actor_name=f"orchestrator:{step.role.value}",
                occurred_at=utcnow(),
                payload={
                    "step_id": step.step_id,
                    "role": step.role.value,
                    "output_artifact": step.output_artifact,
                    "artifact_id": artifact.artifact_id,
                    "sha256": next(iter(artifact.output_hashes.values()), None),
                    "model_label": step.model_label,
                    "authority": step.authority,
                    "plan_sha256": self.plan.plan_sha256,
                },
            )
        )


__all__ = [
    "RoleArtifact",
    "RepairAttempt",
    "RepairDecision",
    "RepairGuard",
    "RoleOrchestrator",
    "role_plan_enabled",
    "set_role_plan_enabled",
]

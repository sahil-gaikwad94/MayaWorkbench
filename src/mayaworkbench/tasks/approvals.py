"""Durable human approvals.

An approval is not a boolean kept in memory. It records the exact action
specification hash shown to the user, a nonce for replay protection, an expiry,
and a one-time consumption. The spec hash binds:

* the task contract content (scope, budgets, acceptance criteria);
* the implementation plan, when present;
* the snapshot and base commit;
* for commands, the complete command policy and runner limits.

Changing any of those requires a new approval. The gateway re-checks the hash
before every side effect.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ApprovalAction,
    ApprovalStatus,
    EventType,
    WorkflowState,
)
from mayaworkbench.domain.models import (
    ApprovalRequest,
    Task,
    TaskContract,
    TaskPlan,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import ApprovalError, TaskError
from mayaworkbench.ids import new_ulid
from mayaworkbench.storage.stores import ApprovalStore, EventStore, PlanStore, RunStore
from mayaworkbench.workspace.runner import CommandPolicy

DEFAULT_TTL_SECONDS = 24 * 3600


def canonical_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def write_spec(
    *,
    run: WorkflowRun,
    contract: TaskContract,
    plan: TaskPlan | None,
) -> dict[str, Any]:
    return {
        "action": ApprovalAction.WRITE_ACCESS.value,
        "run_id": run.id,
        "task_id": run.task_id,
        "contract_id": contract.id,
        "contract_sha256": contract.content_sha256,
        "plan_sha256": plan.plan_sha256 if plan is not None else None,
        "snapshot_id": contract.snapshot_id,
        "base_commit_sha": contract.scope.base_commit_sha,
        "allowed_paths": contract.scope.allowed_paths,
        "excluded_paths": contract.scope.excluded_paths,
        "allow_file_deletion": contract.constraints.allow_file_deletion,
    }


def command_spec(*, run: WorkflowRun, contract: TaskContract) -> dict[str, Any]:
    policy = CommandPolicy.from_constraints(contract.constraints)
    return {
        "action": ApprovalAction.COMMAND_EXECUTION.value,
        "run_id": run.id,
        "task_id": run.task_id,
        "contract_id": contract.id,
        "contract_sha256": contract.content_sha256,
        "snapshot_id": contract.snapshot_id,
        "base_commit_sha": contract.scope.base_commit_sha,
        "command_policy": policy.describe(),
    }


def expected_write_sha(*, run: WorkflowRun, contract: TaskContract, plan: TaskPlan | None) -> str:
    return canonical_sha256(write_spec(run=run, contract=contract, plan=plan))


def expected_command_sha(*, run: WorkflowRun, contract: TaskContract) -> str:
    return canonical_sha256(command_spec(run=run, contract=contract))


class ApprovalService:
    def __init__(
        self,
        approvals: ApprovalStore,
        runs: RunStore,
        plans: PlanStore,
        events: EventStore,
    ):
        self.approvals = approvals
        self.runs = runs
        self.plans = plans
        self.events = events

    # ----------------------------------------------------------------- request
    def request(
        self,
        *,
        run: WorkflowRun,
        contract: TaskContract,
        action: ApprovalAction,
        plan: TaskPlan | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> ApprovalRequest:
        if ttl_seconds < 60 or ttl_seconds > 7 * 24 * 3600:
            raise ApprovalError("approval TTL must be between 60 seconds and 7 days")
        if action == ApprovalAction.WRITE_ACCESS:
            if plan is None:
                raise ApprovalError("write access requires a stored implementation plan")
            spec = write_spec(run=run, contract=contract, plan=plan)
        elif action == ApprovalAction.COMMAND_EXECUTION:
            spec = command_spec(run=run, contract=contract)
        else:  # pragma: no cover - enum exhaustiveness
            raise ApprovalError(f"unsupported approval action: {action}")
        spec_hash = canonical_sha256(spec)
        for existing in self.approvals.find_pending(run.id, action.value):
            if existing.spec_sha256 == spec_hash:
                return existing
        from mayaworkbench.tasks.approval_tokens import approval_message, sign_approval

        expires_at_value = utcnow() + timedelta(seconds=ttl_seconds)
        token_message = approval_message(
            approval_id="apr_" + new_ulid(),
            spec_sha256=spec_hash,
            nonce=secrets.token_hex(24),
            expires_at=to_iso(expires_at_value),
            action=action.value,
        )
        envelope = sign_approval(token_message)
        approval = ApprovalRequest(
            id=token_message["approval_id"],
            run_id=run.id,
            task_id=run.task_id,
            project_id=run.project_id,
            action=action,
            resource_scope=spec,
            spec_sha256=spec_hash,
            nonce=token_message["nonce"],
            expires_at=expires_at_value,
            details={
                "contract_id": contract.id,
                "plan_id": plan.id if plan is not None else None,
                "approval_hmac": envelope,
            },
        )
        self.approvals.create(approval)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.APPROVAL_REQUESTED,
                project_id=run.project_id,
                task_id=run.task_id,
                run_id=run.id,
                snapshot_id=run.snapshot_id,
                entity_type="approval",
                entity_id=approval.id,
                actor_kind=ActorKind.SYSTEM,
                actor_name="approval_service",
                payload={
                    "action": action.value,
                    "spec_sha256": spec_hash,
                    "expires_at": to_iso(approval.expires_at),
                    "resource_scope": _safe_scope(spec),
                },
            )
        )
        return approval

    # ------------------------------------------------------------------ decide
    def grant(
        self, approval_id: str, *, actor: str = "local-user", reason: str | None = None
    ) -> ApprovalRequest:
        approval = self._pending_or_expired(approval_id)
        if not self.approvals.decide(
            approval_id, approve=True, actor=actor, reason=reason
        ):
            raise ApprovalError("Approval was already decided by another process")
        approved = self.approvals.get(approval_id)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.APPROVAL_GRANTED,
                project_id=approved.project_id,
                task_id=approved.task_id,
                run_id=approved.run_id,
                entity_type="approval",
                entity_id=approved.id,
                actor_kind=ActorKind.USER,
                actor_name=actor,
                payload={
                    "action": approved.action.value,
                    "spec_sha256": approved.spec_sha256,
                    "reason": reason,
                },
            )
        )
        if approved.action == ApprovalAction.WRITE_ACCESS:
            details = approved.details
            if details.get("plan_id"):
                self.events.append(
                    WorkflowEvent(
                        event_id="evt_" + new_ulid(),
                        event_type=EventType.PLAN_APPROVED,
                        project_id=approved.project_id,
                        task_id=approved.task_id,
                        run_id=approved.run_id,
                        entity_type="plan",
                        entity_id=str(details["plan_id"]),
                        actor_kind=ActorKind.USER,
                        actor_name=actor,
                        payload={"approval_id": approved.id},
                    )
                )
        return approved

    def deny(
        self, approval_id: str, *, actor: str = "local-user", reason: str
    ) -> ApprovalRequest:
        approval = self._pending_or_expired(approval_id)
        if not self.approvals.decide(
            approval_id, approve=False, actor=actor, reason=reason
        ):
            raise ApprovalError("Approval was already decided by another process")
        denied = self.approvals.get(approval_id)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.APPROVAL_DENIED,
                project_id=denied.project_id,
                task_id=denied.task_id,
                run_id=denied.run_id,
                entity_type="approval",
                entity_id=denied.id,
                actor_kind=ActorKind.USER,
                actor_name=actor,
                payload={"action": denied.action.value, "reason": reason},
            )
        )
        run = self.runs.get(denied.run_id)
        if not run.state.is_terminal:
            from mayaworkbench.tasks.workflow import WorkflowService

            WorkflowService(self.runs, self.events).transition(
                run.id,
                WorkflowState.CANCELLED,
                reason=f"{denied.action.value} approval denied: {reason}",
                actor_kind=ActorKind.USER,
                actor_name=actor,
                finished=True,
            )
        return denied

    # ----------------------------------------------------------------- consume
    def consume(self, *, approval_id: str, spec_sha256: str) -> ApprovalRequest:
        approval = self.approvals.get(approval_id)
        if approval.status != ApprovalStatus.APPROVED:
            raise ApprovalError(
                f"Approval {approval_id} is {approval.status.value}, not APPROVED"
            )
        if approval.spec_sha256 != spec_sha256:
            raise ApprovalError("Approval specification hash does not match this action")
        now_iso = to_iso(utcnow())
        if to_iso(approval.expires_at) <= now_iso:
            raise ApprovalError(
                f"Approval {approval_id} expired at {to_iso(approval.expires_at)}"
            )
        from mayaworkbench.tasks.approval_tokens import approval_message, verify_approval

        envelope = (approval.details or {}).get("approval_hmac")
        message = approval_message(
            approval_id=approval.id,
            spec_sha256=approval.spec_sha256,
            nonce=approval.nonce,
            expires_at=to_iso(approval.expires_at),
            action=approval.action.value,
        )
        if not verify_approval(message, envelope):
            raise ApprovalError("Approval signature is invalid or its signing key is unavailable")
        consumed = self.approvals.consume(
            approval_id,
            spec_sha256=spec_sha256,
            nonce=approval.nonce,
            now_iso=now_iso,
        )
        if consumed is None:
            raise ApprovalError(
                "Approval could not be consumed: it changed, expired, or was already used"
            )
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.APPROVAL_CONSUMED,
                project_id=consumed.project_id,
                task_id=consumed.task_id,
                run_id=consumed.run_id,
                entity_type="approval",
                entity_id=consumed.id,
                actor_kind=ActorKind.SYSTEM,
                actor_name="approval_service",
                payload={"action": consumed.action.value},
            )
        )
        return consumed

    def checked_for_run(
        self,
        *,
        run: WorkflowRun,
        contract: TaskContract,
        plan: TaskPlan | None,
    ) -> dict[ApprovalAction, ApprovalRequest]:
        """Return approved approvals for every action this run requires."""
        result: dict[ApprovalAction, ApprovalRequest] = {}
        for action in contract.required_approvals:
            expected = (
                expected_write_sha(run=run, contract=contract, plan=plan)
                if action == ApprovalAction.WRITE_ACCESS
                else expected_command_sha(run=run, contract=contract)
            )
            candidates = [
                approval
                for approval in self.approvals.list(run_id=run.id, status=ApprovalStatus.APPROVED)
                if approval.action == action and approval.spec_sha256 == expected
            ]
            if not candidates:
                pending = self.approvals.find_pending(run.id, action.value)
                if pending:
                    raise ApprovalError(
                        f"{action.value} approval is still pending: {pending[0].id}",
                        hint=f"Run `maya task approve {pending[0].id}`.",
                    )
                raise ApprovalError(
                    f"No approved {action.value} approval matches the current contract and plan",
                    hint="Create a new run and approve its request.",
                )
            result[action] = max(candidates, key=lambda item: item.decided_at or item.requested_at)
        return result

    # ------------------------------------------------------------------ expire
    def expire_pending(self) -> list[str]:
        expiring = self.approvals.list(status=ApprovalStatus.PENDING, limit=10_000)
        ids: list[str] = []
        now = to_iso(utcnow())
        for approval in expiring:
            if to_iso(approval.expires_at) > now:
                continue
            if self.approvals.expire_one(approval.id, now_iso=now):
                ids.append(approval.id)
                self.events.append(
                    WorkflowEvent(
                        event_id="evt_" + new_ulid(),
                        event_type=EventType.APPROVAL_EXPIRED,
                        project_id=approval.project_id,
                        task_id=approval.task_id,
                        run_id=approval.run_id,
                        entity_type="approval",
                        entity_id=approval.id,
                        actor_kind=ActorKind.SYSTEM,
                        actor_name="approval_service",
                        payload={"action": approval.action.value},
                    )
                )
        return ids

    def list_for_run(self, run_id: str) -> list[ApprovalRequest]:
        self.expire_pending()
        return self.approvals.list(run_id=run_id)

    def _pending_or_expired(self, approval_id: str) -> ApprovalRequest:
        approval = self.approvals.get(approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise ApprovalError(
                f"Approval {approval_id} is already {approval.status.value}"
            )
        if to_iso(approval.expires_at) <= to_iso(utcnow()):
            self.approvals.expire(now_iso=to_iso(utcnow()))
            raise ApprovalError(f"Approval {approval_id} has expired")
        return approval


def _safe_scope(spec: dict[str, Any]) -> dict[str, Any]:
    """Approval events never include command argv or file content."""
    scope = dict(spec)
    scope.pop("plan_sha256", None)
    return scope


__all__ = [
    "ApprovalService",
    "write_spec",
    "command_spec",
    "expected_write_sha",
    "expected_command_sha",
    "canonical_sha256",
    "DEFAULT_TTL_SECONDS",
]

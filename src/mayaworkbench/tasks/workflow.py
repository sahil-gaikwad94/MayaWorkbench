"""The only component allowed to mutate workflow state.

Every transition is checked against an explicit graph, applied with a
compare-and-set update, and persisted together with an append-only event.
Terminal states cannot be reopened: a retry is a new run linked to the same
task and contract. A model response can never call this service directly; the
agent returns a proposed action, and application code decides whether a state
change follows.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import ActorKind, EventType, WorkflowState
from mayaworkbench.domain.models import WorkflowEvent, WorkflowRun
from mayaworkbench.errors import TaskError
from mayaworkbench.ids import new_ulid
from mayaworkbench.storage.stores import EventStore, RunStore

ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset(
        {WorkflowState.CONTRACTED, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.CONTRACTED: frozenset(
        {WorkflowState.PLANNING, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.PLANNING: frozenset(
        {
            WorkflowState.AWAITING_APPROVAL,
            WorkflowState.FAILED,
            WorkflowState.BLOCKED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.AWAITING_APPROVAL: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.BLOCKED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.IMPLEMENTING: frozenset(
        {
            WorkflowState.REVIEWING,
            WorkflowState.PAUSED,
            WorkflowState.BLOCKED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.PAUSED: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.REVIEWING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.REVIEWING: frozenset(
        {
            WorkflowState.COMPLETED,
            WorkflowState.COMPLETED_WITH_LIMITATIONS,
            WorkflowState.NEEDS_HUMAN_DECISION,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
}


class WorkflowService:
    def __init__(self, runs: RunStore, events: EventStore):
        self.runs = runs
        self.events = events

    def transition(
        self,
        run_id: str,
        to_state: WorkflowState,
        *,
        reason: str = "",
        actor_kind: ActorKind = ActorKind.SYSTEM,
        actor_name: str = "workflow",
        payload: dict[str, Any] | None = None,
        started: bool = False,
        finished: bool = False,
        error: str | None = None,
    ) -> WorkflowRun:
        run = self.runs.get(run_id)
        if run.state.is_terminal:
            raise TaskError(
                f"Run is {run.state.value} and terminal; create a new run to continue",
                hint="Terminal states are intentionally not reopenable.",
            )
        allowed = ALLOWED_TRANSITIONS.get(run.state, frozenset())
        if to_state not in allowed:
            raise TaskError(
                f"Invalid workflow transition {run.state.value} -> {to_state.value}",
                hint=f"Allowed from {run.state.value}: "
                + (", ".join(sorted(state.value for state in allowed)) or "none"),
            )
        changed = self.runs.compare_and_set_state(
            run_id,
            expected=[run.state],
            new_state=to_state,
            started=started,
            finished=finished or to_state.is_terminal,
            error=error,
        )
        if not changed:
            current = self.runs.get(run_id)
            raise TaskError(
                f"Run state changed concurrently to {current.state.value}; retry",
                hint="Another process moved this run. Inspect `maya task status`.",
            )
        updated = self.runs.get(run_id)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.TASK_STATE_CHANGED,
                project_id=run.project_id,
                repository_id=run.repository_id,
                snapshot_id=run.snapshot_id,
                task_id=run.task_id,
                run_id=run.id,
                entity_type="run",
                entity_id=run.id,
                actor_kind=actor_kind,
                actor_name=actor_name,
                payload={
                    "from": run.state.value,
                    "to": to_state.value,
                    "reason": reason,
                    **(payload or {}),
                },
            )
        )
        return updated

    def pause(self, run_id: str) -> WorkflowRun:
        run = self.runs.get(run_id)
        if run.state.is_terminal:
            raise TaskError(f"Run is terminal: {run.state.value}")
        if run.state != WorkflowState.IMPLEMENTING:
            raise TaskError(
                f"Run is {run.state.value}; pause is only meaningful while IMPLEMENTING"
            )
        self.runs.set_flags(run_id, pause_requested=True)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.TASK_PAUSED,
                project_id=run.project_id,
                task_id=run.task_id,
                run_id=run.id,
                snapshot_id=run.snapshot_id,
                entity_type="run",
                entity_id=run.id,
                actor_kind=ActorKind.USER,
                actor_name="local-user",
                payload={"requested": True},
            )
        )
        return self.runs.get(run_id)

    def resume(self, run_id: str) -> WorkflowRun:
        run = self.runs.get(run_id)
        if run.state != WorkflowState.PAUSED:
            raise TaskError(f"Run is {run.state.value}; only a PAUSED run can resume")
        self.runs.set_flags(run_id, pause_requested=False, cancel_requested=False)
        updated = self.transition(
            run_id,
            WorkflowState.IMPLEMENTING,
            reason="user resumed the run",
            actor_kind=ActorKind.USER,
            actor_name="local-user",
            started=True,
        )
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.TASK_RESUMED,
                project_id=run.project_id,
                task_id=run.task_id,
                run_id=run.id,
                snapshot_id=run.snapshot_id,
                entity_type="run",
                entity_id=run.id,
                actor_kind=ActorKind.USER,
                actor_name="local-user",
                payload={},
            )
        )
        return updated

    def cancel(self, run_id: str, *, reason: str) -> WorkflowRun:
        run = self.runs.get(run_id)
        if run.state.is_terminal:
            raise TaskError(f"Run is already terminal: {run.state.value}")
        self.runs.set_flags(run_id, cancel_requested=True)
        updated = self.transition(
            run_id,
            WorkflowState.CANCELLED,
            reason=reason,
            actor_kind=ActorKind.USER,
            actor_name="local-user",
            finished=True,
            error=reason,
        )
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.TASK_CANCELLED,
                project_id=run.project_id,
                task_id=run.task_id,
                run_id=run.id,
                snapshot_id=run.snapshot_id,
                entity_type="run",
                entity_id=run.id,
                actor_kind=ActorKind.USER,
                actor_name="local-user",
                payload={"reason": reason, "at": utcnow().isoformat()},
            )
        )
        return updated


__all__ = ["WorkflowService", "ALLOWED_TRANSITIONS"]

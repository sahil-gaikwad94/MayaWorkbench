"""Separate local worker process.

The CLI never executes repository code in its own process. It enqueues a
durable job and launches this module. The worker claims one job with a lease,
runs the bounded agent, records the patch and state, and exits. A crashed worker
leaves a lease that another worker can reclaim after it expires; a cancelled run
is observed through the database and kills its active command process group.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from mayaworkbench.app.container import Services
from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ApprovalAction,
    ApprovalStatus,
    EventType,
    WorkflowState,
    WorkerJobStatus,
    WorkspaceStatus,
)
from mayaworkbench.domain.models import WorkerJob, WorkflowEvent
from mayaworkbench.errors import MayaError, PolicyError
from mayaworkbench.ids import new_ulid
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.tasks.agent import AgentOutcome, ControlledAgent
from mayaworkbench.tasks.approvals import expected_command_sha, expected_write_sha
from mayaworkbench.tasks.inloop import DeclaredCheckRunner
from mayaworkbench.tasks.orientation import build_orientation
from mayaworkbench.tasks.orchestrator import RoleOrchestrator, role_plan_enabled
from mayaworkbench.tasks.roles import build_role_plan
from mayaworkbench.workspace.policy import PathPolicy
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
from mayaworkbench.workspace.tools import ToolGateway

log = get_logger("worker")


class LeaseLost(RuntimeError):
    """Another worker reclaimed this job."""


class Worker:
    def __init__(self, services: Services):
        self.services = services
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"

    # ------------------------------------------------------------------- queue
    def run(self, *, once: bool = True, run_id: str | None = None) -> int:
        processed = 0
        # Do not reclaim a job that keeps crashing its worker forever. Reaping
        # happens before claiming so an exhausted run settles instead of
        # staying IMPLEMENTING with no live worker.
        for exhausted in self.services.jobs.reap_exhausted():
            log.error(
                "worker job exhausted its attempts",
                extra={"job_id": exhausted.id, "run_id": exhausted.run_id},
            )
            self._fail_run(
                exhausted,
                f"worker job {exhausted.id} failed {exhausted.attempts} attempts; the run was failed",
            )
        while True:
            job = self.services.jobs.claim(
                owner=self.owner, lease_seconds=600, run_id=run_id
            )
            if job is None:
                if once:
                    return 0
                time.sleep(1.0)
                continue
            processed += 1
            self._event(
                job,
                EventType.WORKER_LEASE_ACQUIRED,
                {"owner": self.owner, "attempt": job.attempts},
            )
            try:
                self._execute(job)
            except LeaseLost as exc:
                log.warning("worker lease lost", extra={"job_id": job.id, "error": str(exc)})
                self._event(job, EventType.WORKER_LEASE_LOST, {"owner": self.owner})
            except Exception as exc:  # noqa: BLE001 - worker boundary
                log.exception("worker job crashed", extra={"job_id": job.id})
                detail = f"{type(exc).__name__}: {exc}"
                self._fail_run(job, detail)
                self.services.jobs.finish(
                    job.id, status=WorkerJobStatus.FAILED, error=detail[:4000]
                )
                self._event(job, EventType.WORKER_FINISHED, {"status": "FAILED", "error": detail})
            if run_id is not None:
                return 0
            if once:
                return 0

    # --------------------------------------------------------------- execution
    def _execute(self, job: WorkerJob) -> None:
        run = self.services.runs.get(job.run_id)
        if run.state.is_terminal:
            self.services.jobs.finish(
                job.id,
                status=WorkerJobStatus.CANCELLED,
                error=f"run already {run.state.value}",
            )
            return
        if run.state == WorkflowState.PAUSED:
            self.services.jobs.finish(job.id, status=WorkerJobStatus.DONE)
            return
        if run.state != WorkflowState.IMPLEMENTING:
            self.services.jobs.finish(
                job.id,
                status=WorkerJobStatus.FAILED,
                error=f"run is {run.state.value}, not IMPLEMENTING",
            )
            return
        task = self.services.tasks.get(run.task_id)
        contract = self.services.contracts.get(run.contract_id)
        plan = (
            self.services.plans.get(run.plan_id)
            if run.plan_id
            else self.services.plans.latest_for_run(run.id)
        )
        workspace = self.services.workspaces.get_by_run(run.id)
        if workspace is None or workspace.status == WorkspaceStatus.REMOVED:
            self._fail_run(job, "the execution workspace is missing")
            raise MayaError("Workspace is missing")
        consumed = self.services.approvals.list(
            run_id=run.id, status=ApprovalStatus.CONSUMED
        )
        write_approval = next(
            (
                item
                for item in consumed
                if item.action == ApprovalAction.WRITE_ACCESS
                and item.spec_sha256
                == expected_write_sha(run=run, contract=contract, plan=plan)
            ),
            None,
        )
        command_approval = next(
            (
                item
                for item in consumed
                if item.action == ApprovalAction.COMMAND_EXECUTION
                and item.spec_sha256 == expected_command_sha(run=run, contract=contract)
            ),
            None,
        )
        if write_approval is None:
            self._fail_run(job, "the consumed write approval no longer matches the plan")
            self.services.jobs.finish(
                job.id, status=WorkerJobStatus.FAILED, error="write approval mismatch"
            )
            return
        if (
            ApprovalAction.COMMAND_EXECUTION in contract.required_approvals
            and command_approval is None
        ):
            self._fail_run(job, "the consumed command approval no longer matches the policy")
            self.services.jobs.finish(
                job.id, status=WorkerJobStatus.FAILED, error="command approval mismatch"
            )
            return

        lease_seconds = max(600, min(7200, contract.constraints.max_runtime_seconds + 300))
        last_beat = [0.0]
        last_cancel_check = [0.0]
        cancel_state = [False]

        def heartbeat() -> bool:
            now = time.monotonic()
            if now - last_beat[0] >= 10:
                if not self.services.jobs.heartbeat(job.id, owner=self.owner, lease_seconds=lease_seconds):
                    raise LeaseLost(f"job {job.id} lease was reclaimed")
                last_beat[0] = now
            return True

        def cancel_requested() -> bool:
            now = time.monotonic()
            if now - last_cancel_check[0] >= 1:
                cancel_state[0] = self.services.runs.get(run.id).cancel_requested
                last_cancel_check[0] = now
                heartbeat()
            return cancel_state[0]

        from dataclasses import replace

        from mayaworkbench.domain.enums import ExecutionTrust
        from mayaworkbench.workspace.backends import (
            BackendCommandRunner,
            RootlessContainerBackend,
        )
        from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace
        from mayaworkbench.workspace.task_runtime import CommandRunnerTaskRuntime

        contract_policy = CommandPolicy.from_constraints(contract.constraints)
        trust = contract.constraints.execution_trust
        run_home = Path(workspace.path).parent / "home"
        container_backend = None
        capability_evidence = None
        if trust == ExecutionTrust.ROOTLESS_CONTAINER.value:
            container_backend = RootlessContainerBackend(
                Path(workspace.path),
                run_home,
                replace(contract_policy, trust_profile=ExecutionTrust.LOCAL_TRUSTED.value),
            )
            # Verify THIS task's limits in a task-equivalent container before
            # anything runs. A smoke test that only proves "a container starts"
            # is not evidence for pids/memory/cpu, so the probe reads the
            # effective values back from inside the container. Failure blocks
            # the run; it never downgrades to host execution.
            capability_evidence = container_backend.verify_capabilities()
            if not capability_evidence.available:
                raise PolicyError(
                    "ROOTLESS_CONTAINER cannot be established: "
                    + "; ".join(capability_evidence.mandatory_failures)
                )
            # Provision once. The sanitized snapshot and its overlay are the
            # single file view used by all tools, commands, in-loop checks and
            # artifact collection.
            runtime_workspace = RuntimeWorkspace.for_container(
                container_backend, Path(workspace.path), snapshot_id=run.snapshot_id
            )
            runner = BackendCommandRunner(container_backend, runtime=runtime_workspace.runtime)
        elif trust in {
            ExecutionTrust.LOCAL_TRUSTED.value,
            ExecutionTrust.READ_ONLY_ANALYSIS.value,
        }:
            runtime_workspace = RuntimeWorkspace.for_host(Path(workspace.path))
            runner = CommandRunner(
                runtime_workspace.path,
                policy=contract_policy,
                run_home=run_home,
                # Opt into a verified cgroup v2 pids scope when this host
                # delegates one; otherwise process_count stays unsupported.
                use_cgroup_scope=trust == ExecutionTrust.LOCAL_TRUSTED.value,
            )
        else:
            raise PolicyError(  # fail closed; never downgrade an isolated request
                f"execution trust profile {trust!r} is not available to the worker"
            )

        # Every component receives the SAME runtime: no hidden runners.
        task_runtime = CommandRunnerTaskRuntime(
            workspace=runtime_workspace, runner=runner, trust_profile=trust
        )
        active_path = runtime_workspace.path
        policy = PathPolicy(
            active_path,
            allowed_paths=contract.scope.allowed_paths,
            excluded_paths=contract.scope.excluded_paths,
            allow_file_deletion=contract.constraints.allow_file_deletion,
        )
        try:
            binding = {
                **run.details,
                "runtime_binding": {
                    **runtime_workspace.describe(),
                    "runtime_ref": runtime_workspace.runtime.as_dict()
                    if runtime_workspace.runtime
                    else None,
                    "trust_profile": trust,
                    "capability_evidence": capability_evidence.as_dict()
                    if capability_evidence is not None
                    else None,
                },
            }
            self.services.runs.update_progress(run.id, details=binding)
        except Exception as exc:  # noqa: BLE001 - evidence must not break execution
            log.warning("runtime binding could not be persisted", extra={"error": str(exc)})

        # In-loop checks are declared in the contract, not proposed by the model.
        # They may run only when the same COMMAND_EXECUTION approval that governs
        # the gateway is present; otherwise the gate records BLOCKED, honestly.
        checks = DeclaredCheckRunner(
            contract=contract,
            workspace=active_path,
            run_home=Path(workspace.path).parent / "check-home",
            authorized=(
                command_approval is not None
                and ApprovalAction.COMMAND_EXECUTION in contract.required_approvals
            ),
            # The same runtime the implementer used. A rootless task therefore
            # runs its declared checks inside its own container; there is no
            # hidden host runner and no silent trust downgrade.
            runtime=task_runtime,
        )
        try:
            orientation = build_orientation(
                retrieval=self.services.retrieval,
                files=self.services.files,
                symbols=self.services.symbols,
                issues=self.services.issues,
                snapshot_id=run.snapshot_id,
                goal=task.goal,
                allowed_paths=contract.scope.allowed_paths,
                excluded_paths=contract.scope.excluded_paths,
            )
        except Exception as exc:  # noqa: BLE001 - orientation is an aid, never a gate
            log.warning("orientation unavailable", extra={"run_id": run.id, "error": str(exc)})
            orientation = {
                "kind": "repository_orientation",
                "available": False,
                "error": f"{type(exc).__name__}: {exc}"[:400],
            }
        # Closed-loop experience: confirmed, non-expired prior negative results
        # are ADVISORY hints during planning/repair, never prohibitions. This
        # connects memory/experience.py to the live task loop (audit FIX-05).
        try:
            from mayaworkbench.memory.experience import suggest_prior_attempts

            orientation["advisory_prior_failures"] = suggest_prior_attempts(
                self.services.memory,
                project_id=run.project_id,
                goal=task.goal,
                limit=5,
            )
        except Exception as exc:  # noqa: BLE001 - memory is advisory, never a gate
            log.warning("prior-attempt recall unavailable", extra={"error": str(exc)})
            orientation["advisory_prior_failures"] = []

        def symbol_lookup(query: str, limit: int) -> list[dict[str, Any]]:
            hits = self.services.search.search_symbols(run.snapshot_id, query, limit=limit)
            return [hit.model_dump(mode="json") for hit in hits]

        from mayaworkbench.workspace.sessions import (
            SessionManager,
            UnavailableSessionBackend,
        )
        from mayaworkbench.workspace.tools import redact as redact_tool_output

        if trust == ExecutionTrust.ROOTLESS_CONTAINER.value and container_backend is not None:
            # The session process lives inside the task container: the host runs
            # only the OCI client. If the retained container cannot start, the
            # backend reports the capability unavailable; there is no host
            # fallback either way.
            from mayaworkbench.workspace.container_sessions import ContainerSessionBackend

            container_runtime = runtime_workspace.runtime
            if container_runtime is None:  # pragma: no cover - provision() guarantees it
                raise PolicyError(
                    "ROOTLESS_CONTAINER has no provisioned runtime to own a session"
                )
            sessions = SessionManager(
                active_path,
                runner,
                redactor=redact_tool_output,
                backend=ContainerSessionBackend(
                    container_backend,
                    container_runtime,
                    workspace=active_path,
                    policy=contract_policy,
                ),
            )
        elif trust == ExecutionTrust.ROOTLESS_CONTAINER.value:  # pragma: no cover - defensive
            sessions = SessionManager(
                active_path,
                runner,
                redactor=redact_tool_output,
                backend=UnavailableSessionBackend(
                    "no container backend was provisioned for this isolated task; "
                    "no session was started on the host"
                ),
            )
        else:
            sessions = SessionManager(
                active_path,
                runner,
                redactor=redact_tool_output,
            )

        gateway = ToolGateway(
            run=run,
            task=task,
            contract=contract,
            workspace=active_path,
            policy=policy,
            runner=runner,
            calls=self.services.calls,
            artifacts=self.services.artifacts,
            events=self.services.events,
            write_approval=write_approval,
            expected_write_spec=expected_write_sha(run=run, contract=contract, plan=plan),
            command_approval=command_approval,
            expected_command_spec=expected_command_sha(run=run, contract=contract),
            cancel_requested=cancel_requested,
            check_runner=checks,
            session_manager=sessions,
            read_only_profile=(trust == ExecutionTrust.READ_ONLY_ANALYSIS.value),
            allowed_api_hosts=tuple(
                host.strip()
                for host in os.environ.get("MAYA_API_HOSTS", "").split(",")
                if host.strip()
            ),
            allowed_db_dirs=tuple(
                path.strip()
                for path in os.environ.get("MAYA_DB_DIRS", "").split(",")
                if path.strip()
            ),
            allowed_log_dirs=tuple(
                path.strip()
                for path in os.environ.get("MAYA_LOG_DIRS", "").split(",")
                if path.strip()
            ),
            symbol_lookup=symbol_lookup,
        )
        agent = ControlledAgent(
            settings=self.services.settings,
            gateway=self.services.gateway,
            runs=self.services.runs,
            calls=self.services.calls,
            artifacts=self.services.artifacts,
            run=self.services.runs.get(run.id),
            task=task,
            contract=contract,
            plan=plan,
            tool_gateway=gateway,
            heartbeat=heartbeat,
            orientation=orientation,
            events=self.services.events,
            prior_attempts=self._prior_attempts(task),
        )
        heartbeat()
        try:
            if role_plan_enabled(run):
                # WP8: the single deterministic orchestrator walks the typed role
                # DAG. The old single-role path below is unchanged for runs
                # without the use_role_plan detail flag.
                role_plan = build_role_plan(task, contract, orientation)
                orchestrator = RoleOrchestrator(
                    runs=self.services.runs,
                    artifacts=self.services.artifacts,
                    events=self.services.events,
                    workflow=self.services.workflow,
                    plan=role_plan,
                    task=task,
                    contract=contract,
                    run=self.services.runs.get(run.id),
                    orientation=orientation,
                    implementation_plan=plan,
                    tool_gateway=gateway,
                    implementer=lambda: agent.run_loop(),
                    patch_provider=lambda: self.services.task_service.capture_diff(run.id),
                )
                outcome = orchestrator.run()
            else:
                outcome = agent.run_loop()
        finally:
            # No session may outlive the run: stop every process group this
            # worker created before capturing the patch or settling state.
            sessions.stop_all(reason="run finished")
            # A container overlay is retained until verification has compared
            # the exact candidate tree; only the container/process is stopped.
            task_runtime.stop(
                reason="run finished",
                keep_overlay=(trust == ExecutionTrust.ROOTLESS_CONTAINER.value),
            )
        self._settle(job, outcome)

    def _prior_attempts(self, task: Any) -> list[dict[str, Any]]:
        """Recall confirmed prior failures that resemble this task, best-first.

        Advisory only: the payload tells the implementer which approaches already
        failed, never that an approach is forbidden. Unconfirmed attempts are
        excluded by ``suggest_prior_attempts`` itself, so a run's own speculation
        cannot come back as evidence against it.
        """
        try:
            suggestions = self.services.memory.suggest_prior_attempts(
                project_id=task.project_id, goal=task.goal, limit=5
            )
        except Exception as exc:  # noqa: BLE001 - recall must never block a run
            log.warning("prior attempt recall unavailable", extra={"error": str(exc)})
            return []
        recalled: list[dict[str, Any]] = []
        for item in suggestions:
            recalled.append(
                {
                    "memory_id": item.get("memory_id"),
                    "similarity": item.get("score"),
                    "goal": item.get("goal", ""),
                    "hypothesis": item.get("hypothesis", ""),
                    "base_commit": item.get("base_commit"),
                }
            )
        return recalled

    def _import_container_diff(self, run: Any) -> list[dict[str, str]]:
        """Bring the exact container overlay contents into the Git worktree.

        A container overlay is not a Git repository, so the durable patch is
        captured after importing the content-aware diff through a path-checked
        boundary. This is what makes "the patch Maya produced" the same tree
        that checks were run against.
        """
        binding = (run.details or {}).get("runtime_binding")
        if not binding or binding.get("kind") != "container":
            return []
        from mayaworkbench.workspace.runtime_workspace import (
            RuntimeWorkspace,
            import_diff_to_worktree,
            manifest,
        )

        base = Path(binding["base_path"])
        workspace = RuntimeWorkspace(
            "container",
            base,
            Path(binding["active_path"]),
            manifest(base),
        )
        worktree = self.services.workspaces.get_by_run(run.id)
        if worktree is None:
            raise MayaError("the Git worktree is gone; the candidate patch cannot be captured")
        changes = import_diff_to_worktree(workspace, Path(worktree.path))
        self.services.runs.update_progress(
            run.id, details={**run.details, "runtime_content_diff": changes}
        )
        return changes

    def _settle(self, job: WorkerJob, outcome: AgentOutcome) -> None:
        # A stalled worker may resume after its lease expired and another worker
        # took over. Settlement must not let the stale worker write run state.
        if not self.services.jobs.heartbeat(job.id, owner=self.owner, lease_seconds=600):
            raise LeaseLost(f"job {job.id} lease was reclaimed before settlement")
        run = self.services.runs.get(job.run_id)
        self.services.runs.update_progress(
            run.id,
            step_count=outcome.steps,
            tool_call_count=outcome.tool_calls,
            finish_summary=outcome.summary or None,
            limitations=outcome.limitations,
            details={
                **run.details,
                "agent_outcome": outcome.state.value,
                "agent_error": outcome.error,
                "worker_owner": self.owner,
            },
        )
        if outcome.state == WorkflowState.REVIEWING:
            try:
                self._import_container_diff(run)
                self.services.task_service.capture_diff(run.id)
            except Exception as exc:  # noqa: BLE001
                self.services.runs.update_progress(
                    run.id,
                    details={**self.services.runs.get(run.id).details, "diff_error": str(exc)},
                )
                self._transition_or_fail(
                    run.id, WorkflowState.FAILED, f"could not capture the candidate patch: {exc}"
                )
                self.services.jobs.finish(
                    job.id, status=WorkerJobStatus.FAILED, error=str(exc)[:4000]
                )
                return
            self._transition_or_fail(
                run.id, WorkflowState.REVIEWING, "agent finished; candidate patch captured"
            )
            self.services.jobs.finish(job.id, status=WorkerJobStatus.DONE)
            self._event(job, EventType.WORKER_FINISHED, {"status": "DONE", "state": "REVIEWING"})
            return
        if outcome.state == WorkflowState.PAUSED:
            self._transition_or_fail(run.id, WorkflowState.PAUSED, "pause requested")
            self.services.jobs.finish(job.id, status=WorkerJobStatus.DONE)
            self._event(job, EventType.WORKER_FINISHED, {"status": "DONE", "state": "PAUSED"})
            return
        if outcome.state == WorkflowState.CANCELLED:
            try:
                self._import_container_diff(run)
                self.services.task_service.capture_diff(run.id)
            except Exception as exc:  # noqa: BLE001 - partial evidence is best effort
                self.services.runs.update_progress(
                    run.id,
                    details={**self.services.runs.get(run.id).details, "diff_error": str(exc)},
                )
            if not self.services.runs.get(run.id).state.is_terminal:
                self._transition_or_fail(run.id, WorkflowState.CANCELLED, "cancelled by user")
            self.services.jobs.finish(job.id, status=WorkerJobStatus.CANCELLED, error="cancelled")
            self._event(job, EventType.WORKER_FINISHED, {"status": "CANCELLED"})
            return
        target = (
            WorkflowState.BLOCKED
            if outcome.state == WorkflowState.BLOCKED
            else WorkflowState.FAILED
        )
        try:
            self._import_container_diff(run)
            self.services.task_service.capture_diff(run.id)
        except Exception as exc:  # noqa: BLE001 - preserve partial evidence
            self.services.runs.update_progress(
                run.id,
                details={**self.services.runs.get(run.id).details, "diff_error": str(exc)},
            )
        self._record_failed_attempt(run, outcome, target)
        self._transition_or_fail(run.id, target, outcome.error or outcome.summary)
        self.services.jobs.finish(
            job.id,
            status=WorkerJobStatus.DONE if target == WorkflowState.BLOCKED else WorkerJobStatus.FAILED,
            error=outcome.error,
        )
        self._event(job, EventType.WORKER_FINISHED, {"status": target.value})

    def _record_failed_attempt(self, run: Any, outcome: Any, target: Any) -> None:
        """Record an unconfirmed negative-result memory for a failed hypothesis."""
        if target.value not in {"FAILED", "BLOCKED"}:
            return
        try:
            from mayaworkbench.memory.experience import record_attempt

            task = self.services.tasks.get(run.task_id)
            contract = self.services.contracts.get(run.contract_id)
            workspace = self.services.workspaces.get_by_run(run.id)
            diff = self.services.diffs.get_by_run(run.id)
            base_commit = workspace.base_commit_sha if workspace else None
            if base_commit is not None and len(base_commit) != 40:
                base_commit = None
            record_attempt(
                self.services.memory,
                project_id=run.project_id,
                goal=task.goal,
                hypothesis=outcome.summary or "implement the stated objective",
                outcome=outcome.error or target.value,
                failure_signature=outcome.error or outcome.summary or target.value,
                base_commit=base_commit,
                patch_hash=getattr(diff, "patch_sha256", None),
                environment=(contract.constraints.execution_trust if contract else ""),
            )
        except Exception as exc:  # noqa: BLE001 - memory recording is advisory
            log.warning("failed attempt was not recorded", extra={"error": str(exc)})

    # ----------------------------------------------------------------- helpers
    def _transition_or_fail(self, run_id: str, target: WorkflowState, reason: str) -> None:
        run = self.services.runs.get(run_id)
        if run.state.is_terminal or run.state == target:
            return
        try:
            self.services.workflow.transition(
                run_id,
                target,
                reason=reason,
                error=reason if target in {WorkflowState.FAILED, WorkflowState.BLOCKED} else None,
                finished=True,
            )
        except MayaError:
            current = self.services.runs.get(run_id)
            if not current.state.is_terminal:
                self.services.runs.update_progress(
                    run_id,
                    error=reason[:4000],
                    details={**current.details, "state_transition_error": reason},
                )

    def _fail_run(self, job: WorkerJob, reason: str) -> None:
        run = self.services.runs.get(job.run_id)
        if run.state.is_terminal:
            return
        try:
            self.services.workflow.transition(
                run.id,
                WorkflowState.FAILED,
                reason=reason,
                error=reason,
                finished=True,
            )
        except MayaError:
            pass
        self.services.runs.update_progress(
            run.id,
            error=reason[:4000],
            details={**self.services.runs.get(run.id).details, "worker_error": reason},
        )
        self.services.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.TASK_FAILED,
                project_id=run.project_id,
                repository_id=run.repository_id,
                snapshot_id=run.snapshot_id,
                task_id=run.task_id,
                run_id=run.id,
                entity_type="run",
                entity_id=run.id,
                payload={"reason": reason[:4000]},
            )
        )

    def _event(self, job: WorkerJob, event_type: EventType, payload: dict[str, Any]) -> None:
        run = self.services.runs.get(job.run_id)
        self.services.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=run.project_id,
                repository_id=run.repository_id,
                snapshot_id=run.snapshot_id,
                task_id=run.task_id,
                run_id=run.id,
                entity_type="worker_job",
                entity_id=job.id,
                actor_kind=ActorKind.SYSTEM,
                actor_name="worker",
                payload=payload,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mayaworkbench.worker")
    parser.add_argument("--home", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    services = Services.open(args.home)
    try:
        worker = Worker(services)
        return worker.run(once=args.once, run_id=args.run_id)
    finally:
        services.db.close()


__all__ = ["Worker", "main", "LeaseLost"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

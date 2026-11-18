"""Concurrency and crash-consistency guarantees for task execution.

These tests use two independent ``Services`` instances on one home directory
(two SQLite connections, like two CLI processes) rather than threads sharing a
connection, so they exercise the same interleaving the CLI and worker see.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from mayaworkbench.app.container import Services
from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.domain.enums import TaskType, WorkerJobStatus, WorkflowState
from mayaworkbench.domain.models import WorkerJob
from mayaworkbench.errors import ConflictError


def make_task(services: Services, indexed) -> tuple[object, object]:
    return services.task_service.create_task(
        project_id=indexed.project.id,
        title="Concurrency",
        goal="Reserve exactly one run and one job",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        constraints={"max_steps": 3, "max_tool_calls": 5},
    )


def run_parallel(attempts) -> None:  # type: ignore[no-untyped-def]
    barrier = threading.Barrier(len(attempts))
    errors: list[BaseException] = []

    def call(attempt) -> None:  # type: ignore[no-untyped-def]
        try:
            barrier.wait(timeout=10)
            attempt()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(attempt,)) for attempt in attempts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "a concurrent attempt hung"
    for error in errors:
        raise error


def test_concurrent_start_reserves_only_one_run(indexed, home: Path) -> None:
    services = indexed.services
    task, contract = make_task(services, indexed)
    peers = [Services.open(str(home)) for _ in range(2)]
    outcomes: list[str] = []
    lock = threading.Lock()

    try:

        def reserve(peer: Services) -> None:
            try:
                run = peer.task_service._reserve_run(
                    task=task,
                    contract=contract,
                    repository=indexed.repository,
                    snapshot=indexed.snapshot,
                )
                with lock:
                    outcomes.append(f"run:{run.id}")
            except ConflictError as exc:
                with lock:
                    outcomes.append("conflict")
                assert "active run" in str(exc)

        run_parallel([lambda peer=peer: reserve(peer) for peer in peers])
        runs = services.runs.list(task_id=task.id, limit=10)
        assert len(runs) == 1, f"expected exactly one run, found {len(runs)}"
        assert sum(1 for item in outcomes if item.startswith("run:")) == 1
        assert outcomes.count("conflict") >= 1
    finally:
        for peer in peers:
            peer.close()


def test_concurrent_enqueue_creates_one_job(indexed, home: Path) -> None:
    services = indexed.services
    task, contract = make_task(services, indexed)
    run = services.task_service._reserve_run(
        task=task,
        contract=contract,
        repository=indexed.repository,
        snapshot=indexed.snapshot,
    )
    peers = [Services.open(str(home)) for _ in range(2)]
    try:

        def enqueue(peer: Services, index: int) -> None:
            peer.jobs.enqueue(
                WorkerJob(
                    id=f"job_race{index}",
                    run_id=run.id,
                    task_id=task.id,
                    project_id=indexed.project.id,
                )
            )

        run_parallel(
            [lambda peer=peer, i=i: enqueue(peer, i) for i, peer in enumerate(peers)]
        )
        count = services.db.fetchone(
            "SELECT COUNT(*) AS n FROM worker_jobs WHERE run_id = ?", (run.id,)
        )["n"]
        assert count == 1, f"duplicate worker jobs were created: {count}"
    finally:
        for peer in peers:
            peer.close()


def test_claim_refuses_a_run_that_another_worker_holds(indexed, home: Path) -> None:
    services = indexed.services
    task, contract = make_task(services, indexed)
    run = services.task_service._reserve_run(
        task=task,
        contract=contract,
        repository=indexed.repository,
        snapshot=indexed.snapshot,
    )
    services.jobs.enqueue(
        WorkerJob(
            id="job_primary",
            run_id=run.id,
            task_id=task.id,
            project_id=indexed.project.id,
        )
    )
    first = services.jobs.claim(owner="worker-a", lease_seconds=120)
    assert first is not None

    peer = Services.open(str(home))
    try:
        # Simulate a duplicate row left by an older build or a racing enqueue.
        peer.db.execute(
            "INSERT INTO worker_jobs (id, run_id, task_id, project_id, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, 'QUEUED', ?)",
            (
                "job_duplicate",
                run.id,
                task.id,
                indexed.project.id,
                to_iso(utcnow()),
            ),
        )
        assert peer.jobs.claim(owner="worker-b", lease_seconds=120) is None

        # Once the first lease expires the run becomes claimable again.
        peer.db.execute(
            "UPDATE worker_jobs SET lease_expires_at = ? WHERE id = ?",
            (to_iso(utcnow() - timedelta(seconds=5)), first.id),
        )
        reclaimed = peer.jobs.claim(owner="worker-b", lease_seconds=120)
        assert reclaimed is not None
        assert reclaimed.run_id == run.id
    finally:
        peer.close()


def test_exhausted_jobs_are_failed_and_never_reclaimed(indexed) -> None:
    services = indexed.services
    task, contract = make_task(services, indexed)
    run = services.task_service._reserve_run(
        task=task,
        contract=contract,
        repository=indexed.repository,
        snapshot=indexed.snapshot,
    )
    services.db.execute(
        "INSERT INTO worker_jobs (id, run_id, task_id, project_id, status, attempts, "
        "lease_owner, lease_expires_at, enqueued_at) "
        "VALUES (?, ?, ?, ?, 'LEASED', ?, 'dead-worker', ?, ?)",
        (
            "job_crashloop",
            run.id,
            task.id,
            indexed.project.id,
            services.jobs.MAX_ATTEMPTS,
            to_iso(utcnow() - timedelta(seconds=5)),
            to_iso(utcnow() - timedelta(minutes=5)),
        ),
    )

    reaped = services.jobs.reap_exhausted()
    assert [job.id for job in reaped] == ["job_crashloop"]
    assert services.jobs.get("job_crashloop").status is WorkerJobStatus.FAILED
    assert services.jobs.claim(owner="worker-c", lease_seconds=120) is None


def test_state_change_rolls_back_when_the_event_cannot_be_written(
    indexed, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = indexed.services
    task, contract = make_task(services, indexed)
    run = services.task_service._reserve_run(
        task=task,
        contract=contract,
        repository=indexed.repository,
        snapshot=indexed.snapshot,
    )
    before = services.events.count()

    def broken_append(_event):  # type: ignore[no-untyped-def]
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(services.events, "append", broken_append)
    with pytest.raises(RuntimeError):
        services.workflow.transition(
            run.id, WorkflowState.PLANNING, reason="should not persist"
        )
    # The failed transition must leave both the state and the event log intact.
    assert services.runs.get(run.id).state is WorkflowState.CONTRACTED
    assert services.events.count() == before


def test_settle_refuses_a_job_whose_lease_was_reclaimed(indexed) -> None:
    """A stale worker must not write run state after another worker took over."""
    from mayaworkbench.tasks.agent import AgentOutcome
    from mayaworkbench.worker import LeaseLost, Worker

    services = indexed.services
    task, contract = make_task(services, indexed)
    run = services.task_service._reserve_run(
        task=task,
        contract=contract,
        repository=indexed.repository,
        snapshot=indexed.snapshot,
    )
    services.jobs.enqueue(
        WorkerJob(
            id="job_stale",
            run_id=run.id,
            task_id=task.id,
            project_id=indexed.project.id,
        )
    )
    claimed = services.jobs.claim(owner="worker-that-took-over", lease_seconds=600)
    assert claimed is not None

    worker = Worker(services)
    with pytest.raises(LeaseLost):
        worker._settle(claimed, AgentOutcome(state=WorkflowState.REVIEWING))
    assert services.runs.get(run.id).state is WorkflowState.CONTRACTED

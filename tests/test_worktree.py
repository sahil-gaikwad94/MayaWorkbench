from __future__ import annotations

import hashlib
from pathlib import Path

from mayaworkbench.domain.enums import TaskType, WorkflowState
from mayaworkbench.domain.models import WorkflowRun
from mayaworkbench.ids import new_ulid


def _make_run(services, indexed, *, allowed_paths, task_type=None):
    from mayaworkbench.domain.enums import TaskType

    task, contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Worktree test",
        goal="Verify isolated worktree behaviour",
        task_type=task_type or TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=allowed_paths,
        acceptance_criteria=["workspace matches the snapshot"],
    )
    run = WorkflowRun(
        id="run_" + new_ulid(),
        task_id=task.id,
        contract_id=contract.id,
        project_id=task.project_id,
        repository_id=contract.repository_id,
        snapshot_id=contract.snapshot_id,
        state=WorkflowState.IMPLEMENTING,
        base_commit_sha=contract.scope.base_commit_sha,
    )
    services.runs.create(run)
    return task, contract, run


def test_worktree_is_detached_and_original_checkout_untouched(indexed):
    services = indexed.services
    repo = indexed.repo
    before = (repo / "utils.py").read_bytes()
    task, contract, run = _make_run(services, indexed, allowed_paths=["utils.py"])
    repository = services.repositories.get(contract.repository_id)
    workspace = services.worktrees.create(
        run=run,
        task=task,
        contract=contract,
        repository=repository,
        snapshot=indexed.snapshot,
    )
    try:
        path = Path(workspace.path)
        assert path.is_dir()
        assert path != repo
        assert (path / "utils.py").read_bytes() == before
        assert (repo / "utils.py").read_bytes() == before

        (path / "utils.py").write_text("changed = True\n", encoding="utf-8")
        patch, files, warnings = services.worktrees.capture_diff(workspace)
        assert b"changed = True" in patch
        assert files and files[0].path == "utils.py"
        assert files[0].status == "modified"
        assert (repo / "utils.py").read_bytes() == before
    finally:
        removed = services.worktrees.remove(workspace)
        assert removed.status.value == "REMOVED"
        assert not Path(workspace.path).exists()
        assert (repo / "utils.py").read_bytes() == before


def test_worktree_reconstructs_worktree_snapshot_with_untracked_file(services, project, repo: Path):
    utils = repo / "utils.py"
    utils.write_text(utils.read_text() + "\nEXTRA = 1\n", encoding="utf-8")
    untracked = repo / "notes.txt"
    untracked.write_text("untracked evidence\n", encoding="utf-8")
    attached = services.repository_service.attach(project, repo)
    outcome = services.repository_service.create_snapshot(
        attached.repository, working_tree="include"
    )
    assert outcome.snapshot.includes_worktree_patch
    services.indexer.index(outcome.snapshot.id)
    task, contract = services.task_service.create_task(
        project_id=project.id,
        title="Worktree snapshot",
        goal="Reconstruct working-tree content",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=outcome.snapshot.id,
        allowed_paths=["utils.py", "notes.txt"],
        acceptance_criteria=["content matches"],
    )
    run = WorkflowRun(
        id="run_" + new_ulid(),
        task_id=task.id,
        contract_id=contract.id,
        project_id=task.project_id,
        repository_id=contract.repository_id,
        snapshot_id=contract.snapshot_id,
        state=WorkflowState.IMPLEMENTING,
        base_commit_sha=contract.scope.base_commit_sha,
    )
    services.runs.create(run)
    workspace = services.worktrees.create(
        run=run,
        task=task,
        contract=contract,
        repository=services.repositories.get(contract.repository_id),
        snapshot=outcome.snapshot,
    )
    try:
        assert "EXTRA = 1" in (Path(workspace.path) / "utils.py").read_text()
        assert (Path(workspace.path) / "notes.txt").read_text() == "untracked evidence\n"
        assert untracked.read_text() == "untracked evidence\n"
    finally:
        services.worktrees.remove(workspace)

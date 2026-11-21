"""Regression tests for defects found by the adversarial audit.

Two behaviours are pinned here:

1. a token embedded in a Git remote URL must not be copied into the workbench
   database or event log when a repository is attached;
2. a tool argument the schema rejects (for example a path containing ``..``) is
   a recoverable model mistake, not a reason to end the whole run.
"""

from __future__ import annotations

from pathlib import Path

from mayaworkbench.domain.enums import TaskType, WorkflowState
from mayaworkbench.worker import Worker
from tests.conftest import git, write
from tests.test_task_workflow import finish_action, start_and_approve

TOKEN = "ghp_AUDITREGRESSION0123456789abcdef"


def test_remote_url_credentials_are_not_stored(services, project, tmp_path: Path):
    repo = tmp_path / "creds-repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "audit@example.com")
    git(repo, "config", "user.name", "Audit")
    write(repo / "module.py", "VALUE = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(
        repo,
        "remote",
        "add",
        "origin",
        f"https://x-access-token:{TOKEN}@github.com/example/repo.git",
    )

    result = services.repository_service.attach(project, repo)
    assert result.created
    assert TOKEN not in (result.repository.remote_url or "")
    assert result.repository.remote_url == "https://github.com/example/repo.git"
    assert result.repository.details["remote_url_credentials_removed"] is True
    assert any("credentials" in warning for warning in result.warnings)

    payloads = services.db.fetchall("SELECT payload_json FROM workflow_events")
    assert all(TOKEN not in row[0] for row in payloads), "token leaked into the event log"

    database_path = Path(services.db.path)
    assert TOKEN.encode() not in database_path.read_bytes(), "token leaked into the database"
    wal = Path(str(database_path) + "-wal")
    if wal.exists():
        assert TOKEN.encode() not in wal.read_bytes(), "token leaked into the WAL"


def test_invalid_tool_arguments_are_recovered_by_the_loop(services, indexed, task_model):
    task, _ = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Recover from invalid arguments",
        goal="Make the smallest safe change",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["AC1"],
        verification_checks=[],
        constraints={
            "max_steps": 6,
            "max_tool_calls": 10,
            "max_model_tokens": 20_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
        },
    )
    invalid = {
        "thought": "Try to escape the workspace.",
        "tool": "write_file",
        "arguments": {"path": "../evil.py", "content": "x = 1\n"},
    }
    result = start_and_approve(
        services, task, task_model, actions=[invalid, finish_action("Done.")]
    )
    services.task_service.prepare_execution(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)

    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, run.error
    # The invalid call never reaches the gateway, so no tool-call row exists and
    # nothing was written outside the workspace.
    assert services.calls.list_by_run(run.id) == []
    assert not (indexed.repo.parent / "evil.py").exists()

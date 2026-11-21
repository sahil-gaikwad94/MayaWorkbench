from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ApprovalAction,
    ApprovalStatus,
    TaskType,
    ToolCallStatus,
    WorkflowState,
)
from mayaworkbench.domain.models import ApprovalRequest, WorkflowRun
from mayaworkbench.errors import ApprovalError, PolicyError
from mayaworkbench.ids import new_ulid
from mayaworkbench.workspace.policy import PathPolicy
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
from mayaworkbench.workspace.tools import ToolGateway


def build_gateway(
    services,
    indexed,
    tmp_path: Path,
    *,
    allow_delete=False,
    network_policy="ALLOW_WITH_APPROVAL",
    excluded_paths=(),
    symbol_lookup=None,
):
    task, contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Gateway test",
        goal="Exercise the typed tool gateway",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py", "src", "tests"],
        constraints={"allow_file_deletion": allow_delete, "max_output_bytes": 2000},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "utils.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / ".env").write_text("TOKEN=x\n", encoding="utf-8")
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
    spec = "a" * 64
    approvals = {}
    for action in (ApprovalAction.WRITE_ACCESS, ApprovalAction.COMMAND_EXECUTION):
        approvals[action] = ApprovalRequest(
            id="apr_" + new_ulid(),
            run_id=run.id,
            task_id=task.id,
            project_id=task.project_id,
            action=action,
            status=ApprovalStatus.CONSUMED,
            spec_sha256=spec,
            nonce="n" * 32,
            expires_at=utcnow() + timedelta(hours=1),
            consumed_at=utcnow(),
        )
    policy = PathPolicy(
        workspace,
        allowed_paths=["utils.py", "src", "tests"],
        excluded_paths=excluded_paths,
        allow_file_deletion=allow_delete,
    )
    runner = CommandRunner(
        workspace,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy=network_policy,
            dependency_policy="APPROVAL_REQUIRED",
            max_output_bytes=1000,
            max_runtime_seconds=10,
        ),
        run_home=tmp_path / "run-home",
    )
    gateway = ToolGateway(
        run=run,
        task=task,
        contract=contract,
        workspace=workspace,
        policy=policy,
        runner=runner,
        calls=services.calls,
        artifacts=services.artifacts,
        events=services.events,
        write_approval=approvals[ApprovalAction.WRITE_ACCESS],
        expected_write_spec=spec,
        command_approval=approvals[ApprovalAction.COMMAND_EXECUTION],
        expected_command_spec=spec,
        symbol_lookup=symbol_lookup,
    )
    return gateway, workspace, run, contract, task


def test_read_search_and_secret_refusal(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    (workspace / "src" / "a.py").write_text("alpha\nbeta\n", encoding="utf-8")
    result = gateway.invoke("read_file", {"path": "utils.py"})
    assert result.status == ToolCallStatus.SUCCEEDED
    assert result.output == "value = 1\n"
    result = gateway.invoke("search_text", {"query": "beta"})
    assert result.status == ToolCallStatus.SUCCEEDED
    assert "src/a.py:2" in result.output
    blocked = gateway.invoke("read_file", {"path": ".env"})
    assert blocked.status == ToolCallStatus.BLOCKED
    assert blocked.error
    traversal = gateway.invoke("read_file", {"path": "../outside"})
    assert traversal.status == ToolCallStatus.BLOCKED


def test_write_patch_overwrite_and_scope(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    written = gateway.invoke(
        "write_file", {"path": "src/new.py", "content": "x = 1\n"}
    )
    assert written.status == ToolCallStatus.SUCCEEDED
    assert (workspace / "src" / "new.py").read_text() == "x = 1\n"
    refused = gateway.invoke(
        "write_file", {"path": "src/new.py", "content": "x = 2\n"}
    )
    assert refused.status == ToolCallStatus.FAILED
    overwritten = gateway.invoke(
        "write_file", {"path": "src/new.py", "content": "x = 2\n", "overwrite": True}
    )
    assert overwritten.status == ToolCallStatus.SUCCEEDED
    secret = gateway.invoke(
        "write_file",
        {"path": "src/credentials.py", "content": "KEY = 'AKIAABCDEFGHIJKLMNOP'\n"},
    )
    assert secret.status in {ToolCallStatus.BLOCKED, ToolCallStatus.FAILED}
    outside = gateway.invoke(
        "write_file", {"path": "other.py", "content": "x\n"}
    )
    assert outside.status == ToolCallStatus.BLOCKED

    patch = (
        "--- a/src/new.py\n+++ b/src/new.py\n@@ -1,1 +1,1 @@\n-x = 2\n+x = 3\n"
    )
    applied = gateway.invoke("apply_patch", {"patch": patch})
    assert applied.status == ToolCallStatus.SUCCEEDED
    assert (workspace / "src" / "new.py").read_text() == "x = 3\n"
    stale = gateway.invoke("apply_patch", {"patch": patch})
    assert stale.status == ToolCallStatus.FAILED


def test_delete_requires_contract_permission(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    refused = gateway.invoke("delete_file", {"path": "utils.py"})
    assert refused.status == ToolCallStatus.BLOCKED
    assert (workspace / "utils.py").exists()
    gateway2, workspace2, *_ = build_gateway(
        services, indexed, tmp_path / "second", allow_delete=True
    )
    allowed = gateway2.invoke("delete_file", {"path": "utils.py"})
    assert allowed.status == ToolCallStatus.SUCCEEDED
    assert not (workspace2 / "utils.py").exists()


def test_command_allowlist_dependency_network_and_output_cap(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    refused = gateway.invoke(
        "run_command", {"argv": ["git", "status"], "timeout_seconds": 5}
    )
    assert refused.status == ToolCallStatus.FAILED
    dependency = gateway.invoke(
        "run_command",
        {"argv": ["python", "-m", "pip", "install", "x"], "timeout_seconds": 5},
    )
    assert dependency.status == ToolCallStatus.FAILED
    absolute = gateway.invoke(
        "run_command", {"argv": ["python", "/etc/passwd"], "timeout_seconds": 5}
    )
    assert absolute.status == ToolCallStatus.FAILED
    output = gateway.invoke(
        "run_command",
        {"argv": ["python", "-c", "print('x'*5000)"], "timeout_seconds": 5},
    )
    assert output.status == ToolCallStatus.SUCCEEDED
    assert output.data["output_truncated"] is True
    assert len(output.output) <= 2000


def test_command_timeout_and_cancel(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    timed = gateway.invoke(
        "run_command",
        {"argv": ["python", "-c", "import time; time.sleep(5)"], "timeout_seconds": 1},
    )
    assert timed.status == ToolCallStatus.TIMED_OUT
    assert timed.data["timed_out"] is True


def test_runner_rejects_missing_approval_and_bad_timeout(services, indexed, tmp_path):
    gateway, workspace, run, contract, task = build_gateway(services, indexed, tmp_path)
    bare = ToolGateway(
        run=run,
        task=task,
        contract=contract,
        workspace=workspace,
        policy=gateway.policy,
        runner=gateway.runner,
        calls=services.calls,
        artifacts=services.artifacts,
        events=services.events,
        write_approval=None,
        expected_write_spec="a" * 64,
        command_approval=None,
        expected_command_spec="a" * 64,
    )
    with pytest.raises(ApprovalError):
        bare.invoke("write_file", {"path": "utils.py", "content": "x\n", "overwrite": True})
    blocked = bare.invoke(
        "run_command", {"argv": ["python", "-c", "print(1)"], "timeout_seconds": 5}
    )
    assert blocked.status == ToolCallStatus.BLOCKED
    with pytest.raises(PolicyError):
        gateway.runner.validate(["python", "-c", "print(1)"], timeout_seconds=999)


# --------------------------------------------------------------- adversarial
def test_deny_by_default_refuses_when_isolation_is_unavailable(
    services, indexed, tmp_path
):
    """MWB-002: a deny policy must fail closed, not run with egress."""
    gateway, workspace, *_ = build_gateway(
        services, indexed, tmp_path, network_policy="DENY_BY_DEFAULT"
    )
    gateway.runner._network_state = (False, "simulated: no network namespace")
    marker = workspace / "network-ran.txt"
    result = gateway.invoke(
        "run_command",
        {
            "argv": [
                "python",
                "-c",
                "open('network-ran.txt', 'w').write('ran'); print('ran')",
            ],
            "timeout_seconds": 5,
        },
    )
    assert result.status == ToolCallStatus.BLOCKED
    assert result.data["blocked"] is True
    assert "DENY_BY_DEFAULT" in result.data["blocked_reason"]
    # The command must not have been started at all.
    assert not marker.exists()


def test_background_child_is_terminated_when_leader_exits(services, indexed, tmp_path):
    """MWB-003: a parent that exits must not leave descendants running."""
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    result = gateway.invoke(
        "run_command",
        {
            "argv": [
                "python",
                "-c",
                (
                    "import subprocess, sys; "
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(30)']); "
                    "open('child.pid', 'w').write(str(child.pid)); "
                    "print('parent exited')"
                ),
            ],
            "timeout_seconds": 10,
        },
    )
    assert result.status == ToolCallStatus.SUCCEEDED
    assert result.data["cleanup_required"] is True
    assert result.data["cleanup_succeeded"] is True
    import os

    pid = int((workspace / "child.pid").read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_large_output_is_truncated_but_the_command_succeeds(services, indexed, tmp_path):
    """MWB-009: output beyond the cap is truncated, not a kill signal."""
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    result = gateway.invoke(
        "run_command",
        {"argv": ["python", "-c", "print('y' * 50_000)"], "timeout_seconds": 10},
    )
    assert result.status == ToolCallStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.data["output_truncated"] is True
    assert len(result.output) < 10_000

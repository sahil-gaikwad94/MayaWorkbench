"""WP2: task contracts grant typed capabilities; the gateway is a closed allowlist."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mayaworkbench.domain.enums import ToolCallStatus
from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.workspace.runner import CommandPolicy

from .test_tool_gateway_runner import build_gateway


def test_capability_grants_are_validated_and_deduplicated():
    constraints = TaskConstraints(capabilities=["file.read", "file.read", "process.execute"])
    assert constraints.capabilities == ["file.read", "process.execute"]
    with pytest.raises(ValidationError, match="unknown tool capabilities"):
        TaskConstraints(capabilities=["shell.execute"])


def test_command_policy_inherits_the_contract_trust_profile():
    policy = CommandPolicy.from_constraints(
        TaskConstraints(execution_trust="ROOTLESS_CONTAINER")
    )
    assert policy.trust_profile == "ROOTLESS_CONTAINER"


def test_process_execute_capability_is_required_for_commands(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    gateway.contract.constraints.capabilities = ["file.read"]
    result = gateway.invoke(
        "run_command",
        {"argv": ["python", "-c", "print('should not run')"], "timeout_seconds": 5},
    )
    assert result.status == ToolCallStatus.BLOCKED
    assert "process.execute" in result.summary


def test_file_read_capability_allows_reads_but_not_writes(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    gateway.contract.constraints.capabilities = ["file.read"]
    read = gateway.invoke("read_file", {"path": "utils.py"})
    assert read.status == ToolCallStatus.SUCCEEDED
    write = gateway.invoke(
        "write_file", {"path": "utils.py", "content": "value = 2\n"}
    )
    assert write.status == ToolCallStatus.BLOCKED
    assert "file.write" in write.summary


def test_task_scope_can_reference_workspace_resources(services, indexed, tmp_path):
    from mayaworkbench.errors import TaskError

    workspace = services.workspace_service.create(indexed.project.id, "task-scope")
    resource = services.workspace_service.attach_resource(
        workspace.id, "DATASET", "train", "dataset://local/train", hash_sha256="a" * 64
    )
    _task, contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Scoped task",
        goal="Work with one dataset",
        task_type=__import__("mayaworkbench.domain.enums", fromlist=["TaskType"]).TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        workspace_id=workspace.id,
        resource_ids=[resource.id],
    )
    assert contract.scope.workspace_id == workspace.id
    assert contract.scope.resource_ids == [resource.id]
    try:
        services.task_service.create_task(
            project_id=indexed.project.id,
            title="Bad scope",
            goal="Unknown resource",
            task_type=__import__("mayaworkbench.domain.enums", fromlist=["TaskType"]).TaskType.ENGINEERING_CHANGE,
            snapshot_id=indexed.snapshot.id,
            allowed_paths=["utils.py"],
            workspace_id=workspace.id,
            resource_ids=["res_unknown"],
        )
    except TaskError:
        pass
    else:
        raise AssertionError("unknown resource must be refused")


def test_legacy_contract_without_grants_keeps_approval_policy(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    assert gateway.contract.constraints.capabilities == []
    result = gateway.invoke(
        "write_file", {"path": "utils.py", "content": "value = 3\n", "overwrite": True}
    )
    assert result.status == ToolCallStatus.SUCCEEDED, result.error
    assert (workspace / "utils.py").read_text() == "value = 3\n"

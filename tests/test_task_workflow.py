from __future__ import annotations

from pathlib import Path

import pytest

from mayaworkbench.domain.enums import ApprovalStatus, TaskType, WorkflowState
from mayaworkbench.errors import ApprovalError, TaskError
from mayaworkbench.worker import Worker


def make_task(services, indexed, *, allowed=("utils.py",), max_steps=8, max_tools=20):
    return services.task_service.create_task(
        project_id=indexed.project.id,
        title="Fix utils",
        goal="Change count_tokens to a simple split and check it",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=list(allowed),
        acceptance_criteria=["count_tokens returns the number of words"],
        constraints={
            "max_steps": max_steps,
            "max_tool_calls": max_tools,
            "max_model_tokens": 20_000,
            "max_runtime_seconds": 120,
            # These tests exercise command execution. DENY_BY_DEFAULT now fails
            # closed when the host cannot create a network namespace, so the
            # egress allowance is an explicit contract choice.
            "network_access": "ALLOW_WITH_APPROVAL",
        },
    )


def start_and_approve(services, task, provider, *, actions):
    provider.actions = list(actions)
    result = services.task_service.start_task(task.id)
    for approval in result.approvals:
        services.task_service.approve(approval.id)
    return result


def finish_action(summary="Done.", limitations=None):
    return {
        "thought": "Finished.",
        "tool": "finish",
        "arguments": {
            "summary": summary,
            "limitations": limitations or [],
            "checks_run": [],
            "remaining_work": [],
        },
    }


def read_utils():
    return {
        "thought": "Read the target.",
        "tool": "read_file",
        "arguments": {"path": "utils.py"},
    }


def patch_utils():
    return {
        "thought": "Make the minimal change.",
        "tool": "apply_patch",
        "arguments": {
            "patch": (
                "--- a/utils.py\n"
                "+++ b/utils.py\n"
                "@@ -8,3 +8,3 @@\n"
                " def count_tokens(text: str) -> int:\n"
                '     \"\"\"Count whitespace-delimited tokens.\"\"\"\n'
                "-    return len(TOKEN_PATTERN.findall(text))\n"
                "+    return len(text.split())\n"
            )
        },
    }


def command_ok():
    return {
        "thought": "Check the behavior.",
        "tool": "run_command",
        "arguments": {
            "argv": [
                "python",
                "-c",
                "from utils import count_tokens; assert count_tokens('a b') == 2; print('ok')",
            ],
            "timeout_seconds": 30,
        },
    }


def test_approvals_are_required_scoped_single_use_and_expire(services, indexed, task_model):
    task, contract = make_task(services, indexed)
    task_model.actions = [finish_action()]
    result = services.task_service.start_task(task.id)
    assert result.run.state == WorkflowState.AWAITING_APPROVAL
    assert {item.action.value for item in result.approvals} == {
        "WRITE_ACCESS",
        "COMMAND_EXECUTION",
    }
    with pytest.raises(ApprovalError, match="pending"):
        services.task_service.prepare_execution(result.run.id)

    write = next(item for item in result.approvals if item.action.value == "WRITE_ACCESS")
    approved = services.task_service.approve(write.id)
    assert approved.status == ApprovalStatus.APPROVED
    with pytest.raises(ApprovalError, match="COMMAND_EXECUTION"):
        services.task_service.prepare_execution(result.run.id)

    from mayaworkbench.tasks.approvals import expected_write_sha

    plan = services.plans.get(result.run.plan_id)
    expected = expected_write_sha(
        run=services.runs.get(result.run.id), contract=contract, plan=plan
    )
    consumed = services.approval_service.consume(approval_id=write.id, spec_sha256=expected)
    assert consumed.status == ApprovalStatus.CONSUMED
    with pytest.raises(ApprovalError, match="not APPROVED"):
        services.approval_service.consume(approval_id=write.id, spec_sha256=expected)

    command = next(
        item for item in result.approvals if item.action.value == "COMMAND_EXECUTION"
    )
    services.db.execute(
        "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", command.id),
    )
    with pytest.raises(ApprovalError, match="expired"):
        services.task_service.approve(command.id)


def test_denial_cancels_the_run(services, indexed, task_model):
    task, _ = make_task(services, indexed)
    task_model.actions = [finish_action()]
    result = services.task_service.start_task(task.id)
    approval = result.approvals[0]
    denied = services.task_service.deny(approval.id, reason="scope is wrong")
    assert denied.status == ApprovalStatus.DENIED
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.CANCELLED


def test_terminal_runs_cannot_be_reopened(services, indexed, task_model):
    task, _ = make_task(services, indexed)
    task_model.actions = [finish_action()]
    result = services.task_service.start_task(task.id)
    services.task_service.cancel_run(result.run.id, reason="stop")
    with pytest.raises(TaskError, match="terminal"):
        services.workflow.transition(result.run.id, WorkflowState.IMPLEMENTING)


def test_full_agent_flow_keeps_original_checkout_and_records_patch(
    services, indexed, task_model
):
    repo_file = indexed.repo / "utils.py"
    before = repo_file.read_text()
    task, _ = make_task(services, indexed)
    result = start_and_approve(
        services,
        task,
        task_model,
        actions=[read_utils(), patch_utils(), command_ok(), finish_action("Changed count_tokens.")],
    )
    prepared = services.task_service.prepare_execution(result.run.id)
    assert prepared.run.state == WorkflowState.IMPLEMENTING
    workspace = services.workspaces.get_by_run(result.run.id)
    assert workspace is not None
    assert repo_file.read_text() == before

    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, run.error
    assert run.step_count == 4
    assert run.tool_call_count == 3
    assert repo_file.read_text() == before
    assert "return len(text.split())" in (
        Path(workspace.path) / "utils.py"
    ).read_text()
    calls = services.calls.list_by_run(run.id)
    assert [call.tool_name for call in calls] == ["read_file", "apply_patch", "run_command"]
    assert all(call.status.value == "SUCCEEDED" for call in calls)
    assert calls[0].output_artifact_id and calls[2].output_artifact_id
    assert calls[1].details["data"]["files"][0]["path"] == "utils.py"

    diff = services.task_service.capture_diff(run.id)
    assert diff.files[0].path == "utils.py"
    assert diff.scope_violations == []
    review = services.task_service.review_run(run.id)
    assert review.scope_ok is True
    assert review.secrets_found is False
    finished = services.task_service.finish_run(
        run.id, accept=True, reason="targeted change is reviewable"
    )
    assert finished.state == WorkflowState.COMPLETED_WITH_LIMITATIONS
    assert repo_file.read_text() == before


def test_pause_then_resume_continues_the_same_run(services, indexed, task_model):
    task, _ = make_task(services, indexed)
    result = start_and_approve(
        services, task, task_model, actions=[read_utils(), finish_action()]
    )
    services.task_service.prepare_execution(result.run.id)
    services.task_service.pause_run(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)
    paused = services.runs.get(result.run.id)
    assert paused.state == WorkflowState.PAUSED

    services.task_service.resume_run(result.run.id, spawn=False)
    Worker(services).run(once=True, run_id=result.run.id)
    resumed = services.runs.get(result.run.id)
    assert resumed.state == WorkflowState.REVIEWING
    assert resumed.tool_call_count == 1


def test_cancel_before_worker_marks_job_and_run_cancelled(services, indexed, task_model):
    task, _ = make_task(services, indexed)
    result = start_and_approve(services, task, task_model, actions=[finish_action()])
    services.task_service.prepare_execution(result.run.id)
    services.task_service.cancel_run(result.run.id, reason="user stopped it")
    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.CANCELLED
    job = services.jobs.get_by_run(result.run.id)
    assert job is not None and job.status.value == "CANCELLED"


def test_scope_violation_is_blocked_and_not_written(services, indexed, task_model):
    task, _ = make_task(services, indexed)
    result = start_and_approve(
        services,
        task,
        task_model,
        actions=[
            {
                "thought": "Try to escape scope.",
                "tool": "write_file",
                "arguments": {"path": "other.py", "content": "bad = True\n"},
            },
            finish_action("Could not write outside scope.", ["Scope refusal."]),
        ],
    )
    services.task_service.prepare_execution(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING
    workspace = services.workspaces.get_by_run(run.id)
    assert not (Path(workspace.path) / "other.py").exists()
    call = services.calls.list_by_run(run.id)[0]
    assert call.tool_name == "write_file"
    assert call.status.value == "BLOCKED"


def test_disallowed_executable_is_refused(services, indexed, task_model):
    task, _ = make_task(services, indexed)
    result = start_and_approve(
        services,
        task,
        task_model,
        actions=[
            {
                "thought": "Try git.",
                "tool": "run_command",
                "arguments": {"argv": ["git", "status"], "timeout_seconds": 5},
            },
            finish_action("Command was refused.", ["git is not in the allowlist."]),
        ],
    )
    services.task_service.prepare_execution(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)
    call = services.calls.list_by_run(result.run.id)[0]
    assert call.status.value == "FAILED"
    assert "not allowed" in (call.error or "")


def test_original_checkout_change_is_detected_and_blocks_acceptance(
    services, indexed, task_model
):
    task, _ = make_task(services, indexed)
    result = start_and_approve(services, task, task_model, actions=[read_utils(), finish_action()])
    services.task_service.prepare_execution(result.run.id)
    # Simulate a process (or a command escaping the worktree) changing the user's
    # checkout while the run is active.
    (indexed.repo / "utils.py").write_text("tampered = True\n", encoding="utf-8")
    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING
    diff = services.diffs.get_by_run(run.id)
    assert diff is not None and diff.original_checkout_unchanged is False
    review = services.task_service.review_run(run.id)
    assert review.scope_ok is False
    assert any(finding.code == "ORIGINAL_CHECKOUT_CHANGED" for finding in review.findings)
    with pytest.raises(TaskError, match="blocking review findings"):
        services.task_service.finish_run(run.id, accept=True, reason="accept anyway")


def test_step_budget_blocks_and_preserves_partial_evidence(services, indexed, task_model):
    task, _ = make_task(services, indexed, max_steps=1)
    result = start_and_approve(services, task, task_model, actions=[read_utils()])
    services.task_service.prepare_execution(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.BLOCKED
    assert run.error == "max_steps"
    assert services.diffs.get_by_run(run.id) is not None


def test_contract_pins_research_context_and_rejects_foreign_claims(
    services, indexed, paper, tmp_path
):
    """MWB-014: a paper-to-code task carries its exact research interpretation."""
    summary = services.claim_service.extract(paper.document.id)
    assert summary.claims
    claim_id = summary.claims[0].id
    mapping = services.mapping_service.map_project(indexed.project, indexed.snapshot)
    task, contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Implement a paper claim",
        goal="Apply the pinned claim in utils.py",
        task_type=TaskType.PAPER_TO_CODE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        research_document_ids=[paper.document.id],
        claim_ids=[claim_id],
        mapping_artifact_id=mapping.artifact_id,
    )
    reference = contract.research_context
    assert reference is not None
    assert reference.document_ids == [paper.document.id]
    assert reference.claim_ids == [claim_id]
    assert reference.mapping_artifact_id == mapping.artifact_id
    assert reference.mapping_sha256

    # The reference is part of the immutable contract and survives a reload.
    reloaded = services.contracts.latest(task.id)
    assert reloaded.research_context == reference

    from mayaworkbench.domain.enums import ClaimType

    other_project = services.create_project("Another Project", "isolation check")
    foreign = services.claim_service.add_claim(
        other_project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="Foreign requirement",
        statement="A requirement from a different project must not be pinned.",
        implementation_requirement="Implement it elsewhere.",
        accept=True,
    )
    with pytest.raises(TaskError, match="belongs to another project"):
        services.task_service.create_task(
            project_id=indexed.project.id,
            title="Foreign claim",
            goal="Should be refused",
            task_type=TaskType.PAPER_TO_CODE,
            snapshot_id=indexed.snapshot.id,
            allowed_paths=["utils.py"],
            claim_ids=[foreign.id],
        )
    with pytest.raises(TaskError, match="was not found"):
        services.task_service.create_task(
            project_id=indexed.project.id,
            title="Missing claim",
            goal="Should be refused",
            task_type=TaskType.PAPER_TO_CODE,
            snapshot_id=indexed.snapshot.id,
            allowed_paths=["utils.py"],
            claim_ids=["rcl_missing"],
        )

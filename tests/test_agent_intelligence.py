"""The agent's grounding, finish gate, repair loop and progress guards.

These tests drive the real worker, the real tool gateway, the real command
runner and a scripted model, so a pass means the whole loop behaved — not that a
helper returned a plausible value.
"""

from __future__ import annotations

import json

import pytest

from mayaworkbench.domain.enums import ApprovalAction, TaskType, ToolCallStatus, WorkflowState
from mayaworkbench.tasks.inloop import (
    DeclaredCheckRunner,
    classify_exit,
    failing_tests,
    failure_excerpt,
)
from mayaworkbench.tasks.inloop import test_summary as pytest_summary
from mayaworkbench.tasks.orientation import MAX_ORIENTATION_CHARS, build_orientation
from mayaworkbench.verification.registry import command_check
from mayaworkbench.worker import Worker

from tests.conftest import ScriptedTaskProvider, install_task_model

CHECK_ARGS = "from utils import count_tokens; assert count_tokens('a-b') == 1; print('ok')"


class RecordingProvider(ScriptedTaskProvider):
    """Scripted provider that also keeps the context it was given."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def generate(self, request):
        self.messages.append(request.messages[-1].content)
        return super().generate(request)


def action(tool: str, **arguments) -> dict:
    return {"thought": f"Use {tool}.", "tool": tool, "arguments": arguments}


def finish(summary: str = "Done.", limitations=None) -> dict:
    return action(
        "finish",
        summary=summary,
        limitations=limitations or [],
        checks_run=[],
        remaining_work=[],
    )


def read_utils() -> dict:
    return action("read_file", path="utils.py")


def patch_utils(from_line: str, to_line: str) -> dict:
    return action(
        "apply_patch",
        patch=(
            "--- a/utils.py\n"
            "+++ b/utils.py\n"
            "@@ -8,3 +8,3 @@\n"
            " def count_tokens(text: str) -> int:\n"
            '     """Count whitespace-delimited tokens."""\n'
            f"-    {from_line}\n"
            f"+    {to_line}\n"
        ),
    )


def make_task(
    services,
    indexed,
    *,
    checks=None,
    required_approvals=None,
    max_repair_attempts: int = 2,
    max_steps: int = 12,
):
    return services.task_service.create_task(
        project_id=indexed.project.id,
        title="Harden count_tokens",
        goal="Change count_tokens to split on whitespace and make the declared check pass",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["count_tokens counts whitespace-separated tokens"],
        verification_checks=checks or [],
        required_approvals=required_approvals
        or [ApprovalAction.WRITE_ACCESS, ApprovalAction.COMMAND_EXECUTION],
        constraints={
            "max_steps": max_steps,
            "max_tool_calls": 40,
            "max_model_tokens": 40_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
            "max_repair_attempts": max_repair_attempts,
        },
    )


def start_and_approve(services, task):
    result = services.task_service.start_task(task.id)
    for approval in result.approvals:
        services.task_service.approve(approval.id)
    services.task_service.prepare_execution(result.run.id)
    return result


# ---------------------------------------------------------------- finish gate
def test_rootless_task_provisions_one_shared_overlay(
    services, indexed, home, monkeypatch
):
    """An isolated task must route tools and commands through one populated tree."""
    from mayaworkbench.workspace import capabilities as caps_mod
    from mayaworkbench.workspace.backends import (
        RootlessContainerBackend,
        RuntimeCapabilities,
    )
    from mayaworkbench.workspace.runner import LimitEnforcement
    from mayaworkbench.worker import Worker

    available = caps_mod.probe_execution_capability_report()
    available.trust_profiles["ROOTLESS_CONTAINER"] = {
        "available": True,
        "reason": "fake smoke-verified runtime",
        "boundary": "test",
    }
    monkeypatch.setattr(
        caps_mod, "probe_execution_capability_report", lambda: available
    )
    monkeypatch.setattr(
        RootlessContainerBackend,
        "capabilities",
        lambda self: RuntimeCapabilities(
            True,
            ("fake rootless runtime",),
            {
                "wall_time": LimitEnforcement("enforced", "supervisor"),
                "cpu": LimitEnforcement("not_applied", None),
                "memory": LimitEnforcement("not_applied", None),
                "process_count": LimitEnforcement("not_applied", None),
                "file_size": LimitEnforcement("unsupported", None),
                "open_files": LimitEnforcement("unsupported", None),
            },
            "none",
        ),
    )

    task, _contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Isolated overlay task",
        goal="Finish without changing files",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["no edits"],
        verification_checks=[],
        required_approvals=[ApprovalAction.WRITE_ACCESS],
        constraints={
            "max_steps": 6,
            "execution_trust": "ROOTLESS_CONTAINER",
            "network_access": "DENY_BY_DEFAULT",
        },
    )
    provider = RecordingProvider()
    provider.actions = [finish("Isolated run complete.")]
    install_task_model(services, home, monkeypatch, provider)
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, (run.state, run.error)
    binding = run.details["runtime_binding"]
    assert binding["kind"] == "container"
    assert binding["trust_profile"] == "ROOTLESS_CONTAINER"
    assert binding["runtime_ref"]["backend"] == "rootless-container"
    from pathlib import Path

    overlay = Path(binding["active_path"])
    assert (overlay / "utils.py").is_file()  # the overlay is populated, not empty


def test_finish_is_a_request_and_a_failed_check_drives_one_repair_round(
    services, indexed, home, monkeypatch
):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    task, contract = make_task(
        services,
        indexed,
        checks=[command_check(f'python -c "{CHECK_ARGS}"', criteria=["AC1"], timeout_seconds=30)],
    )
    provider.actions = [
        read_utils(),
        patch_utils("return len(TOKEN_PATTERN.findall(text))", "return len(text.split()) + 1"),
        finish("First attempt."),
        patch_utils("return len(text.split()) + 1", "return len(text.split())"),
        finish("Fixed after the failing check."),
    ]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, run.error
    assert provider.actions == [], "the repair action and its finish were consumed"
    assert any("REPAIR_REQUIRED" in message for message in provider.messages)

    rounds = run.details["in_loop_check_rounds"]
    assert [item["verdict"] for item in rounds] == ["FAILED", "PASSED"]
    assert run.details["in_loop_verdict"] == "PASSED"
    assert any("In-loop declared checks passed" in item for item in run.limitations)

    events = [item.event_type.value for item in services.events.list(run_id=run.id, limit=500)]
    assert events.count("AGENT_CHECK_COMPLETED") == 2
    assert events.count("AGENT_REPAIR_REQUESTED") == 1

    # The gate must not have produced a verification report: that is the job of
    # `maya verify run` with independently captured evidence.
    assert services.verification.latest_report(run.id) is None


def test_a_real_failure_is_not_turned_into_a_pass_when_repair_budget_is_zero(
    services, indexed, home, monkeypatch
):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    task, _ = make_task(
        services,
        indexed,
        checks=[command_check(f'python -c "{CHECK_ARGS}"', criteria=["AC1"], timeout_seconds=30)],
        max_repair_attempts=0,
    )
    provider.actions = [
        read_utils(),
        patch_utils("return len(TOKEN_PATTERN.findall(text))", "return len(text.split()) + 1"),
        finish("Believe me, it passes."),
    ]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING
    assert run.details["in_loop_verdict"] == "FAILED"
    assert any("failed in-loop" in item for item in run.limitations)
    # No repair round was offered, so no repair prompt was ever sent.
    assert not any("REPAIR_REQUIRED" in message for message in provider.messages)


def test_a_broken_checker_is_not_sent_back_as_a_code_failure(
    services, indexed, home, monkeypatch
):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    # pytest exits 4 (usage error) when the target does not exist. That is a
    # checker problem: asking the model to repair code cannot help.
    task, _ = make_task(
        services,
        indexed,
        checks=[
            command_check(
                'python -m pytest tests/definitely_missing.py -q',
                criteria=["AC1"],
                timeout_seconds=30,
            )
        ],
    )
    provider.actions = [
        read_utils(),
        patch_utils("return len(TOKEN_PATTERN.findall(text))", "return len(text.split())"),
        finish("Implemented; the check itself is misconfigured."),
    ]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, run.error
    assert run.details["in_loop_verdict"] == "BROKEN_CHECKER"
    assert len(run.details["in_loop_check_rounds"]) == 1
    assert any("produced no verdict in-loop" in item for item in run.limitations)
    assert not any("REPAIR_REQUIRED" in message for message in provider.messages)
    assert len(provider.messages) == 4  # plan + three steps, no repair step


def test_a_command_check_requires_command_approval_at_contract_creation(
    services, indexed
):
    # A contract that declares a command check but withholds COMMAND_EXECUTION
    # would promise a verdict it can never produce. It is refused up front
    # rather than discovered as BLOCKED after the model has done the work.
    from mayaworkbench.errors import TaskError

    with pytest.raises(TaskError, match="COMMAND_EXECUTION"):
        make_task(
            services,
            indexed,
            checks=[command_check(f'python -c "{CHECK_ARGS}"', criteria=["AC1"])],
            required_approvals=[ApprovalAction.WRITE_ACCESS],
        )


def test_orientation_reaches_the_first_step_and_stops_being_a_prompt_tax(
    services, indexed, home, monkeypatch
):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    task, _ = make_task(services, indexed, checks=[])
    provider.actions = [read_utils(), action("git_status"), finish("Done.")]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    # messages[0] is the planner; the rest are the agent's steps.
    first, later, last = provider.messages[1], provider.messages[2], provider.messages[-1]
    assert "repository_orientation" in first
    assert "utils.py" in first
    assert "repository_orientation" not in last
    assert "workspace_ledger" in first and "workspace_ledger" in later


# ------------------------------------------------------------------- measures
def test_run_check_tool_executes_only_declared_checks(services, indexed, home, monkeypatch):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    task, contract = make_task(
        services,
        indexed,
        checks=[command_check(f'python -c "{CHECK_ARGS}"', criteria=["AC1"], timeout_seconds=30)],
    )
    key = contract.verification_checks[0].key
    provider.actions = [
        read_utils(),
        action("run_check", key=key),
        patch_utils("return len(TOKEN_PATTERN.findall(text))", "return len(text.split())"),
        action("run_check", key="not.a.declared.check"),
        finish("Done."),
    ]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    calls = services.calls.list_by_run(result.run.id, limit=100)
    checks = [call for call in calls if call.tool_name == "run_check"]
    assert len(checks) == 2
    # The first run_check ran before the patch, so the declared check failed.
    assert checks[0].status == ToolCallStatus.FAILED
    assert checks[0].details["data"]["in_loop"] is True
    # An undeclared key is refused and never reaches the command runner.
    assert checks[1].status == ToolCallStatus.FAILED
    assert "no declared check has key" in (checks[1].error or "")


def test_find_symbol_reads_the_pinned_index(services, indexed, home, monkeypatch):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    task, _ = make_task(services, indexed, checks=[])
    provider.actions = [
        action("find_symbol", query="count_tokens", limit=5),
        finish("Orientation complete."),
    ]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    calls = services.calls.list_by_run(result.run.id, limit=100)
    found = next(call for call in calls if call.tool_name == "find_symbol")
    assert found.status == ToolCallStatus.SUCCEEDED
    assert found.details["data"]["source"] == "pinned_index"
    assert any(
        item["qualified_name"] == "count_tokens" for item in found.details["data"]["results"]
    )


def test_repeating_a_read_only_action_is_refused_instead_of_burning_the_budget(
    services, indexed, home, monkeypatch
):
    provider = RecordingProvider()
    install_task_model(services, home, monkeypatch, provider)
    task, _ = make_task(services, indexed, checks=[])
    provider.actions = [read_utils() for _ in range(8)]
    result = start_and_approve(services, task)
    Worker(services).run(once=True, run_id=result.run.id)

    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.BLOCKED
    assert "repeated action" in (run.error or "")
    assert services.calls.count_by_run(run.id) <= 3
    assert any("repeated the same read-only action" in message for message in provider.messages)


# -------------------------------------------------------------- unit: feedback
@pytest.mark.parametrize(
    ("argv", "exit_code", "infrastructure", "output"),
    [
        (["python", "-m", "pytest", "tests", "-q"], 0, False, ""),
        (["python", "-m", "pytest", "tests", "-q"], 1, False, ""),
        (["python", "-m", "pytest", "tests", "-q"], 2, False, ""),
        (["python", "-m", "pytest", "tests", "-q"], 3, True, ""),
        (["python", "-m", "pytest", "tests", "-q"], 4, True, ""),
        (["python", "-m", "pytest", "tests", "-q"], 5, True, ""),
        (["python", "-m", "pytest", "tests", "-q"], 6, True, ""),
        (["pytest", "-q"], 1, False, ""),
        (["python", "-c", "raise SystemExit(1)"], 1, False, ""),
        (["python", "-c", "pass"], None, True, ""),
        # A checker that cannot import itself is broken apparatus, never a code
        # failure — even though pytest/python exits 1 for ModuleNotFoundError.
        (
            ["python", "-m", "pytest", "-q"],
            1,
            True,
            "No module named pytest",
        ),
        (["python", "-m", "pytest", "--definitely-not-a-flag"], 4, True, "unrecognized arguments: --definitely-not-a-flag"),
        (["pytest", "-q"], 127, True, "pytest: command not found"),
    ],
)
def test_exit_code_classification_separates_code_failures_from_broken_checkers(
    argv, exit_code, infrastructure, output
):
    is_infrastructure, reason = classify_exit(argv, exit_code, output)
    assert is_infrastructure is infrastructure
    assert reason


def test_pytest_failure_parsing_and_excerpt_keep_the_actionable_part():
    output = (
        "tests/test_a.py ....F\n"
        "=================================== FAILURES ===================================\n"
        "________________________ test_count_tokens ________________________________\n"
        "    def test_count_tokens():\n"
        ">       assert count_tokens('a-b') == 1\n"
        "E       assert 2 == 1\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_count_tokens - assert 2 == 1\n"
        "1 failed, 4 passed in 0.12s\n"
    )
    assert failing_tests(output) == ["tests/test_a.py::test_count_tokens"]
    excerpt = failure_excerpt("", output)
    assert "assert 2 == 1" in excerpt
    assert "....F" not in excerpt
    assert "1 failed, 4 passed" in (pytest_summary(output) or "")


def test_a_check_that_cannot_fit_the_round_budget_is_not_run_not_failed(tmp_path):
    from mayaworkbench.domain.models import TaskContract, TaskScope

    contract = TaskContract(
        id="con_1",
        task_id="task_1",
        project_id="prj_1",
        version=1,
        task_type=TaskType.ENGINEERING_CHANGE,
        goal="x",
        repository_id="repo_1",
        snapshot_id="snap_1",
        scope=TaskScope(allowed_paths=["a.py"], base_commit_sha="0" * 40),
        acceptance_criteria=["a"],
        verification_checks=[command_check('python -c "print(1)"')],
        content_sha256="a" * 64,
    )
    runner = DeclaredCheckRunner(
        contract=contract,
        workspace=tmp_path,
        run_home=tmp_path / "home",
        authorized=True,
    )
    round_result = runner.run(deadline_seconds=0)
    assert round_result.results[0].not_run is True
    assert round_result.results[0].outcome == "INCONCLUSIVE"
    assert not round_result.passed
    assert not round_result.failures


def test_feedback_round_does_not_call_a_not_run_check_a_pass(tmp_path):
    from mayaworkbench.domain.models import TaskConstraints, TaskContract, TaskScope

    contract = TaskContract(
        id="con_1",
        task_id="task_1",
        project_id="prj_1",
        version=1,
        task_type=TaskType.ENGINEERING_CHANGE,
        goal="x",
        repository_id="repo_1",
        snapshot_id="snap_1",
        scope=TaskScope(allowed_paths=["a.py"], base_commit_sha="0" * 40),
        acceptance_criteria=["a"],
        verification_checks=[command_check(f'python -c "{CHECK_ARGS}"')],
        content_sha256="a" * 64,
    )
    runner = DeclaredCheckRunner(
        contract=contract,
        workspace=tmp_path,
        run_home=tmp_path / "home",
        authorized=False,
    )
    round_result = runner.run()
    assert not round_result.passed
    assert round_result.infrastructure_failures
    assert not round_result.failures
    assert round_result.results[0].outcome == "BLOCKED"
    del TaskConstraints


# ----------------------------------------------------------------- orientation
def test_orientation_is_grounded_scoped_and_bounded(services, indexed):
    orientation = build_orientation(
        retrieval=services.retrieval,
        files=services.files,
        symbols=services.symbols,
        issues=services.issues,
        snapshot_id=indexed.snapshot.id,
        goal="count tokens in text",
        allowed_paths=["utils.py"],
    )
    assert orientation["index_available"] is True
    assert orientation["retrieval_available"] is True
    assert orientation["symbols"], "the allowed file's symbols should be listed"
    assert {item["path"] for item in orientation["symbols"]} == {"utils.py"}
    assert len(json.dumps(orientation)) <= MAX_ORIENTATION_CHARS + 600


def test_orientation_degrades_honestly_without_an_index(services):
    orientation = build_orientation(
        retrieval=services.retrieval,
        files=services.files,
        symbols=services.symbols,
        issues=services.issues,
        snapshot_id="snap_does_not_exist",
        goal="anything",
        allowed_paths=["utils.py"],
    )
    assert orientation["index_available"] is False
    assert orientation["spans"] == []
    assert orientation["retrieval_notes"] or orientation.get("retrieval_note")

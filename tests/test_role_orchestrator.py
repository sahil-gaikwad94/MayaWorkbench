"""WP8: one deterministic orchestrator with typed role handoffs.

These tests drive the real worker, real stores and a scripted model. They check
the DAG shape, parallel analysis, independent review context, bounded repair,
and that only the backend owns workflow state. No network or real model is used.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mayaworkbench.domain.enums import (
    ApprovalAction,
    CheckOutcome,
    EventType,
    TaskType,
    WorkflowState,
)
from mayaworkbench.tasks.agent import AgentOutcome
from mayaworkbench.tasks.inloop import CheckFeedback, FeedbackRound
from mayaworkbench.tasks.orchestrator import (
    RepairGuard,
    RoleOrchestrator,
    set_role_plan_enabled,
)
from mayaworkbench.tasks.roles import REVIEWER_CONTEXT, AgentRole, build_role_plan
from mayaworkbench.verification.registry import command_check
from mayaworkbench.worker import Worker

from tests.conftest import ScriptedTaskProvider, install_task_model

CHECK_ARGS = "from utils import count_tokens; assert count_tokens('a b') == 2; print('ok')"


def make_task(services, indexed, *, task_type=TaskType.ENGINEERING_CHANGE):
    return services.task_service.create_task(
        project_id=indexed.project.id,
        title="Fix utils with roles",
        goal="Change count_tokens to a whitespace split and pass the declared check",
        task_type=task_type,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["count_tokens counts whitespace-separated tokens"],
        verification_checks=[
            command_check(f'python -c "{CHECK_ARGS}"', criteria=["AC1"], timeout_seconds=30)
        ],
        required_approvals=[ApprovalAction.WRITE_ACCESS, ApprovalAction.COMMAND_EXECUTION],
        constraints={
            "max_steps": 12,
            "max_tool_calls": 40,
            "max_model_tokens": 40_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
            "max_repair_attempts": 2,
        },
    )


def start_approve_prepare(services, task):
    result = services.task_service.start_task(task.id)
    for approval in result.approvals:
        services.task_service.approve(approval.id)
    services.task_service.prepare_execution(result.run.id)
    return result


def action(tool: str, **arguments) -> dict:
    return {"thought": f"Use {tool}.", "tool": tool, "arguments": arguments}


def finish(summary: str) -> dict:
    return action(
        "finish",
        summary=summary,
        limitations=[],
        checks_run=[],
        remaining_work=[],
    )


def failing_round() -> FeedbackRound:
    feedback = CheckFeedback(
        key="command.count_tokens",
        title="count_tokens check",
        gate="C_FUNCTIONAL",
        outcome=CheckOutcome.FAILED.value,
        reason="tests were collected and some failed",
        command=["python", "-m", "pytest", "tests/test_a.py", "-q"],
        exit_code=1,
        excerpt="FAILED tests/test_a.py::test_count_tokens - assert 1 == 2",
        failing_tests=["tests/test_a.py::test_count_tokens"],
    )
    return FeedbackRound(results=[feedback], duration_ms=10)


# ------------------------------------------------------------------- plan DAG
def test_role_plan_is_typed_deterministic_and_task_scoped(services, indexed):
    task, contract = make_task(services, indexed)
    first = build_role_plan(task, contract, {"symbols": [], "index_available": True})
    second = build_role_plan(task, contract, {"symbols": [], "index_available": True})
    assert first.plan_sha256 == second.plan_sha256
    assert first.plan_sha256 == build_role_plan(task, contract, None).plan_sha256

    roles = {step.role for step in first.steps}
    assert AgentRole.WORKSPACE_ANALYST in roles
    assert AgentRole.RESEARCH_ANALYST not in roles  # no research context
    assert AgentRole.IMPLEMENTER in roles
    assert AgentRole.INDEPENDENT_REVIEWER in roles
    assert AgentRole.SECURITY_REVIEWER in roles

    for step in first.steps:
        assert step.objective
        assert step.output_artifact
        assert step.authority
        assert step.budget.max_steps == contract.constraints.max_steps
        assert step.input_hashes["contract"] == contract.content_sha256
        assert isinstance(step.required_capabilities, list)
        assert step.output_hashes == {}  # assigned only when an artifact exists

    # Independent analysis is parallelisable; implementation waits for it.
    workspace = first.by_role(AgentRole.WORKSPACE_ANALYST)
    assert workspace is not None and workspace.parallelizable
    implementer = first.by_role(AgentRole.IMPLEMENTER)
    assert implementer is not None
    assert workspace.step_id in implementer.dependencies
    assert first.by_role(AgentRole.ARCHITECT).step_id in implementer.dependencies

    reviewer = first.by_role(AgentRole.INDEPENDENT_REVIEWER)
    assert reviewer is not None and reviewer.independent
    assert reviewer.context_sources == list(REVIEWER_CONTEXT)
    assert "implementer_self_evaluation" not in reviewer.context_sources
    assert first.by_role(AgentRole.QA_TEST_ENGINEER).step_id in reviewer.dependencies
    assert first.by_role(AgentRole.RELEASE_COORDINATOR).dependencies == [reviewer.step_id]

    # A paper-to-code task adds the parallel research analyst, and the
    # implementer waits for both analysis outputs.
    paper_contract = contract.model_copy(update={"task_type": TaskType.PAPER_TO_CODE})
    paper = build_role_plan(task, paper_contract, None)
    research = paper.by_role(AgentRole.RESEARCH_ANALYST)
    assert research is not None and research.parallelizable
    paper_implementer = paper.by_role(AgentRole.IMPLEMENTER)
    assert research.step_id in paper_implementer.dependencies
    assert workspace.step_id in paper_implementer.dependencies
    assert paper.plan_sha256 != first.plan_sha256


# --------------------------------------------------------------- orchestration
def test_orchestrator_emits_typed_artifacts_and_reuses_controlled_agent(
    services, indexed, home, monkeypatch
):
    self_report = "Implementer says: everything is perfect and fully verified by itself."
    provider = ScriptedTaskProvider()
    provider.actions = [
        action("read_file", path="utils.py"),
        action(
            "apply_patch",
            patch=(
                "--- a/utils.py\n"
                "+++ b/utils.py\n"
                "@@ -8,3 +8,3 @@\n"
                " def count_tokens(text: str) -> int:\n"
                '     """Count whitespace-delimited tokens."""\n'
                "-    return len(TOKEN_PATTERN.findall(text))\n"
                "+    return len(text.split())\n"
            ),
        ),
        finish(self_report),
    ]
    install_task_model(services, home, monkeypatch, provider)
    task, contract = make_task(services, indexed)
    result = services.task_service.start_task(task.id)
    set_role_plan_enabled(services.runs, result.run.id, enabled=True)
    for approval in result.approvals:
        services.task_service.approve(approval.id)
    services.task_service.prepare_execution(result.run.id)

    Worker(services).run(once=True, run_id=result.run.id)

    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, (run.error, run.limitations)
    assert provider.actions == [], "the ControlledAgent implementer consumed its scripted actions"

    expected_ids = build_role_plan(task, services.contracts.get(contract.id), None).role_ids()
    detail_ids = run.details["role_plan_completed"]
    assert set(detail_ids) == set(expected_ids)
    metas = run.details["role_artifacts"]
    assert len(metas) == len(expected_ids)
    assert [item["step_id"] for item in metas] == list(detail_ids)
    assert len(set(detail_ids)) == len(detail_ids)  # no duplicate handoffs

    for meta in metas:
        assert meta["artifact_id"] is not None
        stored = services.artifacts.get(meta["artifact_id"])
        assert stored.kind.value == "OTHER"
        assert stored.created_by.startswith("orchestrator:") or stored.created_by == "ControlledAgent"
        assert json.loads(services.artifacts.read_text(meta["artifact_id"]))["role"] == meta["role"]

    implementer = next(item for item in metas if item["role"] == AgentRole.IMPLEMENTER.value)
    assert implementer["produced_by"] == "ControlledAgent"
    assert self_report in implementer["summary"]
    # The patch is captured after the implementer finishes, so no patch hash is
    # claimed on its artifact; the reviewer gets the real captured hash below.
    assert "patch" not in implementer["output_hashes"]

    reviewer = next(item for item in metas if item["role"] == AgentRole.INDEPENDENT_REVIEWER.value)
    reviewer_text = services.artifacts.read_text(reviewer["artifact_id"])
    for source in ("contract", "patch", "deterministic_results", "evidence"):
        assert source in reviewer_text
    assert run.details["patch_sha256"] in reviewer_text
    assert "excluded_sources" in reviewer_text and "implementer_self_evaluation" in reviewer_text
    # The implementer's self-evaluation is deliberately not part of the
    # reviewer's context, only the exclusion note is.
    assert self_report not in reviewer_text


# ----------------------------------------------------------------- repair loop
def test_repair_guard_requires_all_fields_and_refuses_same_patch_hash():
    guard = RepairGuard(max_attempts=2)
    round_result = failing_round()

    with pytest.raises(ValidationError):
        guard.build_attempt(
            round_result,
            root_cause_hypothesis="short",
            predicted_patch_sha256="not-a-hash",
            targeted_validation="pytest tests/test_a.py::test_count_tokens",
            regression_validation="pytest tests/test_a.py",
            round_number=1,
        )

    def build(patch_hash: str, round_number: int = 1):
        return guard.build_attempt(
            round_result,
            root_cause_hypothesis="The split count is off by one in count_tokens.",
            predicted_patch_sha256=patch_hash,
            targeted_validation="pytest tests/test_a.py::test_count_tokens",
            regression_validation="pytest tests/test_a.py",
            round_number=round_number,
        )

    first = build("a" * 64)
    assert first.failing_evidence[0]["key"] == "command.count_tokens"  # from inloop
    assert guard.evaluate(first).accepted
    guard.record(first)

    repeat = build("a" * 64)
    repeat_decision = guard.evaluate(repeat)
    assert not repeat_decision.accepted
    assert "patch hash" in repeat_decision.reason

    second = build("b" * 64)
    assert guard.evaluate(second).accepted
    guard.record(second)

    exhausted = build("c" * 64)
    exhausted_decision = guard.evaluate(exhausted)
    assert not exhausted_decision.accepted
    assert "budget" in exhausted_decision.reason
    assert len(guard.entries) == 2


def test_orchestrator_persists_repair_ledger(services, indexed, task_model):
    task, contract = make_task(services, indexed)
    result = start_approve_prepare(services, task)
    plan = build_role_plan(task, contract, None)
    orchestrator = RoleOrchestrator(
        runs=services.runs,
        artifacts=services.artifacts,
        events=services.events,
        workflow=services.workflow,
        plan=plan,
        task=task,
        contract=contract,
        run=services.runs.get(result.run.id),
        implementer=lambda: AgentOutcome(state=WorkflowState.REVIEWING, summary="done"),
    )
    attempt, decision = orchestrator.apply_repair_attempt(
        failing_round(),
        root_cause_hypothesis="The split count is off by one in count_tokens.",
        predicted_patch_sha256="d" * 64,
        targeted_validation="pytest tests/test_a.py::test_count_tokens",
        regression_validation="pytest tests/test_a.py",
        round_number=1,
    )
    assert decision.accepted
    ledger = services.runs.get(result.run.id).details["role_repair_ledger"]
    assert len(ledger) == 1
    assert ledger[0]["failing_signature"] == attempt.failing_signature


# --------------------------------------------------------- pause / idempotency
def test_pause_resume_is_idempotent_and_only_backend_owns_state(services, indexed, task_model):
    task, contract = make_task(services, indexed)
    result = start_approve_prepare(services, task)
    run_id = result.run.id
    plan = build_role_plan(task, contract, None)

    calls: list[int] = []

    def fake_implementer() -> AgentOutcome:
        calls.append(1)
        return AgentOutcome(state=WorkflowState.REVIEWING, summary="implemented", steps=1)

    def observer(step_id: str) -> None:
        if step_id == AgentRole.IMPLEMENTER.value:
            services.runs.set_flags(run_id, pause_requested=True)

    orchestrator = RoleOrchestrator(
        runs=services.runs,
        artifacts=services.artifacts,
        events=services.events,
        workflow=services.workflow,
        plan=plan,
        task=task,
        contract=contract,
        run=services.runs.get(run_id),
        implementer=fake_implementer,
        step_observer=observer,
    )

    def state_event_count() -> int:
        return len(
            [
                item
                for item in services.events.list(run_id=run_id, limit=500)
                if item.event_type == EventType.TASK_STATE_CHANGED
            ]
        )

    state_before = services.runs.get(run_id).state
    events_before = state_event_count()
    paused = orchestrator.run()
    assert paused.state == WorkflowState.PAUSED
    assert len(calls) == 1
    # The orchestrator never transitions state; only the worker/backend does.
    assert services.runs.get(run_id).state == state_before
    assert state_event_count() == events_before

    first_completed = services.runs.get(run_id).details["role_plan_completed"]
    assert AgentRole.IMPLEMENTER.value in first_completed
    assert len(first_completed) < len(plan.role_ids())
    first_artifacts = len(services.runs.get(run_id).details["role_artifacts"])

    # Resume: clear the flag and walk the same deterministic plan again.
    services.runs.set_flags(run_id, pause_requested=False)
    resumed = RoleOrchestrator(
        runs=services.runs,
        artifacts=services.artifacts,
        events=services.events,
        workflow=services.workflow,
        plan=plan,
        task=task,
        contract=contract,
        run=services.runs.get(run_id),
        implementer=fake_implementer,
    ).run()
    assert resumed.state == WorkflowState.REVIEWING
    assert len(calls) == 1, "a completed implementer handoff must not be replayed"

    final = services.runs.get(run_id)
    assert set(final.details["role_plan_completed"]) == set(plan.role_ids())
    assert len(final.details["role_artifacts"]) == len(plan.role_ids())
    assert len(final.details["role_artifacts"]) > first_artifacts
    assert services.runs.get(run_id).state == state_before
    assert state_event_count() == events_before

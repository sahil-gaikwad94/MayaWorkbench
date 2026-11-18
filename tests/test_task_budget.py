"""Budget accounting regressions (MWB-006)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mayaworkbench.domain.enums import TaskType
from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import BudgetError, TaskError
from mayaworkbench.tasks.budget import BudgetController


class _Runs:
    def __init__(self, tokens: int = 0, cost: float = 0.0):
        self.state = SimpleNamespace(model_tokens=tokens, model_cost_usd=cost)

    def get(self, _run_id: str):
        return self.state


def _controller(**overrides) -> BudgetController:
    return BudgetController(
        constraints=TaskConstraints(**overrides), run_id="run_test", runs=_Runs()
    )


def test_unknown_price_is_refused_not_treated_as_zero():
    controller = _controller(max_model_cost_usd=0.0)
    with pytest.raises(BudgetError, match="price is not configured"):
        controller.reserve(
            estimated_input_tokens=100,
            desired_output_tokens=200,
            cost_known=False,
        )


def test_explicit_unknown_cost_override_allows_the_call():
    controller = _controller(allow_unknown_cost=True)
    allowance = controller.reserve(
        estimated_input_tokens=100,
        desired_output_tokens=200,
        cost_known=False,
    )
    assert allowance == 200


def test_known_positive_price_with_a_zero_budget_is_refused():
    controller = _controller(max_model_cost_usd=0.0)
    with pytest.raises(BudgetError, match="monetary budget"):
        controller.reserve(
            estimated_input_tokens=100,
            desired_output_tokens=200,
            cost_known=True,
            input_price_per_mtok=1.0,
            output_price_per_mtok=1.0,
        )


def test_output_allowance_cannot_exceed_the_remaining_token_budget():
    controller = BudgetController(
        constraints=TaskConstraints(max_model_tokens=1_100),
        run_id="run_test",
        runs=_Runs(tokens=0),
    )
    allowance = controller.reserve(
        estimated_input_tokens=1_000,
        desired_output_tokens=2_000,
        cost_known=True,
    )
    assert allowance == 100


def test_exhausted_token_budget_stops_the_next_request():
    controller = BudgetController(
        constraints=TaskConstraints(max_model_tokens=1_000),
        run_id="run_test",
        runs=_Runs(tokens=990),
    )
    with pytest.raises(BudgetError, match="model-token budget"):
        controller.reserve(
            estimated_input_tokens=100,
            desired_output_tokens=200,
            cost_known=True,
        )


def test_planning_refuses_an_unknown_model_price(services, indexed, task_model):
    services.settings.model.input_cost_per_mtok = None
    services.settings.model.output_cost_per_mtok = None
    task, _contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Unknown price",
        goal="Change count_tokens",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        constraints={"max_steps": 4, "max_tool_calls": 10},
    )
    with pytest.raises(TaskError, match="price is not configured"):
        services.task_service.start_task(task.id)
    run = services.runs.latest_for_task(task.id)
    assert run is not None
    assert run.state.value == "FAILED"

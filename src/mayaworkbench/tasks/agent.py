"""The bounded Implementer loop.

One model step proposes one typed tool action. Application code validates it,
executes it through the gateway, records the observation, and checks budgets and
control flags. The model cannot mutate workflow state, approve itself, change
scope, or call an unregistered tool.

The loop is deliberately not an autonomous swarm: one role, one action at a
time, explicit budgets, a no-progress guard, and a mandatory ``finish`` action.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError as PydanticValidationError

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import ToolCallStatus, WorkflowState
from mayaworkbench.domain.models import (
    Task,
    TaskContract,
    TaskPlan,
    WorkflowRun,
)
from mayaworkbench.errors import BudgetError, ModelError, PolicyError
from mayaworkbench.models.base import ChatMessage, ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import RunStore, ToolCallStore
from mayaworkbench.tasks.agent_models import AgentAction, FinishArgs
from mayaworkbench.tasks.budget import BudgetController
from mayaworkbench.workspace.tools import ToolGateway

MAX_OBSERVATIONS = 10
MAX_OBSERVATION_CHARS = 5000

SYSTEM_PROMPT = """You are the Implementer role in MayaWorkbench, a controlled engineering agent.

You work only through the provided tools in an isolated Git worktree. The user's
original checkout is never your write target.

Hard rules:
- Read a file before changing it. Apply patches with exact, current context.
- Write only inside the contract's allowed paths. Never touch excluded paths,
  credentials, .git, or files outside the workspace.
- Do not weaken, delete or skip tests to make a command pass. If a test is wrong,
  explain why and propose the smallest honest change.
- Run only commands provided through run_command. Commands run without a shell.
  Do not attempt dependency installation or network access.
- Never claim a check passed unless a command observation shows exit code 0.
  Distinguish implemented from verified.
- Do not fabricate file contents, command output, line numbers or test results.
- If blocked by missing information or an environmental limitation, finish and
  say so in limitations rather than guessing.
- Call finish when the task is done or cannot proceed. The summary must describe
  what changed, what was actually checked, and what remains unverified.

Return exactly one action as structured JSON: a short thought, one tool name,
and its arguments. Tool names and arguments:
- read_file {path, start_line?, end_line?}
- list_files {path?, depth?}
- search_text {query, path?, limit?}
- apply_patch {patch}  # unified diff with ---/+++ and @@ hunks
- write_file {path, content, overwrite?}
- delete_file {path}  # only if the contract enables deletion
- git_status {}
- git_diff {}
- run_command {argv: [executable, ...], timeout_seconds?}
- finish {summary, limitations, checks_run, remaining_work}
"""


@dataclass
class AgentOutcome:
    state: WorkflowState
    summary: str = ""
    limitations: list[str] = field(default_factory=list)
    error: str | None = None
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


class ControlledAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        gateway: ModelGateway,
        runs: RunStore,
        calls: ToolCallStore,
        artifacts: ArtifactStore,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        plan: TaskPlan,
        tool_gateway: ToolGateway,
        heartbeat: Callable[[], bool] | None = None,
    ):
        self.settings = settings
        self.gateway = gateway
        self.runs = runs
        self.calls = calls
        self.artifacts = artifacts
        self.run = run
        self.task = task
        self.contract = contract
        self.plan = plan
        self.tools = tool_gateway
        self.heartbeat = heartbeat
        self._invalid_actions = 0
        self._recent_failures: list[str] = []
        self._consecutive_failures = 0
        self.budget = BudgetController(
            constraints=contract.constraints, run_id=run.id, runs=runs
        )

    # ------------------------------------------------------------------- loop
    def run_loop(self) -> AgentOutcome:
        started = time.monotonic()
        steps = self.run.step_count
        tokens = self.run.model_tokens
        cost = self.run.model_cost_usd
        last_error: str | None = None
        while True:
            if self.heartbeat is not None:
                self.heartbeat()
            current = self.runs.get(self.run.id)
            if current.cancel_requested:
                return AgentOutcome(
                    state=WorkflowState.CANCELLED,
                    summary="Cancellation was requested; the run stopped at a safe point.",
                    error="cancelled",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            if current.pause_requested:
                return AgentOutcome(
                    state=WorkflowState.PAUSED,
                    summary="Pause was requested; the run stopped at a safe point.",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            if time.monotonic() - started > self.contract.constraints.max_runtime_seconds:
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="The task wall-clock budget was exhausted.",
                    limitations=["Wall-clock budget exhausted before the agent finished."],
                    error="max_runtime_seconds",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            if steps >= self.contract.constraints.max_steps:
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="The task step budget was exhausted.",
                    limitations=["Step budget exhausted before a finish action."],
                    error="max_steps",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            if current.tool_call_count >= self.contract.constraints.max_tool_calls:
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="The task tool-call budget was exhausted.",
                    limitations=["Tool-call budget exhausted before a finish action."],
                    error="max_tool_calls",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            if tokens >= self.contract.constraints.max_model_tokens:
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="The task model-token budget was exhausted.",
                    limitations=["Model-token budget exhausted before a finish action."],
                    error="max_model_tokens",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            cost_limit = self.contract.constraints.max_model_cost_usd
            if cost > cost_limit or (cost_limit == 0 and cost > 0):
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary="The task model-cost budget was exhausted.",
                    limitations=[
                        "Model cost exceeded the contract budget; no further model calls were made."
                    ],
                    error="max_model_cost_usd",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            messages = self._messages(last_error=last_error)
            cost_known = (
                self.settings.model.input_cost_per_mtok is not None
                and self.settings.model.output_cost_per_mtok is not None
            )
            try:
                max_output_tokens = self.budget.reserve(
                    estimated_input_tokens=self.budget.estimate_input_tokens(
                        "\n".join(message.content for message in messages)
                    ),
                    desired_output_tokens=min(2000, self.settings.model.max_output_tokens),
                    cost_known=cost_known,
                    input_price_per_mtok=self.settings.model.input_cost_per_mtok,
                    output_price_per_mtok=self.settings.model.output_cost_per_mtok,
                )
            except BudgetError as exc:
                return AgentOutcome(
                    state=WorkflowState.BLOCKED,
                    summary=str(exc),
                    limitations=["The model budget stopped the run before the next request."],
                    error="model_budget",
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            request = ModelRequest(
                purpose="task_step",
                messages=messages,
                json_schema=AgentAction.model_json_schema(),
                schema_name="AgentAction",
                max_output_tokens=max_output_tokens,
            )
            try:
                response, invocation = self.gateway.structured(
                    request,
                    project_id=self.run.project_id,
                    snapshot_id=self.run.snapshot_id,
                    purpose="task_step",
                    correlation_id=self.run.id,
                )
            except (ModelError, PolicyError) as exc:
                return AgentOutcome(
                    state=WorkflowState.FAILED,
                    summary="The model call failed; no further edits were attempted.",
                    error=str(exc),
                    steps=steps,
                    tool_calls=current.tool_call_count,
                    tokens=tokens,
                    cost_usd=cost,
                )
            usage = response.usage
            used = usage.total_tokens
            if used is None and usage.prompt_tokens is not None and usage.completion_tokens is not None:
                used = usage.prompt_tokens + usage.completion_tokens
            if used is None:
                used = max(1, len(response.text) // 3)
            tokens += used
            # An unknown price is unknown, not zero. It is never added to the
            # total as if it were free.
            if usage.cost_usd is not None:
                cost += usage.cost_usd
            self.runs.set_flags(
                self.run.id,
                add_tokens=used,
                add_cost_usd=usage.cost_usd,
            )
            steps += 1
            self.runs.update_progress(self.run.id, step_count=steps)

            try:
                action = self._parse_action(response.structured, response.text)
            except (PydanticValidationError, ValueError, json.JSONDecodeError) as exc:
                self._invalid_actions += 1
                last_error = f"Invalid action ({self._invalid_actions}/3): {exc}"
                if self._invalid_actions >= 3:
                    return AgentOutcome(
                        state=WorkflowState.FAILED,
                        summary="The model did not produce a valid action after three attempts.",
                        limitations=["No valid agent action was produced."],
                        error=last_error,
                        steps=steps,
                        tool_calls=current.tool_call_count,
                        tokens=tokens,
                        cost_usd=cost,
                    )
                self.runs.update_progress(
                    self.run.id, details={**self._details(), "agent_last_error": last_error}
                )
                continue
            self._invalid_actions = 0
            last_error = None

            if action.tool == "finish":
                try:
                    finish = FinishArgs.model_validate(action.arguments)
                except PydanticValidationError as exc:
                    last_error = f"Invalid finish arguments: {exc}"
                    self._invalid_actions += 1
                    if self._invalid_actions >= 3:
                        return AgentOutcome(
                            state=WorkflowState.FAILED,
                            summary="The model repeatedly produced an invalid finish action.",
                            error=last_error,
                            steps=steps,
                            tool_calls=current.tool_call_count,
                            tokens=tokens,
                            cost_usd=cost,
                        )
                    continue
                limitations = list(finish.limitations) + [
                    "The implementer's own report is not verification; run "
                    "`maya verify run` before accepting this run."
                ]
                self.runs.update_progress(
                    self.run.id,
                    finish_summary=finish.summary,
                    limitations=limitations,
                    details={
                        **self._details(),
                        "agent_reported_checks": finish.checks_run,
                        "agent_remaining_work": finish.remaining_work,
                        "agent_reported_checks_note": (
                            "Model-reported checks; not independently verified in Phase 3."
                        ),
                    },
                )
                return AgentOutcome(
                    state=WorkflowState.REVIEWING,
                    summary=finish.summary,
                    limitations=limitations,
                    steps=steps,
                    tool_calls=self.calls.count_by_run(self.run.id),
                    tokens=tokens,
                    cost_usd=cost,
                )

            result = self.tools.invoke(action.tool, action.arguments)
            latest = self.runs.get(self.run.id)
            self.runs.update_progress(
                self.run.id,
                tool_call_count=self.calls.count_by_run(self.run.id),
                details={
                    **self._details(latest),
                    "agent_last_error": None,
                    "last_action": {
                        "tool": action.tool,
                        "status": result.status.value,
                        "summary": result.summary,
                    },
                },
            )
            if result.status == ToolCallStatus.FAILED:
                self._consecutive_failures += 1
                if self._consecutive_failures > self.contract.constraints.max_repair_attempts + 1:
                    return AgentOutcome(
                        state=WorkflowState.BLOCKED,
                        summary="The repair-attempt budget was exhausted.",
                        limitations=[
                            "Tool failures exceeded max_repair_attempts; stopped without a repair hypothesis."
                        ],
                        error="max_repair_attempts",
                        steps=steps,
                        tool_calls=self.calls.count_by_run(self.run.id),
                        tokens=tokens,
                        cost_usd=cost,
                    )
                fingerprint = f"{action.tool}:{json.dumps(action.arguments, sort_keys=True)}"
                self._recent_failures.append(fingerprint)
                if len(self._recent_failures) >= 3 and len(set(self._recent_failures[-3:])) == 1:
                    return AgentOutcome(
                        state=WorkflowState.BLOCKED,
                        summary="The same failing action was repeated three times.",
                        limitations=[
                            "No progress after three identical failed actions; stopped to avoid a loop."
                        ],
                        error=f"repeated action: {action.tool}",
                        steps=steps,
                        tool_calls=self.calls.count_by_run(self.run.id),
                        tokens=tokens,
                        cost_usd=cost,
                    )
            if result.status != ToolCallStatus.FAILED:
                self._consecutive_failures = 0
            if result.status == ToolCallStatus.TIMED_OUT and result.data.get("cancelled"):
                return AgentOutcome(
                    state=WorkflowState.CANCELLED,
                    summary="Cancellation interrupted the running command.",
                    error="cancelled",
                    steps=steps,
                    tool_calls=self.calls.count_by_run(self.run.id),
                    tokens=tokens,
                    cost_usd=cost,
                )
            if latest.pause_requested:
                return AgentOutcome(
                    state=WorkflowState.PAUSED,
                    summary="Pause was requested; the run stopped after the current action.",
                    steps=steps,
                    tool_calls=self.calls.count_by_run(self.run.id),
                    tokens=tokens,
                    cost_usd=cost,
                )

    # ----------------------------------------------------------------- context
    def _messages(self, *, last_error: str | None) -> list[ChatMessage]:
        run = self.runs.get(self.run.id)
        remaining_steps = max(0, self.contract.constraints.max_steps - run.step_count)
        remaining_tokens = max(0, self.contract.constraints.max_model_tokens - run.model_tokens)
        files = self.tools.policy.visible_paths(limit=250)
        observations = self._observations()
        payload = {
            "task": {
                "id": self.task.id,
                "title": self.task.title,
                "type": self.task.task_type.value,
                "goal": self.task.goal,
            },
            "contract": {
                "allowed_paths": self.contract.scope.allowed_paths,
                "excluded_paths": self.contract.scope.excluded_paths,
                "acceptance_criteria": self.contract.acceptance_criteria,
                "unknowns": self.contract.unknowns,
                "max_steps": self.contract.constraints.max_steps,
                "max_tool_calls": self.contract.constraints.max_tool_calls,
                "max_runtime_seconds": self.contract.constraints.max_runtime_seconds,
                "allow_file_deletion": self.contract.constraints.allow_file_deletion,
            },
            "plan": {
                "summary": self.plan.summary,
                "steps": [step.model_dump(mode="json") for step in self.plan.steps],
                "checks": self.plan.checks,
                "risks": self.plan.risks,
            },
            "workspace_files": files,
            "recent_observations": observations,
            "remaining_budget": {
                "steps": remaining_steps,
                "model_tokens": remaining_tokens,
            },
            "last_error": last_error,
        }
        return [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    "Here is the current task context as JSON. Propose the next "
                    "single action.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
                ),
            ),
        ]

    def _observations(self) -> list[dict[str, Any]]:
        calls = self.calls.list_by_run(self.run.id, limit=200)[-MAX_OBSERVATIONS:]
        results: list[dict[str, Any]] = []
        for call in calls:
            output = ""
            if call.output_artifact_id:
                try:
                    output = self.artifacts.read_text(call.output_artifact_id)
                except Exception:
                    output = "[artifact unavailable]"
            results.append(
                {
                    "tool": call.tool_name,
                    "arguments": call.arguments,
                    "status": call.status.value,
                    "summary": (call.details or {}).get("summary", ""),
                    "output": output[:MAX_OBSERVATION_CHARS],
                    "truncated": len(output) > MAX_OBSERVATION_CHARS,
                }
            )
        return results

    def _parse_action(self, structured: dict[str, Any] | None, text: str) -> AgentAction:
        payload = structured
        if payload is None:
            payload = _extract_json(text)
        if not isinstance(payload, dict):
            raise ValueError("model response was not a JSON object")
        return AgentAction.model_validate(payload)

    def _details(self, run: WorkflowRun | None = None) -> dict[str, Any]:
        current = run or self.runs.get(self.run.id)
        details = dict(current.details)
        details.pop("agent_last_error", None)
        return details


def _extract_json(text: str) -> dict[str, Any]:
    """Accept raw JSON or a fenced JSON block, and nothing more permissive."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model response")
    parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response was not a JSON object")
    return parsed


__all__ = ["ControlledAgent", "AgentOutcome", "SYSTEM_PROMPT"]

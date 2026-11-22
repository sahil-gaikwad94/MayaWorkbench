"""The bounded Implementer loop.

One model step proposes one typed tool action. Application code validates it,
executes it through the gateway, records the observation, and checks budgets and
control flags. The model cannot mutate workflow state, approve itself, change
scope, or call an unregistered tool.

Three properties make the loop a checker as well as a maker:

* **Grounded orientation.** The first model step receives spans retrieved for
  the task goal, the symbols defined in the paths it may edit, and the index
  issues that make a file unreliable evidence. It starts from evidence instead
  of guessing filenames.
* **A deterministic finish gate.** ``finish`` is a request, not a verdict. When
  the contract declares verification checks, application code runs them against
  the candidate workspace first. A real failure becomes typed repair feedback
  and the loop continues inside the same budgets. Infrastructure failures are
  attributed to the checker, not the code, and stop the repair loop.
* **A durable ledger.** Files read, files changed, commands run and checks
  executed are reconstructed from the tool-call records, so a paused/resumed run
  or a compacted context still knows what it already did.

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
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
    ToolCallStatus,
    WorkflowState,
)
from mayaworkbench.domain.models import (
    Task,
    TaskContract,
    TaskPlan,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import BudgetError, ModelError, PolicyError, ToolError
from mayaworkbench.ids import new_ulid
from mayaworkbench.models.base import ChatMessage, ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import EventStore, RunStore, ToolCallStore
from mayaworkbench.tasks.agent_models import AgentAction, FinishArgs
from mayaworkbench.tasks.budget import BudgetController
from mayaworkbench.tasks.inloop import READ_ONLY_TOOLS, FeedbackRound, repair_payload
from mayaworkbench.workspace.tools import ToolGateway

MAX_OBSERVATIONS = 10
MAX_OBSERVATION_CHARS = 5000
MAX_LEDGER_READS = 24
MAX_LEDGER_COMMANDS = 12
#: Orientation is a starting aid, not a standing prompt tax: after the first
#: couple of steps the durable ledger and observations are the better context.
ORIENTATION_STEPS = 2

SYSTEM_PROMPT = """You are the Implementer role in MayaWorkbench, a controlled engineering agent.

You work only through the provided tools in an isolated Git worktree. The user's
original checkout is never your write target.

Hard rules:
- Read a file before changing it. Apply patches with exact, current context.
- Write only inside the contract's allowed paths. Never touch excluded paths,
  credentials, .git, or files outside the workspace.
- Do not weaken, delete or skip tests to make a command pass. If a test is wrong,
  explain why and propose the smallest honest change.
- Run only commands provided through run_command, or checks declared in the
  contract through run_check. Commands run without a shell. Do not attempt
  dependency installation or network access.
- Never claim a check passed unless a command observation shows exit code 0.
  Distinguish implemented from verified.
- Do not fabricate file contents, command output, line numbers or test results.
- If blocked by missing information or an environmental limitation, finish and
  say so in limitations rather than guessing.
- Call finish when the task is done or cannot proceed. The summary must describe
  what changed, what was actually checked, and what remains unverified.

`finish` is a request to finish, not a verdict. When the contract declares
verification checks, the application runs them before answering. A real failure
returns a REPAIR_REQUIRED message with the failing check, its output and the
failing test ids; fix the cause, then finish again. If the check itself is
broken (no tests collected, usage error, internal error) that is reported as an
infrastructure failure: repairing code will not help, so explain it and finish.
You cannot edit, skip or waive a declared check.

The repository_orientation in the first message describes the pinned base
revision. Read the live worktree before editing; it may already differ.

Return exactly one action as structured JSON: a short thought, one tool name,
and its arguments. Tool names and arguments:
- read_file {path, start_line?, end_line?}
- list_files {path?, depth?}
- search_text {query, path?, limit?}
- find_symbol {query, limit?}  # pinned index; the worktree may be newer
- apply_patch {patch}  # unified diff with ---/+++ and @@ hunks
- write_file {path, content, overwrite?}
- delete_file {path}  # only if the contract enables deletion
- git_status {}
- git_diff {}
- run_command {argv: [executable, ...], timeout_seconds?}
- run_check {key}  # only keys declared in the contract
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


@dataclass
class FinishDecision:
    """Either the loop stops, or it continues with a repair instruction."""

    outcome: AgentOutcome | None = None
    message: str | None = None


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
        orientation: dict[str, Any] | None = None,
        events: EventStore | None = None,
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
        self.orientation = orientation
        self.events = events
        self._invalid_actions = 0
        self._last_parse_errors: list[str] = []
        self._recent_failures: list[str] = []
        self._consecutive_failures = 0
        self._check_rounds = 0
        self._finish_requested = False
        self._pending_repair: dict[str, Any] | None = None
        self._last_signature: str | None = None
        self._repeat_count = 0
        self._started = time.monotonic()
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
                # A provider that dies after the model already requested finish
                # must not erase the candidate patch. Settle as REVIEWING with a
                # typed limitation; the authoritative verifier decides the
                # outcome from deterministic evidence.
                if getattr(self, "_finish_requested", False):
                    self.runs.update_progress(
                        self.run.id,
                        limitations=list(getattr(self.run, "limitations", ())) + [
                            "IN_LOOP_FAILED: the model provider failed after finish was "
                            "requested; in-loop checks are inconclusive."
                        ],
                        details={
                            **self._details(),
                            "in_loop_verdict": "ERROR",
                            "provider_failure_after_finish": str(exc)[:400],
                        },
                    )
                    return AgentOutcome(
                        state=WorkflowState.REVIEWING,
                        summary=(
                            "Finish was requested, but the model provider failed before "
                            "the in-loop check round completed."
                        ),
                        limitations=[
                            "IN_LOOP_FAILED: in-loop verification is inconclusive; "
                            "`maya verify run` is authoritative."
                        ],
                        error=str(exc),
                        steps=steps,
                        tool_calls=current.tool_call_count,
                        tokens=tokens,
                        cost_usd=cost,
                    )
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
                decision = self._decide_finish(
                    finish, steps=steps, tokens=tokens, cost=cost
                )
                if decision.outcome is not None:
                    return decision.outcome
                last_error = decision.message
                continue

            signature = f"{action.tool}:{json.dumps(action.arguments, sort_keys=True, default=str)}"
            if (
                action.tool in READ_ONLY_TOOLS
                and signature == self._last_signature
                and self._repeat_count >= 1
            ):
                self._repeat_count += 1
                last_error = (
                    "You repeated the same read-only action with identical arguments. "
                    "Its observation is already in the ledger above; it will not change. "
                    "Take a different action or finish with an honest summary."
                )
                if self._repeat_count >= 4:
                    return AgentOutcome(
                        state=WorkflowState.BLOCKED,
                        summary="The same read-only action was repeated with no progress.",
                        limitations=[
                            "No progress after repeated identical read-only actions; "
                            "stopped to avoid a loop.",
                        ],
                        error=f"repeated action: {action.tool}",
                        steps=steps,
                        tool_calls=self.calls.count_by_run(self.run.id),
                        tokens=tokens,
                        cost_usd=cost,
                    )
                continue

            try:
                result = self.tools.invoke(action.tool, action.arguments)
            except ToolError as exc:
                # A model can emit an argument the tool schema rejects (a path
                # containing '..', a negative line, an oversized limit). That is
                # a recoverable mistake: feed it back and let the model correct
                # itself instead of ending the whole run on the first typo.
                self._invalid_actions += 1
                last_error = (
                    f"Invalid tool arguments ({self._invalid_actions}/3): {exc}"
                )
                if self._invalid_actions >= 3:
                    return AgentOutcome(
                        state=WorkflowState.FAILED,
                        summary="The model repeatedly produced invalid tool arguments.",
                        limitations=["No valid tool call was produced after three attempts."],
                        error=last_error,
                        steps=steps,
                        tool_calls=self.calls.count_by_run(self.run.id),
                        tokens=tokens,
                        cost_usd=cost,
                    )
                self.runs.update_progress(
                    self.run.id,
                    details={**self._details(), "agent_last_error": last_error},
                )
                continue
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
            if result.status == ToolCallStatus.SUCCEEDED:
                if signature == self._last_signature:
                    self._repeat_count += 1
                else:
                    self._last_signature = signature
                    self._repeat_count = 0
            else:
                self._last_signature = None
                self._repeat_count = 0
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

    # ----------------------------------------------------------------- finish
    def _decide_finish(
        self,
        finish: FinishArgs,
        *,
        steps: int,
        tokens: int,
        cost: float,
    ) -> FinishDecision:
        """Honour a finish request only after the contract's checks have spoken.

        This is the only place where a model request can turn into REVIEWING.
        It never turns a failure into a pass: it either converts the failure into
        a bounded repair round or records it as an explicit limitation.
        """
        # Remember that the model asked to finish: if the provider fails on a
        # subsequent repair turn, the run must settle as REVIEWING with an
        # explicit in-loop error, never as a silent FAILED with no evidence.
        self._finish_requested = True
        runner = getattr(self.tools, "check_runner", None)
        specs = list(getattr(runner, "specs", []) or []) if runner is not None else []
        if runner is None or not specs:
            return FinishDecision(
                outcome=self._accept_finish(finish, steps=steps, tokens=tokens, cost=cost)
            )
        if not getattr(runner, "authorized", False):
            limitations = list(finish.limitations) + [
                "Declared verification checks could not run in-loop because command "
                "execution was not authorized; `maya verify run` will report them as BLOCKED."
            ]
            return FinishDecision(
                outcome=self._accept_finish(
                    finish,
                    steps=steps,
                    tokens=tokens,
                    cost=cost,
                    extra_limitations=limitations,
                    verdict="NOT_AUTHORIZED",
                )
            )
        max_repairs = self.contract.constraints.max_repair_attempts
        self._check_rounds += 1
        remaining = max(
            1,
            int(self.contract.constraints.max_runtime_seconds - (time.monotonic() - self._started)),
        )
        round_result = runner.run(deadline_seconds=remaining)
        self._record_check_round(round_result, round_number=self._check_rounds)
        if round_result.passed:
            limitations = list(finish.limitations) + [
                f"In-loop declared checks passed (round {self._check_rounds}). `maya verify run` "
                "re-executes them from scratch and remains the authoritative evidence."
            ]
            return FinishDecision(
                outcome=self._accept_finish(
                    finish,
                    steps=steps,
                    tokens=tokens,
                    cost=cost,
                    extra_limitations=limitations,
                    verdict="PASSED",
                )
            )
        if round_result.failures and self._check_rounds <= max_repairs:
            self._pending_repair = repair_payload(
                round_result,
                round_number=self._check_rounds,
                max_rounds=max_repairs + 1,
            )
            self._event(
                EventType.AGENT_REPAIR_REQUESTED,
                payload={
                    "round": self._check_rounds,
                    "max_rounds": max_repairs + 1,
                    "failed_checks": [item.key for item in round_result.failures],
                    "infrastructure_checks": [
                        item.key for item in round_result.infrastructure_failures
                    ],
                },
            )
            failing = ", ".join(item.key for item in round_result.failures)
            return FinishDecision(
                message=(
                    f"Declared check(s) failed in-loop: {failing}. A REPAIR_REQUIRED "
                    "message with the output is included in the context. Fix the cause, then finish."
                )
            )
        limitations = list(finish.limitations)
        for item in round_result.failures:
            limitations.append(f"Declared check '{item.key}' failed in-loop: {item.reason}")
        for item in round_result.infrastructure_failures:
            limitations.append(
                f"Declared check '{item.key}' produced no verdict in-loop: {item.reason}"
            )
        if not round_result.results:
            limitations.append("No declared check produced a result in-loop.")
        limitations.append(
            "In-loop verification did not pass; `maya verify run` is required and will report it."
        )
        verdict = round_result.verdict
        return FinishDecision(
            outcome=self._accept_finish(
                finish,
                steps=steps,
                tokens=tokens,
                cost=cost,
                extra_limitations=limitations,
                verdict=verdict,
            )
        )

    def _accept_finish(
        self,
        finish: FinishArgs,
        *,
        steps: int,
        tokens: int,
        cost: float,
        extra_limitations: list[str] | None = None,
        verdict: str = "NOT_RUN",
    ) -> AgentOutcome:
        limitations = list(finish.limitations) + list(extra_limitations or []) + [
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
                "in_loop_verdict": verdict,
                "in_loop_rounds": self._check_rounds,
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

    # ------------------------------------------------------------------ checks
    def _record_check_round(self, round_result: FeedbackRound, *, round_number: int) -> None:
        payload = {
            "in_loop": True,
            "round": round_number,
            "max_rounds": self.contract.constraints.max_repair_attempts + 1,
            "verdict": round_result.verdict,
            **round_result.to_payload(),
            "note": (
                "Advisory in-loop feedback. `maya verify run` re-executes every declared "
                "check and produces the authoritative evidence."
            ),
        }
        try:
            artifact = self.artifacts.put_text(
                json.dumps(payload, indent=2, sort_keys=True),
                kind=ArtifactKind.VERIFICATION_CHECK,
                media_type="application/json",
                created_by="agent:in_loop_check",
            )
            artifact_id = artifact.id
        except Exception:  # noqa: BLE001 - evidence capture must not break the loop
            artifact_id = None
        run = self.runs.get(self.run.id)
        rounds = list(run.details.get("in_loop_check_rounds", []))
        rounds.append(
            {
                "round": round_number,
                "verdict": payload["verdict"],
                "duration_ms": round_result.duration_ms,
                "checks": [
                    {"key": item.key, "outcome": item.outcome, "infrastructure": item.infrastructure}
                    for item in round_result.results
                ],
                "artifact_id": artifact_id,
            }
        )
        self.runs.update_progress(
            self.run.id,
            details={
                **self._details(run),
                "in_loop_check_rounds": rounds[-5:],
                "in_loop_verdict": payload["verdict"],
            },
        )
        self._event(
            EventType.AGENT_CHECK_COMPLETED,
            payload={
                "round": round_number,
                "verdict": payload["verdict"],
                "duration_ms": round_result.duration_ms,
                "checks": [
                    {"key": item.key, "outcome": item.outcome}
                    for item in round_result.results
                ],
                "artifact_id": artifact_id,
            },
        )

    # ----------------------------------------------------------------- context
    def _messages(self, *, last_error: str | None) -> list[ChatMessage]:
        run = self.runs.get(self.run.id)
        remaining_steps = max(0, self.contract.constraints.max_steps - run.step_count)
        remaining_tokens = max(0, self.contract.constraints.max_model_tokens - run.model_tokens)
        files = self.tools.policy.visible_paths(limit=250)
        observations = self._observations()
        payload: dict[str, Any] = {
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
                "declared_checks": [
                    {"key": item.key, "title": item.title, "gate": item.gate.value}
                    for item in self.contract.verification_checks
                ],
            },
            "plan": {
                "summary": self.plan.summary,
                "steps": [step.model_dump(mode="json") for step in self.plan.steps],
                "checks": self.plan.checks,
                "risks": self.plan.risks,
            },
            "workspace_files": files,
            "workspace_ledger": self._ledger(),
            "recent_observations": observations,
            "remaining_budget": {
                "steps": remaining_steps,
                "model_tokens": remaining_tokens,
                "repair_rounds": max(
                    0,
                    self.contract.constraints.max_repair_attempts + 1 - self._check_rounds,
                ),
            },
            "last_error": last_error,
        }
        if self._pending_repair is not None:
            payload["repair_required"] = self._pending_repair
        if self.orientation and run.step_count < ORIENTATION_STEPS:
            payload["repository_orientation"] = self.orientation
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

    def _ledger(self) -> dict[str, Any]:
        """Reconstruct working memory from durable tool-call records.

        Older observations fall out of the prompt window; the ledger does not.
        It is derived, not stored, so it survives a pause/resume and cannot drift
        from the audited call log.
        """
        calls = self.calls.list_by_run(self.run.id, limit=200)
        reads: dict[str, dict[str, Any]] = {}
        changes: dict[str, dict[str, Any]] = {}
        commands: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        read_pos: dict[str, int] = {}
        change_pos: dict[str, int] = {}
        for index, call in enumerate(calls):
            args = call.arguments or {}
            tool = call.tool_name
            if tool == "read_file" and args.get("path"):
                path = str(args["path"])
                entry = reads.setdefault(
                    path, {"path": path, "reads": 0, "ranges": []}
                )
                entry["reads"] += 1
                if call.status == ToolCallStatus.SUCCEEDED:
                    entry["ranges"].append(
                        [args.get("start_line") or 1, args.get("end_line") or 0]
                    )
                    entry["ranges"] = entry["ranges"][-4:]
                    read_pos[path] = index
            elif tool == "apply_patch":
                for path in args.get("files", []) or []:
                    changes[str(path)] = {"path": str(path), "tool": tool}
                    change_pos[str(path)] = index
            elif tool in {"write_file", "delete_file"} and args.get("path"):
                path = str(args["path"])
                changes[path] = {"path": path, "tool": tool}
                change_pos[path] = index
            elif tool == "run_command":
                commands.append(
                    {
                        "argv": args.get("argv", [])[:6],
                        "exit_code": call.exit_code,
                        "status": call.status.value,
                    }
                )
                commands = commands[-MAX_LEDGER_COMMANDS:]
            elif tool == "run_check":
                checks.append(
                    {
                        "key": args.get("key"),
                        "status": call.status.value,
                        "exit_code": call.exit_code,
                    }
                )
                checks = checks[-8:]
        changed_not_reread = [
            path
            for path in changes
            if read_pos.get(path, -1) < change_pos.get(path, -1)
        ]
        return {
            "files_read": list(reads.values())[-MAX_LEDGER_READS:],
            "files_changed": list(changes.values()),
            "files_changed_not_reread": changed_not_reread[:10],
            "commands": commands,
            "declared_checks_run": checks,
            "note": (
                "Derived from the audited tool-call log. A change not re-read afterwards "
                "has not been reviewed by you."
            ),
        }

    def _parse_action(self, structured: dict[str, Any] | None, text: str) -> AgentAction:
        from mayaworkbench.tasks.llm_capabilities import repair_structured_output
        from mayaworkbench.tasks.agent_models import AgentAction

        if structured is not None:
            return AgentAction.model_validate(structured)
        action, errors = repair_structured_output(text, AgentAction, retry=None)
        self._last_parse_errors = errors
        if action is None:
            raise ValueError("; ".join(errors) or "invalid model output")
        return action

    def _details(self, run: WorkflowRun | None = None) -> dict[str, Any]:
        current = run or self.runs.get(self.run.id)
        details = dict(current.details)
        details.pop("agent_last_error", None)
        if self._last_parse_errors:
            details["agent_parse_errors"] = list(self._last_parse_errors[-5:])
        return details

    def _event(self, event_type: EventType, *, payload: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=event_type,
                    project_id=self.run.project_id,
                    repository_id=self.run.repository_id,
                    snapshot_id=self.run.snapshot_id,
                    task_id=self.run.task_id,
                    run_id=self.run.id,
                    entity_type="run",
                    entity_id=self.run.id,
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="agent_loop",
                    occurred_at=utcnow(),
                    payload=payload,
                )
            )
        except Exception:  # noqa: BLE001 - an audit write must not stop the loop
            return


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


__all__ = ["ControlledAgent", "AgentOutcome", "SYSTEM_PROMPT", "ORIENTATION_STEPS"]

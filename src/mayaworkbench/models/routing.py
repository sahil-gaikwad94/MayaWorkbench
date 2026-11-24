"""Deterministic model routing, health history and provider fallback (WP7).

Routing is policy owned by the workbench. The model is a *proposer*: it suggests
answers and tool calls, but a route, a budget, the data policy and the connector
allowlist decide what may actually happen. Every decision records its reasons,
its rejections and the deterministic companions that must corroborate the model.

The module has no I/O except an optional, explicitly-supplied health history
file used by the CLI; that file stores provider names, outcomes, latencies and
error classes only — never keys, prompts or payloads.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.errors import MayaError, ModelError
from mayaworkbench.models.base import ModelCapabilities, ModelResponse
from mayaworkbench.models.capabilities import TaskType
from mayaworkbench.models.local_openai_compatible import (
    LOCAL_OPENAI_COMPATIBLE,
    is_local_host,
)

DataPolicyLiteral = Literal["LOCAL_ONLY", "BYOK_REMOTE_MODEL"]
ProviderTier = Literal["fast", "reliable", "reasoning", "long_context"]

_MAX_HEALTH_EVENTS = 32
_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{6,})")


class RoutingError(MayaError):
    """No provider could be selected or all fallback attempts failed."""

    code = "ROUTING_ERROR"
    exit_code = 3

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        rejected: dict[str, str] | None = None,
        failures: list[dict[str, str]] | None = None,
    ):
        super().__init__(message, hint=hint)
        self.rejected = dict(rejected or {})
        self.failures = list(failures or [])


# --------------------------------------------------------------------------- #
# Configuration records                                                        #
# --------------------------------------------------------------------------- #
class ProviderSpec(BaseModel):
    """A configured provider as seen by routing. Contains no secret values."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str
    mode: str = "openai_compatible"
    local: bool = False
    priority: int = 100
    tier: ProviderTier = "reliable"
    capabilities: ModelCapabilities
    max_context_tokens: int | None = None
    cost_per_mtok: float | None = None
    key_env: str | None = None
    key_present: bool = False


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: TaskType = "general"
    context_tokens: int = Field(default=0, ge=0)
    uncertain: bool = False
    token_budget: int = Field(default=8000, ge=1)
    data_policy: DataPolicyLiteral = "LOCAL_ONLY"
    require_structured_output: bool = False


class RoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    mode: str
    task_type: str
    reasons: list[str]
    rejected: dict[str, str] = Field(default_factory=dict)
    candidates: list[str] = Field(default_factory=list)
    deterministic_companions: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    token_budget: int
    context_tokens: int
    uncertain: bool
    data_policy: str
    policy_authority: Literal["workbench_policy"] = "workbench_policy"
    model_is_authority: Literal[False] = False


# --------------------------------------------------------------------------- #
# Health                                                                       #
# --------------------------------------------------------------------------- #
class HealthEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["success", "failure"]
    latency_ms: int = Field(default=0, ge=0)
    error_class: str | None = None


@dataclass
class ProviderHealth:
    """Bounded failure/latency history for one provider."""

    name: str
    events: list[HealthEvent] = field(default_factory=list)

    def record_success(self, latency_ms: int) -> None:
        self._append(HealthEvent(outcome="success", latency_ms=max(0, int(latency_ms))))

    def record_failure(self, error_class: str, latency_ms: int = 0) -> None:
        self._append(
            HealthEvent(
                outcome="failure",
                latency_ms=max(0, int(latency_ms)),
                error_class=(error_class or "unknown")[:60],
            )
        )

    def _append(self, event: HealthEvent) -> None:
        self.events.append(event)
        if len(self.events) > _MAX_HEALTH_EVENTS:
            self.events = self.events[-_MAX_HEALTH_EVENTS:]

    def failure_count(self) -> int:
        return sum(1 for event in self.events if event.outcome == "failure")

    def success_count(self) -> int:
        return sum(1 for event in self.events if event.outcome == "success")

    def consecutive_failures(self) -> int:
        count = 0
        for event in reversed(self.events):
            if event.outcome != "failure":
                break
            count += 1
        return count

    def average_latency_ms(self) -> int:
        latencies = [event.latency_ms for event in self.events if event.outcome == "success"]
        if not latencies:
            return 0
        return int(sum(latencies) / len(latencies))

    def failure_rate(self) -> float:
        if not self.events:
            return 0.0
        return round(self.failure_count() / len(self.events), 4)

    def available(
        self, *, max_consecutive_failures: int = 2, max_avg_latency_ms: int | None = None
    ) -> bool:
        if self.consecutive_failures() >= max_consecutive_failures:
            return False
        if max_avg_latency_ms is not None and self.average_latency_ms() > max_avg_latency_ms:
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "observations": len(self.events),
            "successes": self.success_count(),
            "failures": self.failure_count(),
            "consecutive_failures": self.consecutive_failures(),
            "average_latency_ms": self.average_latency_ms(),
            "failure_rate": self.failure_rate(),
            "last_error_class": next(
                (
                    event.error_class
                    for event in reversed(self.events)
                    if event.outcome == "failure" and event.error_class
                ),
                None,
            ),
            "history": [event.model_dump(mode="json") for event in self.events],
        }


class ProviderHealthTracker:
    """In-process health state with an optional JSON history file for the CLI."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        max_consecutive_failures: int = 2,
        max_avg_latency_ms: int | None = None,
    ):
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        self.path = Path(path) if path is not None else None
        self.max_consecutive_failures = int(max_consecutive_failures)
        self.max_avg_latency_ms = max_avg_latency_ms
        self._records: dict[str, ProviderHealth] = {}
        if self.path is not None and self.path.exists():
            self._load()

    def get(self, name: str) -> ProviderHealth:
        if name not in self._records:
            self._records[name] = ProviderHealth(name=name)
        return self._records[name]

    def record_success(self, name: str, latency_ms: int) -> None:
        self.get(name).record_success(latency_ms)
        self._persist()

    def record_failure(self, name: str, error_class: str, latency_ms: int = 0) -> None:
        self.get(name).record_failure(error_class, latency_ms)
        self._persist()

    def available(self, name: str) -> bool:
        return self.get(name).available(
            max_consecutive_failures=self.max_consecutive_failures,
            max_avg_latency_ms=self.max_avg_latency_ms,
        )

    def unavailability_reason(self, name: str) -> str:
        health = self.get(name)
        failures = health.consecutive_failures()
        if failures >= self.max_consecutive_failures:
            return f"{failures} consecutive failures recorded"
        if (
            self.max_avg_latency_ms is not None
            and health.average_latency_ms() > self.max_avg_latency_ms
        ):
            return (
                f"average latency {health.average_latency_ms()}ms exceeds "
                f"{self.max_avg_latency_ms}ms"
            )
        return ""

    def snapshot(self, name: str | None = None) -> dict[str, Any]:
        names = [name] if name is not None else sorted(self._records)
        return {
            "thresholds": {
                "max_consecutive_failures": self.max_consecutive_failures,
                "max_avg_latency_ms": self.max_avg_latency_ms,
            },
            "providers": {item: self.get(item).snapshot() for item in names},
        }

    # ---------------------------------------------------------------- storage
    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "providers": {name: health.snapshot() for name, health in sorted(self._records.items())},
        }

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, json.JSONDecodeError):
            return
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            return
        for name, raw in providers.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            health = ProviderHealth(name=name)
            for event in raw.get("history", [])[-_MAX_HEALTH_EVENTS:]:
                if not isinstance(event, dict) or event.get("outcome") not in ("success", "failure"):
                    continue
                try:
                    latency = max(0, int(event.get("latency_ms", 0)))
                except (TypeError, ValueError):
                    continue
                error_class = event.get("error_class")
                health.events.append(
                    HealthEvent(
                        outcome=event["outcome"],
                        latency_ms=latency,
                        error_class=str(error_class)[:60] if error_class else None,
                    )
                )
            self._records[name] = health

    def _persist(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:  # pragma: no cover - health history is best effort
            pass


# --------------------------------------------------------------------------- #
# Policy tables                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _TaskProfile:
    tiers: tuple[ProviderTier, ...]
    companions: tuple[str, ...]
    long_context: bool = False
    separate_context: bool = False


_PROFILES: dict[str, _TaskProfile] = {
    "general": _TaskProfile(("reliable", "fast"), ("tests",)),
    "repository_summarisation": _TaskProfile(
        ("fast", "reliable"), ("ast_parser", "exact_retrieval")
    ),
    "architecture_design": _TaskProfile(
        ("reasoning", "reliable"), ("dependency_graph", "build_metadata")
    ),
    "routine_code_edit": _TaskProfile(("reliable", "fast"), ("patch_validator", "tests")),
    "paper_reasoning": _TaskProfile(
        ("long_context", "reasoning"), ("pdf_extraction", "citation_checks"), long_context=True
    ),
    "research_search_synthesis": _TaskProfile(
        ("reliable", "fast"), ("source_registry", "quote_validation")
    ),
    "code_review": _TaskProfile(
        ("reasoning", "reliable"),
        ("static_analysis", "type_checks", "secret_scan", "tests"),
        separate_context=True,
    ),
    "result_analysis": _TaskProfile(
        ("reasoning", "reliable"), ("statistics", "manifest_parity")
    ),
    "embedding_retrieval": _TaskProfile(
        ("fast", "reliable"), ("hashing_embedding", "lexical_search_fallback", "reranker")
    ),
}


@dataclass
class _Evaluation:
    decision: RoutingDecision | None
    error: str | None
    steps: list[str]
    rejected: dict[str, str]


def _safe_message(exc: BaseException) -> str:
    message = _SECRET_PATTERN.sub("[REDACTED]", str(exc))
    return message[:300]


# --------------------------------------------------------------------------- #
# Router                                                                       #
# --------------------------------------------------------------------------- #
class ModelRouter:
    """Select a provider deterministically from an explicit request."""

    def __init__(
        self,
        providers: Sequence[ProviderSpec],
        *,
        data_policy: DataPolicyLiteral = "LOCAL_ONLY",
        token_budget: int = 8000,
        health: ProviderHealthTracker | None = None,
        max_consecutive_failures: int = 2,
        max_avg_latency_ms: int | None = None,
    ):
        self.providers = list(providers)
        self.data_policy: DataPolicyLiteral = data_policy
        self.token_budget = int(token_budget)
        self.health = health or ProviderHealthTracker(
            max_consecutive_failures=max_consecutive_failures,
            max_avg_latency_ms=max_avg_latency_ms,
        )
        self._by_name = {spec.name: spec for spec in self.providers}
        if len(self._by_name) != len(self.providers):
            raise RoutingError("Provider names must be unique")

    # ------------------------------------------------------------------ policy
    def evaluate(self, request: RouteRequest) -> _Evaluation:
        profile = _PROFILES[request.task_type]
        steps = [
            f"task_type={request.task_type}: preferred tiers {', '.join(profile.tiers)}",
            f"data_policy={request.data_policy}; remote providers are "
            + ("permitted" if request.data_policy == "BYOK_REMOTE_MODEL" else "refused"),
            f"token_budget={request.token_budget}; context_tokens={request.context_tokens}",
        ]
        rejected: dict[str, str] = {}

        if request.context_tokens > min(request.token_budget, self.token_budget):
            reason = (
                f"context_tokens={request.context_tokens} exceeds budget "
                f"{min(request.token_budget, self.token_budget)}"
            )
            steps.append("REFUSED: " + reason)
            return _Evaluation(None, reason, steps, rejected)

        eligible: list[tuple[float, ProviderSpec]] = []
        for spec in self.providers:
            local = spec.local or spec.mode == LOCAL_OPENAI_COMPATIBLE
            if request.data_policy == "LOCAL_ONLY" and not local:
                rejected[spec.name] = "data_policy LOCAL_ONLY forbids a remote provider"
                continue
            reason = self.health.unavailability_reason(spec.name)
            if reason:
                rejected[spec.name] = "provider health: " + reason
                continue
            if spec.max_context_tokens is not None and request.context_tokens > spec.max_context_tokens:
                rejected[spec.name] = (
                    f"context {request.context_tokens} exceeds model limit "
                    f"{spec.max_context_tokens}"
                )
                continue
            if request.require_structured_output and not spec.capabilities.structured_output:
                rejected[spec.name] = "structured output not supported"
                continue
            score = self._score(spec, request, profile)
            eligible.append((score, spec))

        for name in sorted(rejected):
            steps.append(f"rejected {name}: {rejected[name]}")

        if not eligible:
            reason = "no eligible provider for this request"
            steps.append("REFUSED: " + reason)
            return _Evaluation(None, reason, steps, rejected)

        eligible.sort(key=lambda pair: (-round(pair[0], 6), pair[1].priority, pair[1].name))
        score, selected = eligible[0]
        steps.append(
            f"selected {selected.name}/{selected.model} (tier={selected.tier}, score={score:.2f})"
        )
        if len(eligible) > 1:
            steps.append(
                "fallback chain: " + " -> ".join(spec.name for _score, spec in eligible)
            )
        if request.uncertain:
            steps.append(
                "uncertain=true: prefer reasoning tier and require deterministic verification"
            )
        if profile.separate_context:
            steps.append("code_review: use a separate review context")
        steps.append("deterministic companions: " + ", ".join(profile.companions))
        steps.append("model output is a proposal; workbench policy authorises every action")

        decision = RoutingDecision(
            provider=selected.name,
            model=selected.model,
            mode=selected.mode,
            task_type=request.task_type,
            reasons=steps,
            rejected=dict(sorted(rejected.items())),
            candidates=[spec.name for _score, spec in eligible],
            deterministic_companions=list(profile.companions),
            token_budget=request.token_budget,
            context_tokens=request.context_tokens,
            uncertain=request.uncertain,
            data_policy=request.data_policy,
        )
        return _Evaluation(decision, None, steps, rejected)

    @staticmethod
    def _score(spec: ProviderSpec, request: RouteRequest, profile: _TaskProfile) -> float:
        score = 0.0
        for index, tier in enumerate(profile.tiers):
            if spec.tier == tier:
                score += (len(profile.tiers) - index) * 10.0
        if request.uncertain and spec.tier in ("reasoning", "long_context"):
            score += 15.0
        if profile.long_context and spec.max_context_tokens and spec.max_context_tokens >= 32768:
            score += 10.0
        if spec.cost_per_mtok is not None:
            score -= min(spec.cost_per_mtok, 20.0) / 10.0
        score -= spec.priority / 1000.0
        return round(score, 6)

    # ------------------------------------------------------------------- calls
    def route(self, request: RouteRequest) -> RoutingDecision:
        evaluation = self.evaluate(request)
        if evaluation.decision is None:
            raise RoutingError(
                evaluation.error or "no route available",
                hint="Adjust task type, token budget or data policy, or configure a provider.",
                rejected=evaluation.rejected,
            )
        return evaluation.decision

    def invoke(
        self,
        request: RouteRequest,
        call: Callable[[ProviderSpec], ModelResponse],
    ) -> "InvocationOutcome":
        """Call the selected provider, then fall back on failure.

        ``call`` is injected so tests use fakes and never touch the network.
        Failures are recorded in health history; the returned decision reports
        whether a secondary provider served the request.
        """
        decision = self.route(request)
        attempted: list[str] = []
        failures: list[dict[str, str]] = []
        for name in decision.candidates:
            spec = self._by_name[name]
            attempted.append(name)
            started = time.monotonic()
            try:
                response = call(spec)
            except BaseException as exc:  # noqa: BLE001 - every failure is recorded
                error_class = exc.error_class if isinstance(exc, ModelError) else type(exc).__name__
                self.health.record_failure(
                    name, error_class=error_class, latency_ms=int((time.monotonic() - started) * 1000)
                )
                failures.append(
                    {
                        "provider": name,
                        "error_class": error_class,
                        "message": _safe_message(exc),
                    }
                )
                continue
            self.health.record_success(
                name, latency_ms=int((time.monotonic() - started) * 1000)
            )
            updated = decision.model_copy(
                update={
                    "provider": name,
                    "model": spec.model,
                    "mode": spec.mode,
                    "fallback_used": name != decision.candidates[0],
                }
            )
            return InvocationOutcome(
                response=response,
                decision=updated,
                attempted=attempted,
                failures=failures,
            )
        raise RoutingError(
            "All configured providers failed for this request",
            hint="Inspect `maya model health` and the recorded error classes.",
            failures=failures,
        )


@dataclass
class InvocationOutcome:
    response: ModelResponse
    decision: RoutingDecision
    attempted: list[str]
    failures: list[dict[str, str]]


def explain_route(request: RouteRequest, router: ModelRouter | None = None) -> dict[str, Any]:
    """Deterministic, CLI-friendly explanation of a routing outcome.

    Same inputs always produce the same JSON (no timestamps or random values),
    and no secret can appear because the request and specs carry only names.
    """
    active = router or default_router()
    evaluation = active.evaluate(request)
    payload: dict[str, Any] = {
        "eligible": evaluation.decision is not None,
        "request": request.model_dump(mode="json"),
        "decision": evaluation.decision.model_dump(mode="json")
        if evaluation.decision is not None
        else None,
        "rejected": dict(sorted(evaluation.rejected.items())),
        "steps": evaluation.steps,
        "reason": evaluation.error,
        "policy_authority": "workbench_policy",
        "model_can_authorise_side_effects": False,
    }
    return payload


# --------------------------------------------------------------------------- #
# Settings bridge                                                              #
# --------------------------------------------------------------------------- #
def provider_specs_from_settings(settings: Any) -> list[ProviderSpec]:
    """Build non-secret provider specs from settings and WP7 env overrides."""
    from mayaworkbench.config import api_key_from_env

    base_url = settings.model.base_url
    primary_local = is_local_host(base_url)
    kind = getattr(settings.model, "provider", "openai_compatible")
    mode = {
        "anthropic": "anthropic",
        "gemini": "gemini",
    }.get(kind, LOCAL_OPENAI_COMPATIBLE if primary_local else "openai_compatible")
    from mayaworkbench.config import provider_key_env

    key_env = provider_key_env(settings)
    # Every shipped adapter implements streaming, structured output and tool
    # calls; only OpenAI-compatible adapters expose multiple parallel calls.
    specs = [
        ProviderSpec(
            name="primary",
            model=settings.model.model or "<unset>",
            mode=mode,
            local=primary_local,
            priority=10,
            tier="reliable",
            capabilities=ModelCapabilities(
                structured_output=True,
                streaming=kind != "none",
                tool_calling=True,
                parallel_tool_calls=kind in {"openai_compatible", "local_openai_compatible"},
                prompt_caching=kind == "anthropic",
                provider_family=mode,
            ),
            cost_per_mtok=settings.model.output_cost_per_mtok,
            key_env=key_env,
            key_present=bool(os.environ.get(key_env)),
        )
    ]
    fallback_model = os.environ.get("MAYA_FALLBACK_MODEL_NAME")
    if fallback_model:
        fallback_url = os.environ.get("MAYA_FALLBACK_MODEL_BASE_URL", "https://api.openai.com/v1")
        fallback_local = is_local_host(fallback_url)
        fallback_env = os.environ.get("MAYA_FALLBACK_MODEL_API_KEY_ENV", "MAYA_FALLBACK_MODEL_API_KEY")
        specs.append(
            ProviderSpec(
                name="secondary",
                model=fallback_model,
                mode=LOCAL_OPENAI_COMPATIBLE if fallback_local else "openai_compatible",
                local=fallback_local,
                priority=20,
                tier="fast" if fallback_local else "reasoning",
                capabilities=ModelCapabilities(
                    structured_output=True,
                    streaming=True,
                    tool_calling=True,
                    parallel_tool_calls=True,
                ),
                key_env=fallback_env,
                key_present=bool(os.environ.get(fallback_env)),
            )
        )
    return specs


def default_router(
    settings: Any | None = None,
    *,
    health_path: Path | None = None,
) -> ModelRouter:
    """Construct the router used by the CLI (no network, no key access)."""
    if settings is None:
        from mayaworkbench.config import AppPaths, load_settings

        settings = load_settings(AppPaths.resolve())
    return ModelRouter(
        provider_specs_from_settings(settings),
        data_policy=settings.data_policy,
        token_budget=settings.retrieval.default_token_budget,
        health=ProviderHealthTracker(path=health_path),
    )


__all__ = [
    "RoutingError",
    "ProviderSpec",
    "RouteRequest",
    "RoutingDecision",
    "HealthEvent",
    "ProviderHealth",
    "ProviderHealthTracker",
    "ModelRouter",
    "InvocationOutcome",
    "explain_route",
    "provider_specs_from_settings",
    "default_router",
]

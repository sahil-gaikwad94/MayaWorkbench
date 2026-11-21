"""Raw model-facing capabilities: routing, bounded structured-output repair,
tool-call reliability and per-call usage accounting.

All functions are deterministic and never grant authority. A route is advice for
which provider to call; the ModelGateway still enforces policy. Structured-output
repair retries only within a small budget and always revalidates against a strict
schema; invalid output is never executed or coerced into a default action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

MAX_REPAIR_ATTEMPTS = 3
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    reason: str
    max_output_tokens: int
    require_structured_output: bool = True


def route_task(
    task_type: str,
    *,
    uncertainty: float,
    context_tokens: int,
    provider_health: dict[str, dict[str, float]] | None = None,
    data_policy: str = "LOCAL_ONLY",
) -> ModelRoute:
    """Route by task requirement, not leaderboard position."""
    if context_tokens > 60_000:
        return ModelRoute("long-context", "context exceeds 60k tokens", 6000)
    if uncertainty >= 0.6 or task_type in {"PAPER_TO_CODE", "RESEARCH_ANALYSIS"}:
        return ModelRoute("reasoning", "high uncertainty or research reasoning", 4000)
    if task_type in {"REPOSITORY_QA", "ENGINEERING_CHANGE"}:
        return ModelRoute("coding", "code-aware task with tool validation", 3000)
    if data_policy == "LOCAL_ONLY":
        return ModelRoute("local", "local-only data policy", 1500)
    health = provider_health or {}
    if health:
        best = min(
            health,
            key=lambda name: (health[name].get("failures", 0), health[name].get("avg_latency_ms", 0)),
        )
        return ModelRoute(best, "lowest observed failure count and latency", 2000)
    return ModelRoute("balanced", "no specific requirement", 2000)


def extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = _FENCED.search(candidate)
    if fenced:
        candidate = fenced.group(1)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model response")
    parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response was not a JSON object")
    return parsed


def repair_structured_output(
    raw: str,
    schema: type[T],
    *,
    retry: Callable[[str], str] | None = None,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> tuple[T | None, list[str]]:
    """Parse, validate and optionally ask the provider to repair a bad answer.

    The retry callback is invoked at most ``max_attempts - 1`` additional times.
    No attempt may produce a value that fails the strict schema.
    """
    errors: list[str] = []
    attempts = max(1, min(int(max_attempts), MAX_REPAIR_ATTEMPTS))
    candidate = raw
    for attempt in range(attempts):
        try:
            return schema.model_validate(extract_json_object(candidate)), errors
        except Exception as exc:  # noqa: BLE001 - all parse/validation failures are retryable
            errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {str(exc)[:300]}")
            if retry is None or attempt + 1 >= attempts:
                break
            candidate = str(retry("\n".join(errors)) or "")
    return None, errors


@dataclass
class ToolReliability:
    success: int = 0
    failure: int = 0
    blocked: int = 0

    @property
    def score(self) -> float:
        total = self.success + self.failure + self.blocked
        if total == 0:
            return 0.5
        return round((self.success + 0.5 * self.blocked) / total, 3)


def tool_reliability(run_details: dict[str, Any]) -> dict[str, ToolReliability]:
    """Aggregate observed tool outcomes from the durable run details."""
    scores: dict[str, ToolReliability] = {}
    for call in run_details.get("tool_calls", []) or []:
        name = str(call.get("tool_name") or call.get("tool") or "unknown")
        entry = scores.setdefault(name, ToolReliability())
        status = str(call.get("status", "")).upper()
        if status in {"SUCCEEDED", "COMPLETED"}:
            entry.success += 1
        elif status in {"BLOCKED", "POLICY_BLOCKED"}:
            entry.blocked += 1
        else:
            entry.failure += 1
    return scores


def usage_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Normalise one model invocation into honest usage metadata."""
    prompt = int(record.get("prompt_tokens") or 0)
    completion = int(record.get("completion_tokens") or 0)
    cost = record.get("cost_usd")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "cost_basis": record.get("cost_basis", "unknown"),
    }


__all__ = [
    "ModelRoute",
    "route_task",
    "extract_json_object",
    "repair_structured_output",
    "ToolReliability",
    "tool_reliability",
    "usage_summary",
    "MAX_REPAIR_ATTEMPTS",
]

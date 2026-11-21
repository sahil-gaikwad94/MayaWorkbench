"""North-star metrics for verified task completion (§14).

Activity is not progress. A run counts only when its terminal decision is
accepted (possibly with limitations), its required evidence is complete, and the
recorded verification report agrees. Every other outcome is included in the
denominator, never silently dropped.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

ACCEPTED_DECISIONS = frozenset({"ACCEPTED", "COMPLETED_WITH_LIMITATIONS"})


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    decision: str
    human_minutes: float
    cost_usd: float | None
    tool_calls: int
    model_tokens: int
    evidence_complete: bool
    verification_report_present: bool

    @property
    def verified(self) -> bool:
        return (
            self.decision in ACCEPTED_DECISIONS
            and self.verification_report_present
            and self.evidence_complete
        )


def outcome_from_bundle(directory: str | Path, *, human_minutes: float = 0.0) -> TaskOutcome:
    root = Path(directory)
    report_path = root / "report.json"
    if not report_path.is_file():
        raise ValueError(f"no verification report in {root}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    details = report.get("run_details") or {}
    criteria = report.get("criteria") or []
    evidence = report.get("evidence_links") or []
    return TaskOutcome(
        task_id=str(report.get("task_id") or report.get("run_id") or root.name),
        decision=str(report.get("decision") or "UNKNOWN"),
        human_minutes=max(0.0, float(human_minutes)),
        cost_usd=float(details["cost_usd"]) if isinstance(details.get("cost_usd"), (int, float)) else None,
        tool_calls=int(details.get("tool_calls") or 0),
        model_tokens=int(details.get("tokens") or 0),
        evidence_complete=bool(criteria) and all(
            item.get("status") in {"PASSED", "VERIFIED", "COMPLETED_WITH_LIMITATIONS"}
            for item in criteria
        ) if criteria else bool(evidence),
        verification_report_present=True,
    )


def score(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    if not outcomes:
        return {"tasks": 0, "verified": 0, "rate": 0.0}
    verified = [item for item in outcomes if item.verified]
    supervision = sum(item.human_minutes for item in verified)
    costs = [item.cost_usd for item in verified if item.cost_usd is not None]
    return {
        "tasks": len(outcomes),
        "verified": len(verified),
        "rate": round(len(verified) / len(outcomes), 3),
        "verified_per_human_hour": round(len(verified) / (supervision / 60), 3)
        if supervision > 0 else None,
        "median_cost_usd": round(statistics.median(costs), 4) if costs else None,
        "median_tool_calls": round(statistics.median(
            [float(item.tool_calls) for item in verified]
        ), 2) if verified else None,
        "unsupported": asdict(outcomes[0]) if not verified else None,
    }


__all__ = ["TaskOutcome", "ACCEPTED_DECISIONS", "outcome_from_bundle", "score"]

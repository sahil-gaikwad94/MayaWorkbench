"""`ExperimentLabAuditor` — a facade over the real experiment analysis services.

The v2.0 blueprint named this class but it never existed. This facade groups
protocol planning, comparison and fixed statistics. It never upgrades a verdict
beyond what the recorded metrics and protocol support.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.experiments.service import ExperimentService


class ExperimentLabAuditor:
    def __init__(self, service: ExperimentService):
        self.service = service

    def plan(self, experiment_id: str) -> dict[str, Any]:
        return self.service.plan(experiment_id)

    def compare(self, *, baseline_id: str, candidate_id: str) -> Any:
        return self.service.compare(candidate_id, baseline_id=baseline_id)

    def statistics_summary(self, values: list[float]) -> dict[str, Any]:
        from mayaworkbench.experiments.stats import mean, sample_sd

        return {
            "n": len(values),
            "mean": mean(values),
            "sample_sd": sample_sd(values),
            "note": "Descriptive only; no p-value without a declared design.",
        }


__all__ = ["ExperimentLabAuditor"]

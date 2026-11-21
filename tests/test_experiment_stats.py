"""Numeric and parsing tests for the Phase 5 Experiment Lab.

These tests check hand-computed values rather than snapshots of our own output:
a wrong statistical implementation that is only compared with itself proves
nothing.
"""

from __future__ import annotations

import math

import pytest

from mayaworkbench.domain.enums import MetricAggregation, MetricDirection, MetricsSourceKind
from mayaworkbench.domain.models import MetricSpec
from mayaworkbench.errors import ExperimentError
from mayaworkbench.experiments import stats
from mayaworkbench.experiments.metrics import (
    aggregate,
    extract_metrics,
    parse_imported_metrics,
)

LOSS = MetricSpec(key="loss", direction=MetricDirection.LOWER_IS_BETTER, primary=True)


def test_t_distribution_matches_published_critical_values():
    assert stats.student_t_cdf(2.228, 10) == pytest.approx(0.975, abs=1e-4)
    assert stats.student_t_cdf(-2.228, 10) == pytest.approx(0.025, abs=1e-4)
    assert stats.student_t_cdf(0.0, 7) == pytest.approx(0.5, abs=1e-12)
    assert stats.student_t_quantile(0.975, 10) == pytest.approx(2.2281, abs=1e-3)
    assert stats.student_t_quantile(0.975, 1) == pytest.approx(12.7062, abs=1e-3)
    assert stats.student_t_quantile(0.975, 1000) == pytest.approx(1.9623, abs=1e-3)
    assert stats.two_sided_p_value(2.228, 10) == pytest.approx(0.05, abs=2e-4)


def test_paired_t_matches_hand_computation():
    differences = [1.0, 2.0, 3.0, 4.0]
    result = stats.paired_t(differences)
    assert result.difference == pytest.approx(2.5)
    assert result.degrees_of_freedom == 3
    assert result.t_statistic == pytest.approx(2.5 / (math.sqrt(5 / 3) / 2))
    assert result.p_value == pytest.approx(0.03047, abs=1e-4)
    assert result.ci_low == pytest.approx(0.4457, abs=1e-3)
    assert result.ci_high == pytest.approx(4.5543, abs=1e-3)


def test_paired_t_needs_two_pairs_and_reports_no_test():
    result = stats.paired_t([0.5])
    assert result.p_value is None
    assert result.ci_low is None
    assert result.notes


def test_constant_differences_do_not_fabricate_certainty():
    # A deterministic command produces exactly equal differences. Floating
    # point subtraction can leave a 1e-17 spread; that must not become p = 0.
    result = stats.paired_t([0.1, 0.1 + 1e-17, 0.1 - 1e-17])
    assert result.p_value is None
    assert result.t_statistic is None
    assert result.ci_low == result.ci_high == pytest.approx(0.1, abs=1e-12)
    assert "undefined" in " ".join(result.notes)
    effect = stats.hedges_g_paired([0.1, 0.1 + 1e-17, 0.1 - 1e-17])
    assert effect.value is None


def test_welch_t_and_degrees_of_freedom():
    result = stats.welch_t([1, 2, 3, 4], [2, 3, 4, 5])
    assert result.difference == pytest.approx(-1.0)
    assert result.degrees_of_freedom == pytest.approx(6.0)
    assert result.t_statistic == pytest.approx(-1.0954, abs=1e-4)
    assert result.p_value == pytest.approx(0.3153, abs=1e-3)


def test_welch_with_both_arms_constant_reports_no_test():
    result = stats.welch_t([2.0, 2.0, 2.0], [1.0, 1.0, 1.0])
    assert result.p_value is None
    assert result.ci_low == result.ci_high == pytest.approx(1.0)


def test_hedges_correction_and_holm_adjustment():
    independent = stats.hedges_g_independent([2, 3, 4], [1, 1.5, 2])
    assert independent.value is not None and independent.value > 1.0
    paired = stats.hedges_g_paired([1, 2, 3, 4])
    assert paired.value is not None and paired.value > 0
    adjusted = stats.holm_adjust([0.01, 0.04, 0.03, 0.5])
    assert adjusted == pytest.approx([0.04, 0.09, 0.09, 0.5])
    assert stats.holm_adjust([]) == []


def test_aggregation_is_declared_not_inferred():
    values = [1.0, 3.0, 2.0]
    assert aggregate(values, MetricAggregation.LAST) == 2.0
    assert aggregate(values, MetricAggregation.MEAN) == 2.0
    assert aggregate(values, MetricAggregation.MEDIAN) == 2.0
    assert aggregate(values, MetricAggregation.MAX) == 3.0
    assert aggregate(values, MetricAggregation.MIN) == 1.0
    assert aggregate(values, MetricAggregation.SUM) == 6.0
    assert aggregate([], MetricAggregation.MEAN) is None


def test_stdout_json_uses_the_last_metrics_document(tmp_path):
    stdout = (
        '{"metrics": {"loss": 9.0}}\n'
        "some log line\n"
        '{"metrics": {"loss": 1.5}, "step": 10}\n'
        '{"note": "not metrics"}\n'
    )
    result = extract_metrics(
        source=MetricsSourceKind.STDOUT_JSON,
        stdout=stdout,
        metrics=[LOSS],
        max_observations=100,
        max_file_bytes=1000,
    )
    assert result.values["loss"] == [(0, 10.0, 1.5)]
    assert not result.undeclared


def test_stdout_json_rejects_booleans_and_non_finite(tmp_path):
    with pytest.raises(ExperimentError):
        extract_metrics(
            source=MetricsSourceKind.STDOUT_JSON,
            stdout='{"metrics": {"loss": true}}',
            metrics=[LOSS],
            max_observations=10,
            max_file_bytes=1000,
        )
    with pytest.raises(ExperimentError):
        extract_metrics(
            source=MetricsSourceKind.STDOUT_JSON,
            stdout='{"metrics": {"loss": NaN}}',
            metrics=[LOSS],
            max_observations=10,
            max_file_bytes=1000,
        )


def test_jsonl_keeps_every_step_and_flags_undeclared(tmp_path):
    stdout = "\n".join(
        [
            '{"metrics": {"loss": 3.0, "acc": 0.1}, "step": 1}',
            "not json",
            '{"metrics": {"loss": 2.0}, "step": 2}',
        ]
    )
    result = extract_metrics(
        source=MetricsSourceKind.STDOUT_JSONL,
        stdout=stdout,
        metrics=[LOSS],
        max_observations=100,
        max_file_bytes=1000,
    )
    assert [value for _, _, value in result.values["loss"]] == [3.0, 2.0]
    assert result.undeclared == ["acc"]
    assert result.warnings


def test_json_file_metrics_cannot_escape_the_workspace(tmp_path):
    with pytest.raises(ExperimentError):
        extract_metrics(
            source=MetricsSourceKind.JSON_FILE,
            stdout="",
            workspace=tmp_path,
            path="../outside.json",
            metrics=[LOSS],
            max_observations=10,
            max_file_bytes=1000,
        )


def test_observation_budget_is_enforced():
    stdout = "\n".join(
        ['{"metrics": {"loss": 1.0}}' for _ in range(3)]
    )
    with pytest.raises(ExperimentError):
        extract_metrics(
            source=MetricsSourceKind.STDOUT_JSONL,
            stdout=stdout,
            metrics=[LOSS],
            max_observations=2,
            max_file_bytes=1000,
        )


def test_imported_csv_accepts_tall_and_wide_shapes_case_insensitively():
    tall = "Metric,Value,Step\nloss,1.5,1\nloss,1.2,2\n"
    result = parse_imported_metrics(tall, metrics=[LOSS], max_observations=10)
    assert [value for _, _, value in result.values["loss"]] == [1.5, 1.2]

    wide = "Seed,loss,accuracy\n1,1.1,0.5\n2,1.0,0.6\n"
    result = parse_imported_metrics(wide, metrics=[LOSS], max_observations=10)
    assert [value for _, _, value in result.values["loss"]] == [1.1, 1.0]
    assert result.undeclared == ["accuracy"]


def test_imported_json_reports_missing_declared_metrics():
    result = parse_imported_metrics(
        '{"metrics": {"accuracy": 0.9}}', metrics=[LOSS], max_observations=10
    )
    assert result.missing_declared == ["loss"]
    assert result.undeclared == ["accuracy"]


def test_imported_csv_rejects_non_numeric_values():
    with pytest.raises(ExperimentError):
        parse_imported_metrics(
            "metric,value\nloss,not-a-number\n", metrics=[LOSS], max_observations=10
        )

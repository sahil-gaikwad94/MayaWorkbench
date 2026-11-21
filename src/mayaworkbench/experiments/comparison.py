"""Build a fair baseline/candidate comparison from recorded runs.

Rules encoded here (see docs/adr/0010):

* only ``COMPLETED`` runs with ingested metrics are observations;
* the latest attempt per seed is used, and earlier attempts are disclosed;
* one run is one replicate: raw values inside a run are reduced by the metric's
  declared aggregation before any test, so per-step curves cannot inflate n;
* paired analysis is used when both arms share seeds, otherwise Welch's test;
* the primary metric drives the verdict; secondary metrics are exploratory and
  Holm-adjusted, and are never allowed to declare an improvement on their own;
* fewer than ``min_seeds`` paired/usable runs is ``INCONCLUSIVE``, not a
  directionally promising result dressed up as a finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mayaworkbench.domain.enums import (
    ExperimentDesign,
    ExperimentRunStatus,
    ExperimentVerdict,
    MetricDirection,
    ProtocolComparison,
)
from mayaworkbench.domain.models import (
    ExperimentDefinition,
    ExperimentRun,
    MetricComparison,
    MetricObservation,
    MetricSpec,
    ProtocolFinding,
)
from mayaworkbench.experiments import stats
from mayaworkbench.experiments.metrics import aggregate
from mayaworkbench.experiments.protocol import compare_protocol

ALPHA = stats.DEFAULT_ALPHA


@dataclass
class ComparisonOutcome:
    verdict: ExperimentVerdict
    design: ExperimentDesign
    comparable: bool
    findings: list[ProtocolFinding]
    metrics: list[MetricComparison]
    baseline_run_ids: list[str]
    candidate_run_ids: list[str]
    limitations: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _usable(run: ExperimentRun) -> bool:
    return (
        run.status == ExperimentRunStatus.COMPLETED
        and run.metrics_ingested
        and run.observation_count > 0
    )


def _per_run_values(
    runs: list[ExperimentRun],
    observations: dict[str, list[MetricObservation]],
    metric: MetricSpec,
) -> dict[int, float]:
    """One replicate value per run, using the metric's declared aggregation."""
    values: dict[int, float] = {}
    for run in runs:
        raw = [
            item.value
            for item in observations.get(run.id, [])
            if item.metric_key == metric.key
        ]
        if not raw:
            continue
        reduced = aggregate(raw, metric.aggregation)
        if reduced is not None:
            values[run.seed] = reduced
    return values


def _favors(
    metric: MetricSpec,
    p_value: float | None,
    baseline_mean: float | None,
    candidate_mean: float | None,
    difference: float | None,
    alpha: float,
) -> str:
    if (
        p_value is None
        or p_value >= alpha
        or difference is None
        or baseline_mean is None
        or candidate_mean is None
    ):
        return "INDETERMINATE"
    if metric.direction == MetricDirection.NEUTRAL:
        return "UNSPECIFIED_DIRECTION"
    if metric.direction == MetricDirection.HIGHER_IS_BETTER:
        return "CANDIDATE" if candidate_mean > baseline_mean else "BASELINE"
    return "CANDIDATE" if candidate_mean < baseline_mean else "BASELINE"


def _within_margin(
    metric: MetricSpec,
    ci_low: float | None,
    ci_high: float | None,
    margin: float | None,
) -> bool | None:
    """Non-inferiority test at the declared margin.

    For ``HIGHER_IS_BETTER`` the candidate is non-inferior when the lower
    confidence bound of (candidate - baseline) is above ``-margin``. For
    ``LOWER_IS_BETTER`` the upper bound must be below ``+margin``. ``NEUTRAL``
    metrics have no defined notion of worse and return ``None``.
    """
    if margin is None or ci_low is None or ci_high is None:
        return None
    if metric.direction == MetricDirection.HIGHER_IS_BETTER:
        return ci_low > -margin
    if metric.direction == MetricDirection.LOWER_IS_BETTER:
        return ci_high < margin
    return None


def _metric_comparison(
    metric: MetricSpec,
    *,
    baseline_values: dict[int, float],
    candidate_values: dict[int, float],
    paired: bool,
) -> MetricComparison:
    baseline_list = [baseline_values[seed] for seed in sorted(baseline_values)]
    candidate_list = [candidate_values[seed] for seed in sorted(candidate_values)]
    baseline_summary = stats.summarize(baseline_list)
    candidate_summary = stats.summarize(candidate_list)
    comparison = MetricComparison(
        key=metric.key,
        direction=metric.direction,
        unit=metric.unit,
        aggregation=metric.aggregation,
        primary=metric.primary,
        baseline_values=baseline_list,
        candidate_values=candidate_list,
        baseline_mean=baseline_summary["mean"],
        candidate_mean=candidate_summary["mean"],
        baseline_sd=baseline_summary["sd"],
        candidate_sd=candidate_summary["sd"],
        baseline_median=baseline_summary["median"],
        candidate_median=candidate_summary["median"],
    )
    if paired:
        common = sorted(set(baseline_values) & set(candidate_values))
        differences = [candidate_values[seed] - baseline_values[seed] for seed in common]
        test = stats.paired_t(differences)
        effect = stats.hedges_g_paired(differences)
    else:
        test = stats.welch_t(candidate_list, baseline_list)
        effect = stats.hedges_g_independent(candidate_list, baseline_list)
    comparison.method = test.method
    comparison.difference = test.difference
    comparison.ci_low = test.ci_low
    comparison.ci_high = test.ci_high
    comparison.t_statistic = test.t_statistic
    comparison.degrees_of_freedom = test.degrees_of_freedom
    comparison.p_value = test.p_value
    comparison.effect_size = effect.value
    comparison.effect_size_ci_low = effect.ci_low
    comparison.effect_size_ci_high = effect.ci_high
    comparison.favors = _favors(
        metric,
        test.p_value,
        comparison.baseline_mean,
        comparison.candidate_mean,
        comparison.difference,
        ALPHA,
    )
    comparison.notes.extend(test.notes)
    comparison.notes.extend(effect.notes)
    if comparison.baseline_mean is None or comparison.candidate_mean is None:
        comparison.notes.append("at least one arm has no usable values")
    if len(baseline_list) != len(candidate_list) and not paired:
        comparison.notes.append(
            "unpaired design: arms have different numbers of usable runs"
        )
    return comparison


def build_comparison(
    *,
    baseline: ExperimentDefinition,
    candidate: ExperimentDefinition,
    baseline_runs: dict[int, ExperimentRun],
    candidate_runs: dict[int, ExperimentRun],
    observations: dict[str, list[MetricObservation]],
    baseline_patch_hash: str | None,
    candidate_patch_hash: str | None,
    alpha: float = ALPHA,
) -> ComparisonOutcome:
    findings, unknowns, comparable = compare_protocol(
        baseline,
        candidate,
        baseline_commit=baseline.base_commit_sha,
        candidate_commit=candidate.base_commit_sha,
        baseline_patch=baseline_patch_hash,
        candidate_patch=candidate_patch_hash,
    )
    limitations: list[str] = []

    baseline_all = list(baseline_runs.values())
    candidate_all = list(candidate_runs.values())
    baseline_usable = {seed: run for seed, run in baseline_runs.items() if _usable(run)}
    candidate_usable = {
        seed: run for seed, run in candidate_runs.items() if _usable(run)
    }
    for arm, all_runs, usable in (
        ("baseline", baseline_all, baseline_usable),
        ("candidate", candidate_all, candidate_usable),
    ):
        unused = [run for run in all_runs if run.seed not in usable]
        for run in unused:
            if run.status == ExperimentRunStatus.COMPLETED and not run.metrics_ingested:
                detail = "the command completed but its metrics are not usable"
            else:
                detail = f"run is {run.status.value}"
            limitations.append(
                f"{arm} seed {run.seed}: {detail}"
                + (f" ({run.metrics_error})" if run.metrics_error else "")
                + "; it is not an observation"
            )

    common_seeds = sorted(set(baseline_usable) & set(candidate_usable))
    paired = bool(common_seeds) and set(baseline_usable) == set(candidate_usable)
    design = ExperimentDesign.PAIRED if paired else ExperimentDesign.UNPAIRED

    run_ids = lambda runs: [  # noqa: E731 - small local projection
        run.id for _, run in sorted(runs.items())
    ]

    metrics: list[MetricComparison] = []
    for metric in baseline.metrics:
        baseline_values = _per_run_values(
            list(baseline_usable.values()), observations, metric
        )
        candidate_values = _per_run_values(
            list(candidate_usable.values()), observations, metric
        )
        metrics.append(
            _metric_comparison(
                metric,
                baseline_values=baseline_values,
                candidate_values=candidate_values,
                paired=paired,
            )
        )

    # Multiple-comparison handling: the primary metric is confirmatory, the
    # secondary ones are exploratory and adjusted together.
    secondary = [item for item in metrics if not item.primary]
    adjusted = stats.holm_adjust(
        [item.p_value if item.p_value is not None else 1.0 for item in secondary]
    )
    for item, value in zip(secondary, adjusted):
        item.p_value_adjusted = value
    for item in metrics:
        if item.primary:
            item.p_value_adjusted = item.p_value

    primary_spec = baseline.primary_metric
    primary = next(
        (item for item in metrics if item.key == primary_spec.key), None
    )
    if primary is not None:
        primary.within_margin = _within_margin(
            primary_spec,
            primary.ci_low,
            primary.ci_high,
            candidate.non_inferiority_margin
            if candidate.non_inferiority_margin is not None
            else baseline.non_inferiority_margin,
        )

    for path in unknowns:
        limitations.append(
            f"protocol field {path} is unknown on both arms; fairness there cannot "
            "be verified from the recorded manifest"
        )
    if not comparable:
        limitations.append(
            "protocol differences include undeclared fairness violations; the "
            "numbers are recorded but are not a fair comparison"
        )

    minimum = min(baseline.min_seeds, candidate.min_seeds)
    usable_design_count = (
        len(common_seeds)
        if paired
        else min(
            len(primary.baseline_values) if primary else 0,
            len(primary.candidate_values) if primary else 0,
        )
    )
    if usable_design_count < minimum:
        limitations.append(
            f"{usable_design_count} comparable seed(s) are usable"
            + (" on both arms" if paired else " per arm")
            + f"; the declared minimum is {minimum}"
        )
    if paired and len(common_seeds) < 2:
        limitations.append("a paired test needs at least two paired seeds")

    verdict = _verdict(
        comparable=comparable,
        primary=primary,
        paired=paired,
        common_seeds=len(common_seeds),
        min_seeds=minimum,
        margin=candidate.non_inferiority_margin
        if candidate.non_inferiority_margin is not None
        else baseline.non_inferiority_margin,
        alpha=alpha,
    )
    if verdict == ExperimentVerdict.NO_DETECTABLE_DIFFERENCE:
        limitations.append(
            "no detectable difference is not evidence that the arms are equivalent; "
            "the interval is reported so its width can be judged"
        )
    if verdict == ExperimentVerdict.CANDIDATE_ADVANTAGE:
        limitations.append(
            "statistical evidence under the stated assumptions, not proof of a "
            "scientific claim; the effect size and interval describe a local run"
        )
    if verdict == ExperimentVerdict.CANDIDATE_NON_INFERIOR:
        limitations.append(
            "non-inferiority was assessed against the declared margin on the primary "
            "metric only"
        )

    return ComparisonOutcome(
        verdict=verdict,
        design=design,
        comparable=comparable,
        findings=findings,
        metrics=metrics,
        baseline_run_ids=run_ids(baseline_usable),
        candidate_run_ids=run_ids(candidate_usable),
        limitations=limitations,
        details={
            "paired_seeds": common_seeds,
            "baseline_usable_seeds": sorted(baseline_usable),
            "candidate_usable_seeds": sorted(candidate_usable),
            "alpha": alpha,
            "unknown_protocol_fields": unknowns,
            "earlier_attempts_excluded": [
                run.id
                for run in [*baseline_all, *candidate_all]
                if run.attempt > 1
            ],
        },
    )


def _verdict(
    *,
    comparable: bool,
    primary: MetricComparison | None,
    paired: bool,
    common_seeds: int,
    min_seeds: int,
    margin: float | None,
    alpha: float,
) -> ExperimentVerdict:
    if not comparable:
        return ExperimentVerdict.NOT_COMPARABLE
    if primary is None or primary.baseline_mean is None or primary.candidate_mean is None:
        return ExperimentVerdict.INCONCLUSIVE
    usable = common_seeds if paired else min(
        len(primary.baseline_values), len(primary.candidate_values)
    )
    if usable < min_seeds:
        return ExperimentVerdict.INCONCLUSIVE
    if primary.p_value is None:
        # A constant difference has no sampling variance. Equal constants are
        # an honest "no detectable difference"; a non-zero constant is still
        # not a testable effect with these runs.
        if (
            primary.difference == 0.0
            and primary.baseline_sd in (0.0, None)
            and primary.candidate_sd in (0.0, None)
        ):
            return ExperimentVerdict.NO_DETECTABLE_DIFFERENCE
        return ExperimentVerdict.INCONCLUSIVE
    if primary.p_value < alpha:
        if primary.favors == "CANDIDATE":
            return ExperimentVerdict.CANDIDATE_ADVANTAGE
        if primary.favors == "BASELINE":
            return ExperimentVerdict.BASELINE_ADVANTAGE
    if margin is not None and primary.within_margin is True:
        return ExperimentVerdict.CANDIDATE_NON_INFERIOR
    return ExperimentVerdict.NO_DETECTABLE_DIFFERENCE


__all__ = ["ComparisonOutcome", "build_comparison", "ALPHA"]

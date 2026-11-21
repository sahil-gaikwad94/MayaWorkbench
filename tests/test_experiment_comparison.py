"""Protocol fairness and comparison verdict tests (Phase 5).

The comparison engine is tested without a database: definitions, runs and
observations are plain records, so each rule can be exercised in isolation.
"""

from __future__ import annotations

import pytest

from mayaworkbench.domain.enums import (
    ExperimentDesign,
    ExperimentProfile,
    ExperimentRole,
    ExperimentRunStatus,
    ExperimentVerdict,
    MetricAggregation,
    MetricDirection,
    ProtocolComparison,
)
from mayaworkbench.domain.models import (
    ExperimentDefinition,
    ExperimentProtocol,
    ExperimentRun,
    MetricObservation,
    MetricSpec,
)
from mayaworkbench.experiments.comparison import build_comparison
from mayaworkbench.experiments.protocol import compare_protocol

COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40


def definition(
    role: ExperimentRole,
    *,
    identifier: str,
    commit: str = COMMIT,
    seeds: list[int] | None = None,
    margin: float | None = None,
    protocol: ExperimentProtocol | None = None,
    treatment: list[str] | None = None,
    direction: MetricDirection = MetricDirection.LOWER_IS_BETTER,
) -> ExperimentDefinition:
    return ExperimentDefinition(
        id=identifier,
        project_id="prj_1",
        repository_id="repo_1",
        snapshot_id="snap_1",
        base_commit_sha=commit,
        name=identifier,
        hypothesis="candidate is better",
        role=role,
        profile=ExperimentProfile.STATIC,
        seeds=seeds or [1, 2, 3],
        metrics=[
            MetricSpec(
                key="loss",
                direction=direction,
                aggregation=MetricAggregation.LAST,
                primary=True,
            )
        ],
        protocol=protocol or ExperimentProtocol(),
        treatment_dimensions=treatment or ["command"],
        min_seeds=2,
        non_inferiority_margin=margin,
        content_sha256="c" * 64,
    )


def run(identifier: str, experiment_id: str, seed: int) -> ExperimentRun:
    return ExperimentRun(
        id=identifier,
        experiment_id=experiment_id,
        project_id="prj_1",
        seed=seed,
        attempt=1,
        status=ExperimentRunStatus.COMPLETED,
        metrics_ingested=True,
        observation_count=1,
        exit_code=0,
    )


def observations(runs_and_values):  # type: ignore[no-untyped-def]
    out: dict[str, list[MetricObservation]] = {}
    for run_record, value in runs_and_values:
        out[run_record.id] = [
            MetricObservation(
                id=f"obs_{run_record.id}",
                experiment_id=run_record.experiment_id,
                run_id=run_record.id,
                project_id="prj_1",
                metric_key="loss",
                value=value,
                sample_index=0,
            )
        ]
    return out


def compare(baseline, candidate, baseline_values, candidate_values):  # type: ignore[no-untyped-def]
    baseline_runs = {
        seed: run(f"xrun_b{seed}", baseline.id, seed) for seed in baseline.seeds
    }
    candidate_runs = {
        seed: run(f"xrun_c{seed}", candidate.id, seed) for seed in candidate.seeds
    }
    data = observations(
        [
            *[(baseline_runs[seed], value) for seed, value in baseline_values.items()],
            *[
                (candidate_runs[seed], value)
                for seed, value in candidate_values.items()
            ],
        ]
    )
    return build_comparison(
        baseline=baseline,
        candidate=candidate,
        baseline_runs=baseline_runs,
        candidate_runs=candidate_runs,
        observations=data,
        baseline_patch_hash=None,
        candidate_patch_hash=None,
    )


def test_a_code_change_is_only_fair_when_declared_as_the_treatment():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(
        ExperimentRole.CANDIDATE, identifier="exp_c", commit=CANDIDATE_COMMIT
    )
    findings, unknowns, comparable = compare_protocol(
        baseline,
        candidate,
        baseline_commit=COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        baseline_patch=None,
        candidate_patch=None,
    )
    assert comparable is False
    assert any(
        item.path == "repository.commit"
        and item.classification == ProtocolComparison.FAIRNESS_VIOLATION
        for item in findings
    )

    declared = definition(
        ExperimentRole.CANDIDATE,
        identifier="exp_c2",
        commit=CANDIDATE_COMMIT,
        treatment=["command", "repository.commit", "repository.patch"],
    )
    findings, _unknowns, comparable = compare_protocol(
        baseline,
        declared,
        baseline_commit=COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        baseline_patch=None,
        candidate_patch=None,
    )
    assert comparable is True
    assert any(
        item.path == "repository.commit"
        and item.classification == ProtocolComparison.EXPECTED_TREATMENT
        for item in findings
    )


def test_dataset_difference_is_a_fairness_violation_unless_declared():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    protocol = ExperimentProtocol()
    protocol.dataset.fingerprint = "different"
    candidate = definition(
        ExperimentRole.CANDIDATE, identifier="exp_c", protocol=protocol
    )
    findings, _unknowns, comparable = compare_protocol(
        baseline,
        candidate,
        baseline_commit=COMMIT,
        candidate_commit=COMMIT,
        baseline_patch=None,
        candidate_patch=None,
    )
    assert comparable is False
    assert any(
        item.path == "dataset.fingerprint"
        and item.classification == ProtocolComparison.FAIRNESS_VIOLATION
        for item in findings
    )


def test_unknown_core_fields_are_reported_not_assumed_equal():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c")
    _findings, unknowns, comparable = compare_protocol(
        baseline,
        candidate,
        baseline_commit=COMMIT,
        candidate_commit=COMMIT,
        baseline_patch=None,
        candidate_patch=None,
    )
    assert comparable is True
    assert "dataset.fingerprint" in unknowns
    assert "batch_size" in unknowns


def test_metric_direction_mismatch_blocks_the_comparison():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(
        ExperimentRole.CANDIDATE,
        identifier="exp_c",
        direction=MetricDirection.HIGHER_IS_BETTER,
    )
    _findings, _unknowns, comparable = compare_protocol(
        baseline,
        candidate,
        baseline_commit=COMMIT,
        candidate_commit=COMMIT,
        baseline_patch=None,
        candidate_patch=None,
    )
    assert comparable is False


def test_paired_candidate_advantage():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c")
    outcome = compare(
        baseline,
        candidate,
        {1: 1.00, 2: 1.01, 3: 0.99},
        {1: 0.90, 2: 0.92, 3: 0.89},
    )
    assert outcome.design == ExperimentDesign.PAIRED
    assert outcome.verdict == ExperimentVerdict.CANDIDATE_ADVANTAGE
    primary = outcome.metrics[0]
    assert primary.difference == pytest.approx(-0.0967, abs=1e-3)
    assert primary.p_value is not None and primary.p_value < 0.05


def test_paired_baseline_advantage():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c")
    outcome = compare(
        baseline,
        candidate,
        {1: 0.90, 2: 0.92, 3: 0.89},
        {1: 1.00, 2: 1.01, 3: 0.99},
    )
    assert outcome.verdict == ExperimentVerdict.BASELINE_ADVANTAGE


def test_no_detectable_difference_is_not_equivalence():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b", seeds=[1, 2, 3, 4])
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c", seeds=[1, 2, 3, 4])
    outcome = compare(
        baseline,
        candidate,
        {1: 1.00, 2: 1.02, 3: 0.98, 4: 1.01},
        {1: 1.02, 2: 0.99, 3: 1.00, 4: 0.98},
    )
    assert outcome.verdict == ExperimentVerdict.NO_DETECTABLE_DIFFERENCE
    assert any("not evidence that the arms are equivalent" in item for item in outcome.limitations)


def test_non_inferiority_uses_the_declared_margin():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b", seeds=[1, 2, 3, 4])
    candidate = definition(
        ExperimentRole.CANDIDATE, identifier="exp_c", seeds=[1, 2, 3, 4], margin=0.05
    )
    outcome = compare(
        baseline,
        candidate,
        {1: 1.00, 2: 1.02, 3: 0.98, 4: 1.01},
        {1: 1.005, 2: 1.035, 3: 0.975, 4: 1.02},
    )
    assert outcome.verdict == ExperimentVerdict.CANDIDATE_NON_INFERIOR
    assert outcome.metrics[0].within_margin is True


def test_significant_regression_beats_non_inferiority():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b", seeds=[1, 2, 3, 4])
    candidate = definition(
        ExperimentRole.CANDIDATE, identifier="exp_c", seeds=[1, 2, 3, 4], margin=10.0
    )
    outcome = compare(
        baseline,
        candidate,
        {1: 1.00, 2: 1.01, 3: 0.99, 4: 1.00},
        {1: 2.00, 2: 2.02, 3: 1.99, 4: 2.01},
    )
    assert outcome.verdict == ExperimentVerdict.BASELINE_ADVANTAGE


def test_too_few_seeds_is_inconclusive():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b", seeds=[1])
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c", seeds=[1])
    outcome = compare(baseline, candidate, {1: 1.0}, {1: 0.9})
    assert outcome.verdict == ExperimentVerdict.INCONCLUSIVE
    assert any("declared minimum" in item for item in outcome.limitations)


def test_disjoint_seed_sets_use_welch_and_are_not_rejected_for_lacking_pairs():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b", seeds=[1, 2, 3])
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c", seeds=[4, 5, 6])
    outcome = compare(
        baseline,
        candidate,
        {1: 1.00, 2: 1.01, 3: 0.99},
        {4: 0.90, 5: 0.91, 6: 0.89},
    )
    assert outcome.design == ExperimentDesign.UNPAIRED
    assert outcome.metrics[0].method == "welch_t"
    assert outcome.verdict == ExperimentVerdict.CANDIDATE_ADVANTAGE


def test_incomparable_protocol_produces_no_directional_verdict():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(
        ExperimentRole.CANDIDATE, identifier="exp_c", commit=CANDIDATE_COMMIT
    )
    outcome = compare(
        baseline,
        candidate,
        {1: 1.00, 2: 1.01, 3: 0.99},
        {1: 0.90, 2: 0.92, 3: 0.89},
    )
    assert outcome.comparable is False
    assert outcome.verdict == ExperimentVerdict.NOT_COMPARABLE
    assert outcome.metrics[0].p_value is not None  # numbers are still reported


def test_constant_nonzero_difference_is_inconclusive_not_certain():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c")
    outcome = compare(
        baseline,
        candidate,
        {1: 1.0, 2: 1.0, 3: 1.0},
        {1: 0.9, 2: 0.9, 3: 0.9},
    )
    assert outcome.verdict == ExperimentVerdict.INCONCLUSIVE
    assert outcome.metrics[0].p_value is None
    assert outcome.metrics[0].ci_low == outcome.metrics[0].ci_high


def test_secondary_metrics_are_holm_adjusted_and_cannot_drive_the_verdict():
    baseline = definition(ExperimentRole.BASELINE, identifier="exp_b")
    candidate = definition(ExperimentRole.CANDIDATE, identifier="exp_c")
    candidate.metrics = [
        *candidate.metrics,
        MetricSpec(
            key="throughput",
            direction=MetricDirection.HIGHER_IS_BETTER,
            primary=False,
        ),
    ]
    baseline.metrics = [
        *baseline.metrics,
        MetricSpec(
            key="throughput",
            direction=MetricDirection.HIGHER_IS_BETTER,
            primary=False,
        ),
    ]
    baseline_runs = {seed: run(f"xrun_b{seed}", baseline.id, seed) for seed in baseline.seeds}
    candidate_runs = {seed: run(f"xrun_c{seed}", candidate.id, seed) for seed in candidate.seeds}
    primary_baseline = {1: 1.00, 2: 1.01, 3: 0.99}
    primary_candidate = {1: 0.90, 2: 0.92, 3: 0.89}
    secondary_baseline = {1: 100.0, 2: 101.0, 3: 99.0}
    secondary_candidate = {1: 150.0, 2: 152.0, 3: 148.0}
    data = observations(
        [
            *[(baseline_runs[seed], value) for seed, value in primary_baseline.items()],
            *[(candidate_runs[seed], value) for seed, value in primary_candidate.items()],
        ]
    )
    for run_record, values in (
        (baseline_runs, secondary_baseline),
        (candidate_runs, secondary_candidate),
    ):
        for seed, value in values.items():
            run_record_id = run_record[seed].id
            data[run_record_id].append(
                MetricObservation(
                    id=f"obs2_{run_record_id}",
                    experiment_id=run_record[seed].experiment_id,
                    run_id=run_record_id,
                    project_id="prj_1",
                    metric_key="throughput",
                    value=value,
                    sample_index=0,
                )
            )
    outcome = build_comparison(
        baseline=baseline,
        candidate=candidate,
        baseline_runs=baseline_runs,
        candidate_runs=candidate_runs,
        observations=data,
        baseline_patch_hash=None,
        candidate_patch_hash=None,
    )
    secondary = next(item for item in outcome.metrics if not item.primary)
    assert secondary.p_value is not None
    assert secondary.p_value_adjusted is not None
    assert secondary.p_value_adjusted >= secondary.p_value
    # The primary metric decides the verdict; the strong secondary result does
    # not create an advantage the primary metric did not support.
    assert outcome.verdict == ExperimentVerdict.CANDIDATE_ADVANTAGE

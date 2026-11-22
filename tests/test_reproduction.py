"""Pure, offline tests for the R2I four-tier reproducibility report.

Every record here is a real Pydantic domain model (no mocks of the models
themselves); the report builder is exercised as the pure function it is.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mayaworkbench.domain.enums import (
    CheckKind,
    CheckOutcome,
    ClaimKind,
    ClaimMappingStatus,
    ClaimReviewStatus,
    CriterionStatus,
    ExperimentDesign,
    ExperimentVerdict,
    MappingAuthor,
    MetricDirection,
    ProtocolComparison,
    TaskDecision,
    TaskType,
    VerificationGate,
    VerificationMethod,
)
from mayaworkbench.domain.models import (
    ClaimMapping,
    CriterionAssessment,
    DiffFile,
    ExperimentComparison,
    MetricComparison,
    ProtocolFinding,
    ResearchClaim,
    Task,
    TaskDiff,
    VerificationCheck,
    VerificationFinding,
    VerificationReport,
    WorkflowRun,
)
from mayaworkbench.research.obligations import (
    coverage,
    imported_evidence,
    kind_for_check_key,
    kinds_for_validation_method,
    obligations_for_claims,
)
from mayaworkbench.research.reproduction import (
    SCHEMA_VERSION,
    TIER_NAMES,
    _ref,
    build_reproduction_report,
    render_reproduction_markdown,
)

FIXED_TIME = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40
DIGEST = "b" * 64


# ------------------------------------------------------------------- factories
def claim(
    id: str,
    *,
    kind: ClaimKind = ClaimKind.PAPER_METHOD,
    validation_methods: tuple[str, ...] = (),
    validation_ids: tuple[str, ...] = (),
    statement: str = "The method preserves its defining structure.",
) -> ResearchClaim:
    return ResearchClaim(
        id=id,
        project_id="proj_1",
        kind=kind,
        title=f"claim {id}",
        statement=statement,
        validation_methods=list(validation_methods),
        validation_ids=list(validation_ids),
    )


def mapping(
    status: ClaimMappingStatus,
    *,
    id: str = "map_1",
    claim_id: str = "claim_1",
    details: dict | None = None,
    gaps: tuple[str, ...] = (),
) -> ClaimMapping:
    return ClaimMapping(
        id=id,
        project_id="proj_1",
        claim_id=claim_id,
        snapshot_id="snap_1",
        status=status,
        rule_pack_version="mapping/v1",
        details=details if details is not None else {"rule_status": status.value},
        gaps=list(gaps),
    )


def check(
    key: str,
    outcome: CheckOutcome = CheckOutcome.PASSED,
    *,
    id: str | None = None,
    kind: CheckKind = CheckKind.COMMAND,
    required: bool = True,
    gate: VerificationGate = VerificationGate.FUNCTIONAL,
) -> VerificationCheck:
    return VerificationCheck(
        id=id or "chk_" + key.replace(".", "_"),
        verification_id="ver_1",
        run_id="run_1",
        task_id="task_1",
        project_id="proj_1",
        key=key,
        gate=gate,
        kind=kind,
        title=key,
        method=VerificationMethod.DETERMINISTIC_TEST,
        outcome=outcome,
        required=required,
    )


def verification_report(
    *,
    criteria: tuple[CriterionStatus, ...] = (CriterionStatus.VERIFIED,),
    details: dict | None = None,
) -> VerificationReport:
    return VerificationReport(
        id="vr_1",
        verification_id="ver_1",
        run_id="run_1",
        task_id="task_1",
        project_id="proj_1",
        decision=TaskDecision.ACCEPTED,
        summary="verified",
        criteria=[
            CriterionAssessment(
                criterion_id=f"AC{index}", text="criterion", status=status
            )
            for index, status in enumerate(criteria, start=1)
        ],
        json_sha256=DIGEST,
        details=details or {},
    )


def diff(*, empty: bool = False) -> TaskDiff:
    return TaskDiff(
        id="diff_1",
        run_id="run_1",
        task_id="task_1",
        project_id="proj_1",
        workspace_id="ws_1",
        base_commit_sha=SHA,
        patch_artifact_id="art_patch",
        patch_sha256=DIGEST,
        files=[] if empty else [DiffFile(path="src/model.py", additions=3)],
    )


def comparison(
    verdict: ExperimentVerdict,
    *,
    comparable: bool = True,
    with_metric: bool = True,
    details: dict | None = None,
    limitations: tuple[str, ...] = (),
) -> ExperimentComparison:
    parity = verdict in {
        ExperimentVerdict.CANDIDATE_NON_INFERIOR,
        ExperimentVerdict.NO_DETECTABLE_DIFFERENCE,
    }
    if verdict is ExperimentVerdict.CANDIDATE_NON_INFERIOR:
        ci_low, ci_high = 0.01, 0.05
    elif verdict is ExperimentVerdict.NO_DETECTABLE_DIFFERENCE:
        ci_low, ci_high = -0.02, 0.02
    else:
        ci_low, ci_high = 0.1, 0.5
    metrics = []
    if with_metric:
        metrics.append(
            MetricComparison(
                key="accuracy",
                direction=MetricDirection.HIGHER_IS_BETTER,
                primary=True,
                p_value=0.01,
                ci_low=ci_low,
                ci_high=ci_high,
                favors="INDETERMINATE" if parity else "CANDIDATE",
                within_margin=True,
            )
        )
    return ExperimentComparison(
        id="cmp_1",
        project_id="proj_1",
        task_id="task_1",
        baseline_experiment_id="exp_base",
        candidate_experiment_id="exp_cand",
        baseline_definition_sha256=DIGEST,
        candidate_definition_sha256=DIGEST,
        verdict=verdict,
        design=ExperimentDesign.PAIRED,
        comparable=comparable,
        metrics=metrics,
        baseline_run_ids=["xrun_01J0000000000000000000000B"],
        candidate_run_ids=["xrun_01J0000000000000000000000C"],
        limitations=list(limitations),
        json_sha256=DIGEST,
        details=details
        if details is not None
        else {
            "non_inferiority_margin": 0.1,
            "margin_source": "pre-registered protocol",
        },
    )


def _metric(**overrides: object) -> MetricComparison:
    base: dict[str, object] = {
        "key": "accuracy",
        "direction": MetricDirection.HIGHER_IS_BETTER,
        "primary": True,
        "p_value": 0.01,
        "ci_low": 0.1,
        "ci_high": 0.5,
        "favors": "CANDIDATE",
        "within_margin": True,
    }
    base.update(overrides)
    return MetricComparison(**base)  # type: ignore[arg-type]


def build(
    *,
    claims: tuple[ResearchClaim, ...] = (),
    mappings: tuple[ClaimMapping, ...] = (),
    checks: tuple[VerificationCheck, ...] = (),
    report: VerificationReport | None = None,
    comparison_value: ExperimentComparison | None = None,
    candidate_diff: TaskDiff | None = None,
    run_details: dict | None = None,
    task_value: Task | None = None,
    run_value: WorkflowRun | None = None,
) -> object:
    run = WorkflowRun(
        id="run_1",
        task_id="task_1",
        contract_id="contract_1",
        project_id="proj_1",
        repository_id="repo_1",
        snapshot_id="snap_1",
        base_commit_sha=SHA,
    )
    task = Task(
        id="task_1",
        project_id="proj_1",
        title="Reproduce the paper method",
        task_type=TaskType.PAPER_TO_CODE,
        goal="Reproduce the reported method and result",
    )
    if run_details is None and comparison_value is not None:
        # A run-level binding must be declared explicitly; otherwise the
        # task-scoped comparison cannot raise this run to YES. Tests that
        # exercise the unbound case pass run_details={} explicitly.
        run_details = {"experiment_comparison_id": comparison_value.id}
    return build_reproduction_report(
        run=run_value or run,
        task=task_value or task,
        run_details=run_details or {},
        claims=list(claims),
        mappings=list(mappings),
        report=report,
        checks=list(checks),
        comparison=comparison_value,
        diff=candidate_diff,
        generated_at=FIXED_TIME,
    )


def passing_scenario() -> dict:
    """A run with verified mapping, reproduced patch and passing functional check."""
    return {
        "claims": (claim("claim_1"),),
        "mappings": (mapping(ClaimMappingStatus.PRESENT),),
        "checks": (
            check("invariant.shape_preserved", kind=CheckKind.ML_INVARIANT),
            check(
                "patch.reproducible",
                kind=CheckKind.DETERMINISTIC,
                gate=VerificationGate.CHANGE,
            ),
            check(
                "test.integrity",
                kind=CheckKind.DETERMINISTIC,
                gate=VerificationGate.REVIEW,
            ),
        ),
        "report": verification_report(),
        "candidate_diff": diff(),
    }


# --------------------------------------------------------------- obligation tests
def test_obligation_taxonomy_is_deterministic_and_typed() -> None:
    assert kinds_for_validation_method("finite-difference gradient check") == ("GRADIENT",)
    assert kinds_for_validation_method("softmax row-sum numerical tolerance") == (
        "NUMERICAL_REFERENCE",
    )
    assert kinds_for_validation_method("nothing declared here") == ()

    claims = (
        claim("c_method", statement="uses a causal mask", validation_methods=("check shape",)),
        claim("c_result", kind=ClaimKind.PAPER_RESULT),
        claim("c_req", kind=ClaimKind.REPRODUCTION_REQUIREMENT),
    )
    first = obligations_for_claims(claims, checks=())
    second = obligations_for_claims(tuple(reversed(claims)), checks=())
    assert first == second
    assert [item.id for item in first] == sorted(item.id for item in first)
    kinds = {item.kind for item in first}
    assert {"MASKING", "INVARIANT", "SHAPE", "METRIC", "BASELINE_PARITY",
            "DATASET_PARITY", "SEED", "SPLIT_PARITY"} <= kinds
    assert all(item.required for item in first)
    with pytest.raises(ValidationError):
        # extra="forbid" is enforced on validation, not on model_copy.
        type(first[0]).model_validate({**first[0].model_dump(), "bogus": 1})


def test_invariant_key_table_and_coverage() -> None:
    assert kind_for_check_key("invariant.causal_mask") == "MASKING"
    assert kind_for_check_key("invariant.finite_difference_gradient") == "GRADIENT"
    assert kind_for_check_key("invariant.softmax_row_sum") == "NUMERICAL_REFERENCE"
    assert kind_for_check_key("invariant.permutation_equivariance") == "INVARIANT"
    assert kind_for_check_key("command.unit") is None

    invariant = check("invariant.causal_mask", kind=CheckKind.ML_INVARIANT)
    shape = check("invariant.shape_preserved", kind=CheckKind.ML_INVARIANT)
    obligations = obligations_for_claims(
        (claim("c1", validation_methods=("causal mask",)),), checks=(invariant, shape)
    )
    assert coverage(obligations, (invariant, shape))["missing_required"] == []
    unverified = coverage(
        obligations,
        (
            invariant.model_copy(update={"outcome": CheckOutcome.SKIPPED}),
            shape.model_copy(update={"outcome": CheckOutcome.SKIPPED}),
        ),
    )
    assert unverified["missing_required"]
    assert all(not item["covered"] for item in unverified["obligations"])


# ---------------------------------------------------------------------- tier tests
def test_no_evidence_is_not_checked_except_empty_diff_is_no() -> None:
    report = build()
    assert report.schema_version == SCHEMA_VERSION
    assert report.tiers["METHOD_CODE_FOUND"].level == "NOT_CHECKED"
    assert report.tiers["METHOD_IMPLEMENTED"].level == "NO"
    assert report.tiers["METHOD_VALIDATED"].level == "NOT_CHECKED"
    assert report.tiers["RESULT_REPRODUCED"].level == "NOT_CHECKED"
    assert report.coverage["missing_required"] == []


def test_passing_records_yield_first_three_yes_and_no_result_check() -> None:
    report = build(**passing_scenario())
    assert report.tiers["METHOD_CODE_FOUND"].level == "YES"
    assert report.tiers["METHOD_IMPLEMENTED"].level == "YES"
    assert report.tiers["METHOD_VALIDATED"].level == "YES"
    assert report.tiers["RESULT_REPRODUCED"].level == "NOT_CHECKED"
    assert report.coverage["missing_required"] == []
    for tier in report.tiers.values():
        assert tier.evidence_refs or tier.level in {"NO", "NOT_CHECKED"}


def test_comparison_verdicts_drive_result_tier() -> None:
    scenario = passing_scenario()
    yes = build(**scenario, comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE))
    assert yes.tiers["RESULT_REPRODUCED"].level == "YES"

    no = build(**scenario, comparison_value=comparison(ExperimentVerdict.BASELINE_ADVANTAGE))
    assert no.tiers["RESULT_REPRODUCED"].level == "NO"

    imported = build(
        **scenario,
        comparison_value=comparison(
            ExperimentVerdict.CANDIDATE_ADVANTAGE,
            details={"experiment_imported_runs": True},
        ),
    )
    assert imported.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("outside this workbench" in gap for gap in imported.tiers["RESULT_REPRODUCED"].gaps)

    by_limitation = build(
        **scenario,
        comparison_value=comparison(
            ExperimentVerdict.CANDIDATE_ADVANTAGE,
            limitations=("2 runs were produced outside this workbench",),
        ),
    )
    assert by_limitation.tiers["RESULT_REPRODUCED"].level == "PARTIAL"

    inconclusive = build(**scenario, comparison_value=comparison(ExperimentVerdict.INCONCLUSIVE))
    assert inconclusive.tiers["RESULT_REPRODUCED"].level == "NOT_CHECKED"


def test_integrity_failure_forces_no_validated_and_caps_result() -> None:
    scenario = passing_scenario()
    failed_integrity = check(
        "test.integrity",
        CheckOutcome.FAILED,
        kind=CheckKind.DETERMINISTIC,
        gate=VerificationGate.REVIEW,
    )
    report = build(
        **{**scenario, "checks": (*scenario["checks"], failed_integrity)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    # Severe tampering must never raise a tier: validation is NO and a clean
    # comparison may reach only PARTIAL.
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert report.tiers["METHOD_IMPLEMENTED"].level == "PARTIAL"
    assert report.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("test.integrity" in gap for gap in report.tiers["METHOD_VALIDATED"].gaps)
    assert any("test.integrity" in gap for gap in report.tiers["RESULT_REPRODUCED"].gaps)


def test_survived_mutation_forces_no_validated() -> None:
    scenario = passing_scenario()
    report = build(
        **scenario,
        run_details={
            "mutation": {
                "results": [
                    {"outcome": "SURVIVED", "mutation_id": "mut_1", "declared": True}
                ]
            }
        },
    )
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert any("mut_1" in gap for gap in report.tiers["METHOD_VALIDATED"].gaps)


def test_unverifiable_mapping_never_reaches_code_found_yes() -> None:
    scenario = passing_scenario()
    uncertain = build(
        **{**scenario, "mappings": (mapping(ClaimMappingStatus.UNCERTAIN),)}
    )
    assert uncertain.tiers["METHOD_CODE_FOUND"].level == "NOT_CHECKED"

    coverage_limited = build(
        **{**scenario, "mappings": (
            mapping(ClaimMappingStatus.PRESENT, details={"coverage_limited": True}),
        )}
    )
    assert coverage_limited.tiers["METHOD_CODE_FOUND"].level != "YES"
    assert coverage_limited.tiers["METHOD_CODE_FOUND"].level == "NOT_CHECKED"

    absent = build(**{**scenario, "mappings": (mapping(ClaimMappingStatus.ABSENT),)})
    assert absent.tiers["METHOD_CODE_FOUND"].level == "NO"


def test_missing_required_obligation_prevents_yes() -> None:
    scenario = passing_scenario()
    with_gap = build(
        **{
            **scenario,
            "claims": (claim("claim_1", validation_methods=("gradient derivative",)),),
        }
    )
    assert with_gap.tiers["METHOD_VALIDATED"].level == "PARTIAL"
    assert with_gap.coverage["missing_required"]
    assert any("obligation" in gap for gap in with_gap.tiers["METHOD_VALIDATED"].gaps)


def test_canonical_hash_changes_with_tier_reason() -> None:
    base = build(**passing_scenario())
    changed = build(
        **passing_scenario(),
        run_details={"mutation": {"mutation_survived": True}},
    )
    assert len(base.canonical_sha256) == 64
    assert base.canonical_sha256 != changed.canonical_sha256
    identical = build(**passing_scenario())
    assert identical.canonical_sha256 == base.canonical_sha256


def test_markdown_renders_all_four_tiers_and_obligations() -> None:
    report = build(**passing_scenario())
    markdown = render_reproduction_markdown(report)
    for name in TIER_NAMES:
        assert f"## {name}" in markdown
    assert "## Obligations" in markdown
    assert "obl:claim_1:INVARIANT" in markdown
    assert "## Limitations" in markdown
    assert "not proofs" in markdown
    assert "Hash-verified bundles" in markdown


# ------------------------------------------------ adversarial-review regressions
def test_tampered_harness_cannot_raise_any_tier() -> None:
    """Finding 1 & 4: mutation bypass must not let a clean comparison reach YES."""
    scenario = passing_scenario()
    bypass = check("command.mutation", CheckOutcome.PASSED).model_copy(
        update={
            "findings": [
                VerificationFinding(code="MUTATION_SURVIVED", message="the mutant survived")
            ]
        }
    )
    report = build(
        **{**scenario, "checks": (*scenario["checks"], bypass)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert report.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("mutation" in gap for gap in report.tiers["RESULT_REPRODUCED"].gaps)


def test_result_requires_a_passing_required_functional_check() -> None:
    """Finding 2: a fair comparison with no passing functional check is not YES."""
    scenario = passing_scenario()
    not_required = check(
        "invariant.shape_preserved", kind=CheckKind.ML_INVARIANT, required=False
    )
    report = build(
        **{**scenario, "checks": (not_required, *scenario["checks"][1:])},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    tier = report.tiers["RESULT_REPRODUCED"]
    assert tier.level != "YES"
    assert any("no passing required functional check" in gap for gap in tier.gaps)


def test_failed_functional_check_blocks_result_reproduction() -> None:
    """Finding 2: a FAILED required functional check cannot be reproduced."""
    scenario = passing_scenario()
    failed = check("invariant.shape_preserved", CheckOutcome.FAILED)
    report = build(
        **{**scenario, "checks": (failed, *scenario["checks"][1:])},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert report.tiers["RESULT_REPRODUCED"].level != "YES"
    assert any("METHOD_VALIDATED is NO" in gap for gap in report.tiers["RESULT_REPRODUCED"].gaps)


def test_no_detectable_difference_needs_a_satisfied_margin() -> None:
    """Finding 3: parity only reaches YES with within_margin=True."""
    scenario = passing_scenario()
    within = build(
        **scenario,
        comparison_value=comparison(ExperimentVerdict.NO_DETECTABLE_DIFFERENCE),
    )
    assert within.tiers["RESULT_REPRODUCED"].level == "YES"
    outside = build(
        **scenario,
        comparison_value=comparison(ExperimentVerdict.NO_DETECTABLE_DIFFERENCE).model_copy(
            update={"metrics": [_metric(within_margin=False)]}
        ),
    )
    assert outside.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("margin" in gap for gap in outside.tiers["RESULT_REPRODUCED"].gaps)


def test_model_or_human_checks_never_cover_an_obligation() -> None:
    """Finding 4: an invariant key is not evidence when check.kind is non-deterministic."""
    model = check("invariant.shape_preserved", kind=CheckKind.MODEL_REVIEW)
    human = check("invariant.causal_mask", kind=CheckKind.HUMAN)
    obligations = obligations_for_claims(
        (claim("c", validation_methods=("invariant", "causal mask")),),
        checks=(model, human),
    )
    result = coverage(obligations, (model, human))
    assert result["missing_required"]
    assert all(not item["covered"] for item in result["obligations"])


def test_no_fallback_to_unrelated_declared_check_keys() -> None:
    """Finding 5: an obligation with no matching declared key stays unsatisfied."""
    command = check("command.unit")
    claims = (claim("c", validation_methods=("gradient derivative",), validation_ids=(command.id,)),)
    obligations = obligations_for_claims(claims, checks=(command,))
    gradient = [item for item in obligations if item.kind == "GRADIENT"]
    assert len(gradient) == 1
    assert gradient[0].satisfying_check_keys == ()
    assert coverage(obligations, (command,))["missing_required"]


def test_validation_method_declares_every_matching_kind() -> None:
    """Finding 6: all taxonomy matches are returned, de-duplicated and stable."""
    assert kinds_for_validation_method("gradient and causal mask and shape") == (
        "GRADIENT",
        "MASKING",
        "SHAPE",
    )


def test_comparison_coverage_requires_decisive_usable_fair_local_evidence() -> None:
    """Finding 7: comparison evidence requires more than comparable=True."""
    obligations = obligations_for_claims((claim("c_result", kind=ClaimKind.PAPER_RESULT),))
    assert coverage(obligations, (), comparison(ExperimentVerdict.INCONCLUSIVE))["missing_required"]
    assert coverage(
        obligations, (), comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE, with_metric=False)
    )["missing_required"]
    unfair = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={
            "findings": [
                ProtocolFinding(path="seed", classification=ProtocolComparison.FAIRNESS_VIOLATION)
            ]
        }
    )
    assert coverage(obligations, (), unfair)["missing_required"]
    assert coverage(
        obligations,
        (),
        comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE, details={"source": "IMPORTED"}),
    )["missing_required"]
    assert not coverage(
        obligations, (), comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE)
    )["missing_required"]


def test_primary_metric_must_be_declared_exactly_once() -> None:
    """Finding 8: no primary and multiple primaries are unusable, never invented."""
    scenario = passing_scenario()
    none = build(
        **scenario,
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE, with_metric=False),
    ).tiers["RESULT_REPRODUCED"]
    assert none.level != "YES"
    assert any("no primary metric" in gap for gap in none.gaps)

    two = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={"metrics": [_metric(), _metric(key="loss")]}
    )
    tier = build(**scenario, comparison_value=two).tiers["RESULT_REPRODUCED"]
    assert tier.level != "YES"
    assert any("primary metrics" in gap for gap in tier.gaps)


def test_malformed_statistics_never_yield_yes() -> None:
    """Finding 9: p-values, CIs and favors are validated before a YES."""
    scenario = passing_scenario()
    variants = (
        _metric(p_value=1.5),
        _metric(p_value=float("nan")),
        _metric(p_value_adjusted=2.0),
        _metric(ci_low=0.9, ci_high=0.1),
        _metric(favors="BOGUS"),
        _metric(
            favors="CANDIDATE",
            direction=MetricDirection.LOWER_IS_BETTER,
            baseline_mean=1.0,
            candidate_mean=2.0,
        ),
    )
    for metric in variants:
        cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
            update={"metrics": [metric]}
        )
        tier = build(**scenario, comparison_value=cmp).tiers["RESULT_REPRODUCED"]
        assert tier.level != "YES"
        assert tier.gaps


def test_mapping_verification_requires_explicit_evidence_and_provenance() -> None:
    """Findings 10 & 11: provenance, review status and explicit evidence matter."""
    scenario = passing_scenario()

    def level_for(mapping_value: ClaimMapping) -> str:
        return build(**{**scenario, "mappings": (mapping_value,)}).tiers[
            "METHOD_CODE_FOUND"
        ].level

    assert level_for(
        mapping(ClaimMappingStatus.PRESENT, details={"rule_status": None})
    ) == "NOT_CHECKED"

    manual = mapping(
        ClaimMappingStatus.PRESENT,
        details={"rule_status": None, "manual_override": True},
    ).model_copy(
        update={
            "created_by": MappingAuthor.USER,
            "review_status": ClaimReviewStatus.NEEDS_REVIEW,
        }
    )
    assert level_for(manual) == "NOT_CHECKED"
    accepted = manual.model_copy(
        update={
            "review_status": ClaimReviewStatus.ACCEPTED,
            "rationale": "reviewed spans support the definition",
        }
    )
    assert level_for(accepted) == "YES"

    model_only = mapping(
        ClaimMappingStatus.PRESENT,
        details={"rule_status": None, "model": {"confidence": 0.9}},
    ).model_copy(
        update={
            "created_by": MappingAuthor.MODEL,
            "model_invocation_id": "mi_1",
            "review_status": ClaimReviewStatus.NEEDS_REVIEW,
        }
    )
    assert level_for(model_only) == "NOT_CHECKED"
    model_accepted_without_evidence = model_only.model_copy(
        update={"review_status": ClaimReviewStatus.ACCEPTED}
    )
    assert level_for(model_accepted_without_evidence) == "NOT_CHECKED"

    rejected = claim("claim_1").model_copy(
        update={"review_status": ClaimReviewStatus.REJECTED}
    )
    assert (
        build(
            **{**scenario, "claims": (rejected,)},
        ).tiers["METHOD_CODE_FOUND"].level
        == "NOT_CHECKED"
    )


def test_patch_check_uses_explicit_allowlist() -> None:
    """Finding 12: substring matching on 'reproduc' is not an allowlist."""
    scenario = passing_scenario()
    rest = tuple(item for item in scenario["checks"] if item.key != "patch.reproducible")
    not_reproducible = check(
        "patch.not_reproducible", CheckOutcome.PASSED, gate=VerificationGate.CHANGE
    )
    tier = build(**{**scenario, "checks": (*rest, not_reproducible)}).tiers[
        "METHOD_IMPLEMENTED"
    ]
    assert tier.level == "PARTIAL"
    assert any("patch.reproducible" in gap for gap in tier.gaps)

    integrity_patch = check(
        "patch.integrity", CheckOutcome.PASSED, gate=VerificationGate.CHANGE
    )
    assert (
        build(**{**scenario, "checks": (*rest, integrity_patch)}).tiers[
            "METHOD_IMPLEMENTED"
        ].level
        == "YES"
    )


def test_inputs_must_be_scoped_to_run_task_and_project() -> None:
    """Finding 13: foreign records raise instead of producing a tier."""
    scenario = passing_scenario()
    foreign_check = check("command.foreign").model_copy(update={"run_id": "run_other"})
    with pytest.raises(ValueError, match="not scoped"):
        build(**{**scenario, "checks": (*scenario["checks"], foreign_check)})

    foreign_comparison = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={"task_id": "task_other"}
    )
    with pytest.raises(ValueError, match="not scoped"):
        build(**scenario, comparison_value=foreign_comparison)

    foreign_mapping = mapping(ClaimMappingStatus.PRESENT).model_copy(
        update={"snapshot_id": "snap_other"}
    )
    with pytest.raises(ValueError, match="not scoped"):
        build(**{**scenario, "mappings": (foreign_mapping,)})

    foreign_claim = claim("claim_1").model_copy(update={"project_id": "proj_other"})
    with pytest.raises(ValueError, match="not scoped"):
        build(**{**scenario, "claims": (foreign_claim,)})


def test_mutation_evidence_in_details_and_finding_codes() -> None:
    """Finding 14: details dicts and bypass codes count as mutation evidence."""
    scenario = passing_scenario()
    detail_check = check("command.mutation").model_copy(
        update={"details": {"mutation_survived": True}}
    )
    report = build(**{**scenario, "checks": (*scenario["checks"], detail_check)})
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert any("mutation" in gap for gap in report.tiers["METHOD_VALIDATED"].gaps)

    bypass = check("command.bypass").model_copy(
        update={
            "findings": [
                VerificationFinding(code="CRITICAL_TEST_BYPASS", message="bypassed")
            ]
        }
    )
    report = build(**{**scenario, "checks": (*scenario["checks"], bypass)})
    assert report.tiers["METHOD_VALIDATED"].level == "NO"

    by_counts = build(
        **{
            **scenario,
            "report": verification_report(
                details={"mutation": {"counts": {"SURVIVED": 2}}}
            ),
        }
    )
    assert by_counts.tiers["METHOD_VALIDATED"].level == "NO"
    assert any("2 mutation" in gap for gap in by_counts.tiers["METHOD_VALIDATED"].gaps)


def test_integrity_kind_and_nonfailed_outcomes_downgrade() -> None:
    """Finding 15: TEST_INTEGRITY kind and PARTIAL/UNVERIFIED also downgrade."""
    scenario = passing_scenario()
    partial_integrity = check(
        "evaluation.integrity",
        CheckOutcome.PARTIAL,
        kind=CheckKind.TEST_INTEGRITY,
        gate=VerificationGate.REVIEW,
    )
    report = build(
        **{**scenario, "checks": (*scenario["checks"], partial_integrity)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert report.tiers["RESULT_REPRODUCED"].level == "PARTIAL"


def test_tiers_cite_only_records_that_produced_them() -> None:
    """Finding 16: verified mappings, report hashes and comparison hashes are cited."""
    scenario = passing_scenario()
    unverified = mapping(
        ClaimMappingStatus.PRESENT,
        id="map_unverified",
        details={"rule_status": None},
    )
    report = build(
        **{**scenario, "mappings": (mapping(ClaimMappingStatus.PRESENT), unverified)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    found = report.tiers["METHOD_CODE_FOUND"]
    assert [item["id"] for item in found.evidence_refs if item["kind"] == "mapping"] == [
        "map_1"
    ]
    result = report.tiers["RESULT_REPRODUCED"]
    assert any(
        item["kind"] == "comparison" and item.get("sha256") == DIGEST
        for item in result.evidence_refs
    )
    validated = report.tiers["METHOD_VALIDATED"]
    # A clean verification report does not produce the tier: only checks do.
    assert all(item["kind"] != "report" for item in validated.evidence_refs)

    bad_report = verification_report(criteria=(CriterionStatus.FAILED,))
    cited = build(
        **{**scenario, "report": bad_report},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert any("criterion" in gap for gap in cited.tiers["METHOD_IMPLEMENTED"].gaps)
    assert any(
        item["kind"] == "report" for item in cited.tiers["METHOD_IMPLEMENTED"].evidence_refs
    )
    assert any(
        item["kind"] == "report" and item.get("sha256") == DIGEST
        for item in cited.tiers["METHOD_VALIDATED"].evidence_refs
    )


def test_fabricated_run_ids_cannot_support_a_reproduced_claim() -> None:
    scenario = passing_scenario()
    forged = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={
            "baseline_run_ids": ["nonexistent_b"],
            "candidate_run_ids": ["nonexistent_c"],
        }
    )
    report = build(**scenario, comparison_value=forged)
    tier = report.tiers["RESULT_REPRODUCED"]
    assert tier.level != "YES"
    assert any("not workbench run ids" in gap for gap in tier.gaps)


def test_comparison_needs_both_run_ids_for_yes() -> None:
    """Finding 17: a comparison with no runs cannot reproduce a result."""
    scenario = passing_scenario()
    no_runs = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={"baseline_run_ids": [], "candidate_run_ids": []}
    )
    tier = build(**scenario, comparison_value=no_runs).tiers["RESULT_REPRODUCED"]
    assert tier.level != "YES"
    assert any("no baseline and candidate runs" in gap for gap in tier.gaps)


def test_fairness_details_are_normalised_robustly() -> None:
    """Finding 19: a string or dict in fairness details does not inject entries."""
    scenario = passing_scenario()
    string_case = build(
        **scenario,
        comparison_value=comparison(
            ExperimentVerdict.CANDIDATE_ADVANTAGE,
            details={"fairness_violations": "leaked seed"},
        ),
    ).tiers["RESULT_REPRODUCED"]
    assert string_case.level != "YES"
    assert "leaked seed" in string_case.gaps

    dict_case = build(
        **scenario,
        comparison_value=comparison(
            ExperimentVerdict.CANDIDATE_ADVANTAGE,
            details={"fairness_violations": {"arm": "seed"}},
        ),
    ).tiers["RESULT_REPRODUCED"]
    assert dict_case.level != "YES"
    assert "arm=seed" in dict_case.gaps


class _BadPayload:
    def model_dump(self) -> object:
        return "not-a-dict"


def test_malformed_payload_fails_closed_with_an_explicit_note() -> None:
    """Finding 20: a non-dict model_dump is a named gap, never a silent {."""
    report = build(**passing_scenario(), run_details=_BadPayload())
    tier = report.tiers["METHOD_VALIDATED"]
    assert tier.level != "YES"
    assert any("model_dump returned str" in gap for gap in tier.gaps)


def test_imported_provenance_caps_yes() -> None:
    """Finding 21: imported runs from any provenance field are a gap."""
    scenario = passing_scenario()
    comparisons = (
        comparison(
            ExperimentVerdict.CANDIDATE_ADVANTAGE,
            details={"metrics_source_kind": "IMPORTED"},
        ),
        comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
            update={"details": {"run_provenance": {"source": "IMPORTED"}}}
        ),
    )
    for cmp in comparisons:
        tier = build(**scenario, comparison_value=cmp).tiers["RESULT_REPRODUCED"]
        assert tier.level != "YES"
        assert any("outside this workbench" in gap for gap in tier.gaps)

    imported_check = check("command.local").model_copy(
        update={"details": {"source_kind": "IMPORTED"}}
    )
    tier = build(
        **{**scenario, "checks": (*scenario["checks"], imported_check)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    ).tiers["RESULT_REPRODUCED"]
    assert tier.level != "YES"


def test_imported_and_malformed_statistics_cannot_yield_yes() -> None:
    """Findings 9 & 21 together: imported runs AND malformed stats block YES."""
    cmp = comparison(
        ExperimentVerdict.CANDIDATE_ADVANTAGE, details={"source": "IMPORTED"}
    ).model_copy(update={"metrics": [_metric(p_value=1.7)]})
    tier = build(**passing_scenario(), comparison_value=cmp).tiers["RESULT_REPRODUCED"]
    assert tier.level != "YES"
    assert any("outside this workbench" in gap for gap in tier.gaps)
    assert any("p-value" in gap for gap in tier.gaps)


def test_no_claims_never_yields_a_vacuous_yes() -> None:
    """Finding 22: empty obligations must not make 'not missing' a success."""
    checks = (
        check("invariant.shape_preserved", kind=CheckKind.ML_INVARIANT),
        check(
            "patch.reproducible", kind=CheckKind.DETERMINISTIC, gate=VerificationGate.CHANGE
        ),
    )
    report = build(checks=checks, comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE))
    assert report.coverage["missing_required"] == []
    assert report.tiers["METHOD_VALIDATED"].level == "NOT_CHECKED"
    assert report.tiers["RESULT_REPRODUCED"].level == "NOT_CHECKED"
    assert any(
        "no research claims" in gap for gap in report.tiers["RESULT_REPRODUCED"].gaps
    )


def test_ref_keeps_legitimate_falsy_identifiers() -> None:
    """Finding 23: only None/empty strings are dropped, not id=0."""
    assert _ref("diff", 0) == {"kind": "diff", "id": "0"}
    assert _ref("diff", None) is None
    assert _ref("diff", "") is None


def test_markdown_handles_a_rehydrated_report_missing_a_tier() -> None:
    """Finding 24: a missing tier renders an explicit NOT_CHECKED gap."""
    report = build(**passing_scenario())
    incomplete = report.model_copy(update={"tiers": {}})
    markdown = render_reproduction_markdown(incomplete)
    assert "NOT_CHECKED — missing tier" in markdown


def test_validated_tier_cites_comparison_covering_metric_obligations() -> None:
    """Finding 16: comparison evidence that covers obligations is cited."""
    scenario = passing_scenario()
    claims = (*scenario["claims"], claim("c_result", kind=ClaimKind.PAPER_RESULT))
    report = build(
        **{**scenario, "claims": claims},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert any(
        item["kind"] == "comparison" and item.get("sha256") == DIGEST
        for item in report.tiers["METHOD_VALIDATED"].evidence_refs
    )


# ------------------------------------------------ cycle-2 adversarial regressions
def test_cycle2_foreign_task_record_is_rejected() -> None:
    """F1: a task record from another run/task must not be mixed into a report."""
    scenario = passing_scenario()
    foreign_id = Task(
        id="task_other",
        project_id="proj_1",
        title="other task",
        task_type=TaskType.PAPER_TO_CODE,
        goal="other goal",
    )
    with pytest.raises(ValueError, match="does not match run"):
        build(**scenario, task_value=foreign_id)

    foreign_project = Task(
        id="task_1",
        project_id="proj_other",
        title="foreign project task",
        task_type=TaskType.PAPER_TO_CODE,
        goal="other goal",
    )
    with pytest.raises(ValueError, match="belongs to project"):
        build(**scenario, task_value=foreign_project)


def test_cycle2_duplicate_check_key_cannot_be_masked() -> None:
    """F2: a PASSED duplicate never covers an obligation when its twin failed."""
    scenario = passing_scenario()
    good = check(
        "invariant.shape_preserved",
        CheckOutcome.PASSED,
        id="chk_good",
        kind=CheckKind.ML_INVARIANT,
    )
    bad = check(
        "invariant.shape_preserved",
        CheckOutcome.FAILED,
        id="chk_bad",
        kind=CheckKind.ML_INVARIANT,
    )
    for checks in ((bad, good), (good, bad)):
        all_checks = (*checks, *scenario["checks"][1:])
        obligations = obligations_for_claims(scenario["claims"], checks=all_checks)
        result = coverage(obligations, all_checks)
        assert any("duplicate check key" in item for item in result["key_conflicts"])
        invariant = [item for item in result["obligations"] if "INVARIANT" in item["id"]][0]
        assert invariant["evidence"] == []
        assert not invariant["covered"]

        report = build(**{**scenario, "checks": all_checks})
        validated = report.tiers["METHOD_VALIDATED"]
        assert validated.level == "NO"
        assert any("duplicate check key" in gap for gap in validated.gaps)


def test_cycle2_mutation_markers_in_finding_details_and_wide_codes() -> None:
    """F3: bypass evidence in finding.details and every accepted code count."""
    scenario = passing_scenario()
    for code in (
        "MUTANT_SURVIVED",
        "MUTATION_NOT_KILLED",
        "EVAL_HARNESS_WEAKENED",
        "CRITICAL_TEST_BYPASS",
        "SURVIVED",
    ):
        marker = check("command.probe").model_copy(
            update={"findings": [VerificationFinding(code=code, message="probe result")]}
        )
        tier = build(
            **{**scenario, "checks": (*scenario["checks"], marker)}
        ).tiers["METHOD_VALIDATED"]
        assert tier.level == "NO"
        assert any(code in gap or "mutation" in gap for gap in tier.gaps)

    deep: dict = {}
    node = deep
    for _ in range(7):
        node["nested"] = {}
        node = node["nested"]
    node["counts"] = {"SURVIVED": 3}
    marker = check("command.probe").model_copy(
        update={
            "findings": [
                VerificationFinding(code="INFO", message="nested probe", details=deep)
            ]
        }
    )
    tier = build(
        **{**scenario, "checks": (*scenario["checks"], marker)}
    ).tiers["METHOD_VALIDATED"]
    assert tier.level == "NO"
    assert any("3 mutation" in gap for gap in tier.gaps)


@pytest.mark.parametrize(
    "outcome",
    [
        CheckOutcome.SKIPPED,
        CheckOutcome.INCONCLUSIVE,
        CheckOutcome.BLOCKED,
        CheckOutcome.NOT_APPLICABLE,
    ],
)
def test_cycle2_non_success_integrity_checks_downgrade(outcome: CheckOutcome) -> None:
    """F4: no integrity evidence is not integrity success."""
    scenario = passing_scenario()
    integrity = check(
        "test.integrity",
        outcome,
        kind=CheckKind.DETERMINISTIC,
        gate=VerificationGate.REVIEW,
    )
    report = build(
        **{**scenario, "checks": (*scenario["checks"], integrity)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert report.tiers["METHOD_VALIDATED"].level == "NO"
    assert report.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("test integrity is not verified" in gap for gap in report.tiers["METHOD_VALIDATED"].gaps)


def test_cycle2_null_statistics_never_yield_yes() -> None:
    """F5: p>=alpha, a CI spanning zero and indeterminate favors block YES."""
    scenario = passing_scenario()
    cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={
            "metrics": [
                _metric(p_value=0.9, ci_low=-0.2, ci_high=0.3, favors="")
            ]
        }
    )
    tier = build(**scenario, comparison_value=cmp).tiers["RESULT_REPRODUCED"]
    assert tier.level == "PARTIAL"
    assert any("alpha=0.05" in reason for reason in tier.reasons)
    assert any("not below alpha" in gap for gap in tier.gaps)
    assert any("indeterminate favors" in gap for gap in tier.gaps)
    assert any("includes zero" in gap for gap in tier.gaps)

    declared = comparison(
        ExperimentVerdict.CANDIDATE_ADVANTAGE,
        details={"alpha": 0.001},
    ).model_copy(update={"metrics": [_metric(p_value=0.01)]})
    strict = build(**scenario, comparison_value=declared).tiers["RESULT_REPRODUCED"]
    assert strict.level == "PARTIAL"
    assert any("alpha=0.001" in gap for gap in strict.gaps)


def test_cycle2_unbound_task_scoped_comparison_caps_at_partial() -> None:
    """F6: without a run-level binding a task-scoped comparison cannot be YES."""
    scenario = passing_scenario()
    cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE)
    unbound = build(**scenario, comparison_value=cmp, run_details={})
    tier = unbound.tiers["RESULT_REPRODUCED"]
    assert tier.level == "PARTIAL"
    assert any("task-scoped" in gap for gap in tier.gaps)
    assert any("task-scoped" in item for item in unbound.limitations)

    bound = build(
        **scenario,
        comparison_value=cmp,
        run_details={"experiment_comparison_id": cmp.id},
    )
    assert bound.tiers["RESULT_REPRODUCED"].level == "YES"
    assert not any("task-scoped" in item for item in bound.limitations)

    mismatched = build(
        **scenario,
        comparison_value=cmp,
        run_details={"experiment_comparison_id": "cmp_other"},
    )
    assert mismatched.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("does not match" in gap for gap in mismatched.tiers["RESULT_REPRODUCED"].gaps)


def test_cycle2_evidence_refs_cite_only_producing_records() -> None:
    """F7: failing duplicates, partial mappings and report refs stay off a YES."""
    scenario = passing_scenario()
    partial = mapping(
        ClaimMappingStatus.PARTIAL,
        id="map_partial",
        details={"rule_status": "PARTIAL"},
    )
    bad_patch = check(
        "patch.reproducible",
        CheckOutcome.FAILED,
        id="chk_bad_patch",
        gate=VerificationGate.CHANGE,
    )
    found = build(
        **{**scenario, "mappings": (scenario["mappings"][0], partial), "checks": (*scenario["checks"], bad_patch)},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert [item["id"] for item in found.tiers["METHOD_CODE_FOUND"].evidence_refs] == ["map_1"]
    implemented = found.tiers["METHOD_IMPLEMENTED"].evidence_refs
    assert "chk_bad_patch" not in [item["id"] for item in implemented]
    assert any(item["id"] == "chk_patch_reproducible" for item in implemented)
    assert all(item["kind"] != "report" for item in found.tiers["METHOD_VALIDATED"].evidence_refs)

    failed_functional = check(
        "invariant.shape_preserved",
        CheckOutcome.FAILED,
        id="chk_failed_functional",
    )
    invalid = build(
        **{**scenario, "checks": (failed_functional, *scenario["checks"][1:])},
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
    )
    assert [item["id"] for item in invalid.tiers["METHOD_VALIDATED"].evidence_refs] == [
        "chk_failed_functional"
    ]
    baseline = build(
        **scenario,
        comparison_value=comparison(ExperimentVerdict.BASELINE_ADVANTAGE),
    )
    assert all(item["kind"] != "check" for item in baseline.tiers["RESULT_REPRODUCED"].evidence_refs)


def test_cycle2_parity_requires_exact_margin_evidence() -> None:
    """F8: within_margin alone is a boolean assertion, not margin evidence."""
    scenario = passing_scenario()
    bare = build(
        **scenario,
        comparison_value=comparison(ExperimentVerdict.NO_DETECTABLE_DIFFERENCE, details={}),
    ).tiers["RESULT_REPRODUCED"]
    assert bare.level == "PARTIAL"
    assert any("margin value" in gap for gap in bare.gaps)

    declared = build(
        **scenario,
        comparison_value=comparison(
            ExperimentVerdict.CANDIDATE_NON_INFERIOR,
            details={"margin": 0.1, "margin_source": "pre-registered protocol"},
        ),
    ).tiers["RESULT_REPRODUCED"]
    assert declared.level == "YES"
    assert any("declared margin 0.1" in reason for reason in declared.reasons)


def test_cycle2_checks_must_belong_to_the_supplied_report() -> None:
    """F9: a check from another verification is mixed evidence."""
    scenario = passing_scenario()
    foreign = check("command.foreign_verification").model_copy(
        update={"verification_id": "ver_other"}
    )
    with pytest.raises(ValueError, match="verification"):
        build(**{**scenario, "checks": (*scenario["checks"], foreign)})


def test_cycle2_deep_and_cyclic_imported_evidence_is_detected() -> None:
    """F10: the depth cutoff is gone; arbitrary depth and cycles are safe."""
    deep: dict = {}
    node = deep
    for _ in range(8):
        node["nested"] = {}
        node = node["nested"]
    node["imported_runs"] = True
    assert any("imported_runs=True" in item for item in imported_evidence(deep))

    tier = build(
        **passing_scenario(),
        comparison_value=comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE),
        run_details=deep,
    ).tiers["RESULT_REPRODUCED"]
    assert tier.level == "PARTIAL"
    assert any("outside this workbench" in gap for gap in tier.gaps)

    cyclic: dict = {"note": "all local"}
    cyclic["self"] = cyclic
    assert imported_evidence(cyclic) == []

    cyclic["deep"] = deep
    assert any("imported_runs=True" in item for item in imported_evidence(cyclic))


def test_cycle2_zero_claims_preserves_tampering_gaps() -> None:
    """F11: the zero-claims override cannot erase computed downgrades."""
    report = build(run_details={"mutation": {"mutation_survived": True}})
    for name in ("METHOD_VALIDATED", "RESULT_REPRODUCED"):
        tier = report.tiers[name]
        assert tier.level == "NOT_CHECKED"
        assert any("mutation" in gap for gap in tier.gaps)
        assert any("mutation" in reason for reason in tier.reasons)
        assert any("no research claims" in gap for gap in tier.gaps)

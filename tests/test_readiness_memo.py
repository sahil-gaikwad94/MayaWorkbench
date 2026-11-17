"""Reproduction readiness and versioned research memos."""

from __future__ import annotations

import hashlib

import pytest

from mayaworkbench.domain.enums import (
    ClaimMappingStatus,
    ClaimReviewStatus,
    ClaimType,
    RiskCategory,
    RiskSeverity,
)
from mayaworkbench.domain.models import ClaimMapping, ClaimRiskFactor
from mayaworkbench.errors import ConflictError
from mayaworkbench.research.memo import (
    DEVIATIONS_START,
    INTERPRETATION_END,
    INTERPRETATION_START,
)
from mayaworkbench.research.readiness import WEIGHTS, ReadinessInput, assess_readiness


def passage_containing(services, document_id: str, needle: str):
    for passage in services.passages.list_by_document(document_id):
        if needle in passage.text:
            return passage
    raise AssertionError(f"no passage contains {needle!r}")


def explicit_claim(services, project, paper, *, title="Causal masking"):
    passage = passage_containing(services, paper.document.id, "must prevent")
    return services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
        title=title,
        statement=passage.text,
        document_id=paper.document.id,
        passage_id=passage.id,
        quote=passage.text,
        accept=True,
    )


def section(body: str, start: str, end: str) -> str:
    return body.split(start, 1)[1].split(end, 1)[0]


def test_empty_project_has_insufficient_information(services, project):
    context = services.memo_service.assess(project)
    assessment = context.readiness
    assert assessment.label == "INSUFFICIENT_INFORMATION"
    assert assessment.overall_risk is None
    assert assessment.known_weight == 0.0
    assert assessment.is_probability is False
    assert all(factor.state == "UNKNOWN" for factor in assessment.factors)
    assert all(factor.risk is None for factor in assessment.factors)
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    assert any("not a probability" in item for item in assessment.assumptions)


def test_unknown_factors_are_not_treated_as_zero_risk(services, project, paper):
    claim = explicit_claim(services, project, paper)
    context = services.memo_service.assess(project, document=paper.document)
    assessment = context.readiness
    by_key = {factor.key: factor for factor in assessment.factors}
    assert by_key["data_access"].state == "UNKNOWN"
    assert by_key["compute"].state == "UNKNOWN"
    assert by_key["evaluation"].state == "UNKNOWN"  # the claim declares no validation
    assert by_key["licensing"].state == "KNOWN"  # CC-BY-4.0 front matter
    assert by_key["licensing"].risk is not None and by_key["licensing"].risk < 0.3
    assert assessment.known_weight < 0.5
    assert assessment.label == "INSUFFICIENT_INFORMATION"
    assert claim.id


def test_severity_sets_factor_risk_and_blockers(services, project, paper):
    claim = services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="Data-dependent experiment",
        statement="Reproduce the reported accuracy.",
        validation_methods=["train and evaluate on the reported split"],
        accept=True,
    )
    assessment = assess_readiness(
        ReadinessInput(
            project_id=project.id,
            claims=[services.claims.get(claim.id)],
            documents=[paper.document],
            mappings=[
                ClaimMapping(
                    id="map_1",
                    project_id=project.id,
                    claim_id=claim.id,
                    snapshot_id="snap_x",
                    status=ClaimMappingStatus.PRESENT,
                    status_reason="all signals found",
                    rule_pack_version="mapping/v1",
                )
            ],
            risk_factors=[
                ClaimRiskFactor(
                    id="ris_1",
                    claim_id=claim.id,
                    category=RiskCategory.DATA_ACCESS,
                    severity=RiskSeverity.BLOCKER,
                    detail="the dataset is not publicly available",
                )
            ],
        )
    )
    by_key = {factor.key: factor for factor in assessment.factors}
    assert by_key["data_access"].state == "KNOWN"
    assert by_key["data_access"].risk == 1.0
    assert by_key["compute"].state == "UNKNOWN"
    assert by_key["evaluation"].risk == 0.0  # the only active claim declares validation
    assert assessment.overall_risk is not None
    assert any("BLOCKER" in blocker for blocker in assessment.blockers)
    assert any("not publicly available" in blocker for blocker in assessment.blockers)


def test_readiness_uses_accepted_claims_for_code_mapping(services, project, paper):
    accepted = explicit_claim(services, project, paper)
    unreviewed = services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="Unreviewed requirement",
        statement="An unreviewed requirement.",
    )
    project_id = project.id
    mappings = [
        ClaimMapping(
            id="map_1",
            project_id=project_id,
            claim_id=accepted.id,
            snapshot_id="snap_x",
            status=ClaimMappingStatus.PRESENT,
            status_reason="all signals found",
            rule_pack_version="mapping/v1",
        ),
        ClaimMapping(
            id="map_2",
            project_id=project_id,
            claim_id=unreviewed.id,
            snapshot_id="snap_x",
            status=ClaimMappingStatus.ABSENT,
            status_reason="nothing found",
            rule_pack_version="mapping/v1",
        ),
    ]
    assessment = assess_readiness(
        ReadinessInput(
            project_id=project_id,
            claims=[services.claims.get(accepted.id), services.claims.get(unreviewed.id)],
            mappings=mappings,
        )
    )
    by_key = {factor.key: factor for factor in assessment.factors}
    assert by_key["code_mapping"].risk == 0.0
    assert "accepted by review" in by_key["code_mapping"].explanation


def test_absent_accepted_claim_is_a_blocker(services, project, paper):
    claim = explicit_claim(services, project, paper)
    assessment = assess_readiness(
        ReadinessInput(
            project_id=project.id,
            claims=[services.claims.get(claim.id)],
            mappings=[
                ClaimMapping(
                    id="map_1",
                    project_id=project.id,
                    claim_id=claim.id,
                    snapshot_id="snap_x",
                    status=ClaimMappingStatus.ABSENT,
                    status_reason="not found",
                    rule_pack_version="mapping/v1",
                )
            ],
        )
    )
    assert any("no implementation evidence" in blocker for blocker in assessment.blockers)


def test_restrictive_licence_raises_the_licensing_factor(services, project, paper):
    assessment = assess_readiness(
        ReadinessInput(
            project_id=project.id,
            documents=[paper.document.model_copy(update={"license": "CC BY-NC 4.0"})],
        )
    )
    licensing = next(
        factor for factor in assessment.factors if factor.key == "licensing"
    )
    assert licensing.state == "KNOWN"
    assert licensing.risk == 0.6


def test_memo_has_every_section_and_is_stored_as_an_artifact(services, project, paper):
    explicit_claim(services, project, paper)
    build = services.memo_service.build(project, document=paper.document)
    memo = build.memo
    assert memo.version == 1
    assert memo.artifact_id
    assert memo.readiness["label"] in {
        "LOW_RISK",
        "MODERATE_RISK",
        "HIGH_RISK",
        "INSUFFICIENT_INFORMATION",
    }
    assert memo.source_interpretation_sha256
    body = build.markdown
    for heading in (
        "## 1. Research question",
        "## 2. Paper identity and version",
        "## 3. Central contribution",
        "## 4. Method breakdown",
        "## 5. Equations and assumptions",
        "## 6. Required implementation behaviour",
        "## 7. Paper-specified vs inferred values and requirements",
        "## 8. Repository mapping",
        "## 9. Unresolved ambiguities",
        "## 10. Data and compute requirements",
        "## 11. Proposed validation",
        "## 12. Reproduction limitations",
        "## 13. Source references",
        "## 14. Observed deviations and results",
    ):
        assert heading in body
    assert paper.document.title in body
    assert "metadata provenance" in body
    assert "not a probability" in body.lower()
    assert "No repository snapshot was mapped" in body
    assert "No source passage is referenced" not in body
    interpretation = section(body, INTERPRETATION_START, INTERPRETATION_END)
    assert hashlib.sha256(interpretation.encode("utf-8")).hexdigest() == (
        memo.source_interpretation_sha256
    )
    stored = services.artifacts.read_text(memo.artifact_id)
    assert stored == body


def test_memo_reports_mapping_and_readiness_with_a_snapshot(indexed, paper):
    services = indexed.services
    claim = services.claim_service.add_claim(
        indexed.project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="Causal masking",
        statement="The implementation must apply a causal mask.",
        implementation_requirement="triu + masked_fill in the attention forward pass",
        expected_code_signals=["triu", "masked_fill"],
        accept=True,
    )
    services.mapping_service.map_project(indexed.project, indexed.snapshot)
    build = services.memo_service.build(
        indexed.project, document=paper.document, snapshot=indexed.snapshot
    )
    body = build.markdown
    assert indexed.snapshot.identity_label in body
    assert "| claim | status | evidence (verified/total) | reason |" in body
    assert "PRESENT" in body
    assert "mapping/v1" in body
    assert claim.title in body
    assert build.matrix is not None
    assert build.readiness.mapping_counts.get("PRESENT") == 1


def test_memo_append_reuses_the_source_interpretation_verbatim(services, project, paper):
    explicit_claim(services, project, paper)
    first = services.memo_service.build(project, document=paper.document)
    second = services.memo_service.build(
        project,
        document=paper.document,
        append=True,
        deviations=["Reported accuracy 78.4, reproduced 74.1."],
    )
    assert second.memo.version == 2
    assert second.memo.supersedes_memo_id == first.memo.id
    first_block = section(first.markdown, INTERPRETATION_START, INTERPRETATION_END)
    second_block = section(second.markdown, INTERPRETATION_START, INTERPRETATION_END)
    assert first_block == second_block
    assert second.memo.source_interpretation_sha256 == first.memo.source_interpretation_sha256
    assert "Reported accuracy 78.4, reproduced 74.1." in second.markdown
    assert "intentionally" not in section(
        second.markdown, DEVIATIONS_START, "<!-- maya:deviations:end -->"
    ).lower().split("Reported accuracy")[0]

    third = services.memo_service.build(
        project,
        document=paper.document,
        append=True,
        deviations=["Second deviation: different hardware."],
    )
    assert third.memo.version == 3
    assert "Reported accuracy 78.4, reproduced 74.1." in third.markdown
    assert "Second deviation: different hardware." in third.markdown


def test_memo_append_without_a_previous_version_is_refused(services, project, paper):
    with pytest.raises(ConflictError, match="no previous memo"):
        services.memo_service.build(project, document=paper.document, append=True)


def test_memo_append_refuses_a_tampered_interpretation(services, project, paper):
    explicit_claim(services, project, paper)
    first = services.memo_service.build(project, document=paper.document)
    services.db.execute(
        "UPDATE research_memos SET body_markdown = REPLACE(body_markdown, ?, ?) WHERE id = ?",
        ("Causal masking", "Something else entirely", first.memo.id),
    )
    with pytest.raises(ConflictError, match="modified after it was written"):
        services.memo_service.build(project, document=paper.document, append=True)


def test_memo_without_claims_or_documents_is_refused(services, project):
    with pytest.raises(ConflictError, match="no claims or research documents"):
        services.memo_service.build(project)


def test_memo_list_shows_versions_newest_first(services, project, paper):
    explicit_claim(services, project, paper)
    services.memo_service.build(project, document=paper.document)
    services.memo_service.build(project, document=paper.document, append=True)
    memos = services.memos.list(project_id=project.id, document_id=paper.document.id)
    assert [memo.version for memo in memos] == [2, 1]
    latest = services.memos.latest(
        project_id=project.id, document_id=paper.document.id, snapshot_id=None
    )
    assert latest is not None and latest.version == 2


def test_rejected_claims_are_visible_in_the_memo(services, project, paper):
    claim = explicit_claim(services, project, paper)
    services.claim_service.review_claim(claim.id, accepted=False, note="misread the paper")
    build = services.memo_service.build(project, document=paper.document)
    assert "REJECTED" in build.markdown
    assert "misread the paper" in build.markdown

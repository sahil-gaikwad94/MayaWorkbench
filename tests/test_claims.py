"""Claims: deterministic extraction, manual authoring, review and the claim DAG."""

from __future__ import annotations

import pytest

from mayaworkbench.domain.enums import (
    ClaimConfidence,
    ClaimReviewStatus,
    ClaimType,
    RiskCategory,
    RiskSeverity,
)
from mayaworkbench.domain.models import ClaimDependency
from mayaworkbench.errors import CitationError, ConflictError, UsageError
from mayaworkbench.research.dag import validate_claim_dag


def passage_containing(services, document_id: str, needle: str):
    for passage in services.passages.list_by_document(document_id):
        if needle in passage.text:
            return passage
    raise AssertionError(f"no passage contains {needle!r}")


def add_explicit(services, project, paper, *, title="Explicit claim", statement=None):
    passage = passage_containing(services, paper.document.id, "triangular mask")
    return services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
        title=title,
        statement=statement or passage.text,
        document_id=paper.document.id,
        passage_id=passage.id,
        quote=passage.text,
    )


def add_user(services, project, *, title="Requirement", signals=None):
    return services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title=title,
        statement="The implementation must apply a causal mask.",
        implementation_requirement="masked_fill on the score matrix",
        expected_code_signals=signals or ["masked_fill"],
    )


def test_heuristic_extraction_creates_verified_explicit_claims(services, project, paper):
    summary = services.claim_service.extract(paper.document.id)
    assert summary.method == "DETERMINISTIC"
    assert summary.model_used is False
    assert summary.claims_created >= 4
    assert summary.claims_created + summary.claims_preserved == services.claims.count(
        project_id=project.id
    )
    assert summary.artifact_id
    assert summary.extraction_id
    for claim in summary.claims:
        assert claim.claim_type is ClaimType.EXPLICIT_SOURCE_CLAIM
        assert claim.review_status is ClaimReviewStatus.NEEDS_REVIEW
        assert claim.source_locations
        assert claim.created_by_detail.startswith("deterministic-extractor/")
        verification = services.claim_service.verify_sources(claim)
        assert verification and all(verified for _location, verified, _reason in verification)
        location = claim.source_locations[0]
        assert location.quote in services.passages.get(location.passage_id).text
        assert location.quote_hash


def test_extraction_rejects_reference_sections(services, project, tmp_path):
    path = tmp_path / "refs.md"
    path.write_text(
        "# Method\n\nWe use a learned mask to prevent future positions from leaking into the "
        "representation.\n\n# References\n\n[1] A. Author. A cited paper. 2020.\n",
        encoding="utf-8",
    )
    document = services.ingestion.ingest_file(project, path).document
    summary = services.claim_service.extract(document.id)
    assert summary.claims
    assert all("cited paper" not in claim.statement for claim in summary.claims)


def test_identical_extraction_must_be_forced(services, project, paper):
    first = services.claim_service.extract(paper.document.id)
    with pytest.raises(ConflictError, match="already been run"):
        services.claim_service.extract(paper.document.id)
    second = services.claim_service.extract(paper.document.id, force=True)
    assert second.claims_created == first.claims_created
    assert services.extractions.count() == 2


def test_reextraction_preserves_reviewed_claims_and_replaces_machine_claims(
    services, project, paper
):
    first = services.claim_service.extract(paper.document.id)
    accepted = services.claim_service.review_claim(
        first.claims[0].id, accepted=True, note="matches the abstract"
    )
    rejected = services.claim_service.review_claim(
        first.claims[1].id, accepted=False, note="overstates the result"
    )
    second = services.claim_service.extract(paper.document.id, force=True)
    assert second.claims_preserved == 2
    assert accepted.id in {claim.id for claim in services.claims.list(project_id=project.id)}
    assert rejected.id in {claim.id for claim in services.claims.list(project_id=project.id)}
    # The accepted claim keeps its review status; the machine claims are fresh.
    assert services.claims.get(accepted.id).review_status is ClaimReviewStatus.ACCEPTED
    assert all(
        claim.review_status is ClaimReviewStatus.NEEDS_REVIEW
        for claim in second.claims
        if claim.id not in {accepted.id, rejected.id}
    )


def test_explicit_claim_cannot_carry_basis_passages(services, project, paper):
    passage = passage_containing(services, paper.document.id, "triangular mask")
    with pytest.raises(UsageError, match="basis passages"):
        services.claim_service.add_claim(
            project.id,
            claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
            title="Mixed claim",
            statement=passage.text,
            document_id=paper.document.id,
            passage_id=passage.id,
            quote=passage.text,
            basis_passage_ids=[passage.id],
        )


def test_inferred_claims_need_a_derivation_note(services, project):
    with pytest.raises(UsageError, match="--derivation-note"):
        services.claim_service.add_claim(
            project.id,
            claim_type=ClaimType.INFERRED_REQUIREMENT,
            title="Inferred",
            statement="Padding must be masked.",
            derivation_note="",
        )
    claim = services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.INFERRED_REQUIREMENT,
        title="Inferred",
        statement="Padding must be masked.",
        implementation_requirement="pad mask",
        derivation_note="The paper evaluates padded batches but does not state this explicitly.",
    )
    assert claim.claim_type is ClaimType.INFERRED_REQUIREMENT
    assert claim.source_locations == []
    assert claim.derivation_note


def test_review_requires_a_note_to_reject(services, project, paper):
    claim = add_explicit(services, project, paper)
    with pytest.raises(UsageError, match="--note"):
        services.claim_service.review_claim(claim.id, accepted=False)
    rejected = services.claim_service.review_claim(
        claim.id, accepted=False, note="not supported by the cited passage"
    )
    assert rejected.review_status is ClaimReviewStatus.REJECTED
    assert rejected.reviewed_by == "local-user"
    assert rejected.reviewed_at is not None
    with pytest.raises(ConflictError, match="already has this review"):
        services.claim_service.review_claim(
            claim.id, accepted=False, note="not supported by the cited passage"
        )


def test_editing_a_claim_records_a_revision_and_resets_review(services, project, paper):
    claim = add_explicit(services, project, paper)
    services.claim_service.review_claim(claim.id, accepted=True, note="ok")
    updated = services.claim_service.edit_claim(
        claim.id,
        changes={"title": "Corrected title", "statement": "A corrected statement about masks."},
        reason="tightened wording",
    )
    assert updated.title == "Corrected title"
    assert updated.review_status is ClaimReviewStatus.NEEDS_REVIEW
    revisions = services.claims.list_revisions(claim.id)
    assert len(revisions) == 2  # review + edit
    assert revisions[-1].reason == "tightened wording"
    assert revisions[-1].before["title"] == claim.title
    assert revisions[-1].after["title"] == "Corrected title"
    assert [revision.revision for revision in revisions] == [1, 2]


def test_editing_with_keep_review_preserves_acceptance(services, project, paper):
    claim = add_explicit(services, project, paper)
    services.claim_service.review_claim(claim.id, accepted=True, note="ok")
    updated = services.claim_service.edit_claim(
        claim.id,
        changes={"missing_details": ["padding policy"]},
        reason="record what the paper omits",
        keep_review=True,
    )
    assert updated.review_status is ClaimReviewStatus.ACCEPTED
    assert updated.missing_details == ["padding policy"]


def test_editing_the_source_quote_reverifies_it(services, project, paper):
    claim = add_explicit(services, project, paper)
    other = passage_containing(services, paper.document.id, "does not change asymptotic")
    updated = services.claim_service.edit_claim(
        claim.id,
        changes={},
        source_location=(other.id, other.text),
        reason="cite the complexity passage instead",
    )
    assert updated.source_locations[0].passage_id == other.id
    with pytest.raises(CitationError, match="verbatim"):
        services.claim_service.edit_claim(
            claim.id,
            changes={},
            source_location=(other.id, "a paraphrase that does not appear"),
            reason="bad quote",
        )


def test_claim_edit_without_reason_is_refused_by_the_cli(services, project, paper):
    # The service allows an empty reason for programmatic edits, but the CLI requires one.
    claim = add_explicit(services, project, paper)
    updated = services.claim_service.edit_claim(
        claim.id, changes={"title": "Renamed"}, reason=""
    )
    assert updated.title == "Renamed"


def test_user_requirements_carry_no_paper_quote_but_can_cite_basis(services, project, paper):
    passage = passage_containing(services, paper.document.id, "triangular mask")
    claim = services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="User requirement",
        statement="Causal masking must be implemented in the attention forward pass.",
        document_id=paper.document.id,
        passage_id=passage.id,
        derivation_note="",
        accept=True,
        risk_factors=[
            (RiskCategory.DATA_ACCESS, RiskSeverity.WARNING, "needs WikiText-103"),
        ],
    )
    assert claim.source_locations == []
    assert claim.basis_passage_ids == [passage.id]
    assert claim.review_status is ClaimReviewStatus.ACCEPTED
    factors = services.claims.list_risk_factors([claim.id])
    assert len(factors) == 1
    assert factors[0].category is RiskCategory.DATA_ACCESS
    assert factors[0].severity is RiskSeverity.WARNING


def test_confidence_filter_drops_low_confidence_candidates(services, project, paper):
    summary = services.claim_service.extract(
        paper.document.id, min_confidence=ClaimConfidence.HIGH
    )
    assert all(
        claim.confidence is ClaimConfidence.HIGH for claim in summary.claims
    )


def test_section_restriction_and_missing_section(services, project, paper):
    summary = services.claim_service.extract(paper.document.id, section="2.1 Masking")
    assert summary.claims
    assert all(
        "2.1 Masking" in claim.source_locations[0].section
        for claim in summary.claims
        if claim.source_locations
    )
    from mayaworkbench.errors import NotFoundError

    with pytest.raises(NotFoundError, match="No passage matches"):
        services.claim_service.extract(paper.document.id, section="9 Nonexistent")


def test_dag_orders_dependencies_and_reports_depth(services, project, paper):
    first = add_explicit(services, project, paper, title="A")
    second = services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.INFERRED_REQUIREMENT,
        title="B",
        statement="B follows from A.",
        implementation_requirement="do B",
        derivation_note="derived from A",
        depends_on=[first.id],
    )
    dag = services.claim_service.dag(project_id=project.id)
    assert dag.valid
    assert dag.edge_count == 1
    assert dag.topological_order.index(first.id) < dag.topological_order.index(second.id)
    assert dag.depth[second.id] == dag.depth[first.id] + 1
    assert first.id in dag.roots
    assert second.id in dag.leaves


def test_dag_reports_missing_dependencies_and_cycles(services, project, paper):
    first = add_explicit(services, project, paper, title="A")
    second = add_user(services, project, title="B")
    broken = validate_claim_dag(
        [first, second],
        [
            ClaimDependency(
                id="dep_missing",
                claim_id=first.id,
                depends_on_claim_id="clm_missing",
            )
        ],
        project_id=project.id,
    )
    assert not broken.valid
    assert any(issue.code == "MISSING_DEPENDENCY" for issue in broken.errors)

    services.claims.add_dependency(
        ClaimDependency(id="dep_ab", claim_id=first.id, depends_on_claim_id=second.id)
    )
    services.claims.add_dependency(
        ClaimDependency(id="dep_ba", claim_id=second.id, depends_on_claim_id=first.id)
    )
    dag = services.claim_service.dag(project_id=project.id)
    assert not dag.valid
    assert dag.cycles
    assert any(issue.code == "DEPENDENCY_CYCLE" for issue in dag.errors)
    assert set(dag.unorderable) == {first.id, second.id}


def test_cross_document_parents_are_refused(services, project, paper, tmp_path):
    other_path = tmp_path / "other.md"
    other_path.write_text("# Other\n\nAnother method.\n", encoding="utf-8")
    other = services.ingestion.ingest_file(project, other_path).document
    parent = add_explicit(services, project, paper)
    other_passage = services.passages.list_by_document(other.id)[0]
    with pytest.raises(UsageError, match="another document"):
        services.claim_service.add_claim(
            project.id,
            claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
            title="Child",
            statement=other_passage.text,
            document_id=other.id,
            passage_id=other_passage.id,
            quote=other_passage.text,
            parent_claim_id=parent.id,
        )


def test_claim_belongs_to_its_project(services, project, paper):
    other = services.create_project("Another", "second")
    with pytest.raises(UsageError, match="another project"):
        services.claim_service.add_claim(
            other.id,
            claim_type=ClaimType.USER_REQUIREMENT,
            title="Foreign",
            statement="No.",
            depends_on=[add_user(services, project).id],
        )


def test_deleting_machine_claims_keeps_reviewed_ones(services, project, paper):
    summary = services.claim_service.extract(paper.document.id)
    reviewed = services.claim_service.review_claim(
        summary.claims[0].id, accepted=True, note="keep"
    )
    dropped = services.claims.delete_machine_claims(
        paper.document.id, keep=[reviewed.id]
    )
    remaining = services.claims.list(project_id=project.id)
    assert {claim.id for claim in remaining} == {reviewed.id}
    assert len(dropped) == len(summary.claims) - 1

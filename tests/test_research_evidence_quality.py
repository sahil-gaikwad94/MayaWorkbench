"""WP-RESEARCH-1 end-to-end: citations, disagreements and version alerts.

Every test uses real services and the migrated local database. No layer is
stubbed: records cross domain -> store -> service -> CLI and back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import (
    CitationGranularity,
    CitationRelation,
    ClaimMappingStatus,
    ClaimType,
    DisagreementKind,
    DisagreementStatus,
    PaperStatus,
)
from mayaworkbench.domain.models import ClaimMapping, SourceMetadata
from mayaworkbench.errors import CitationError, ConflictError, UsageError
from mayaworkbench.research.discovery import SourceHit
from mayaworkbench.research.passages import quote_hash

QUOTE = "we apply a triangular mask with torch.triu before the softmax"


def _passage_with(services, document_id: str, needle: str):
    for passage in services.passages.list_by_document(document_id):
        if needle in passage.text:
            return passage
    raise AssertionError(f"no passage contains {needle!r}")


def _claim(services, project, paper):
    passage = _passage_with(services, paper.document.id, "triangular mask")
    return services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
        title="Causal mask before softmax",
        statement="The method applies a triangular mask before softmax",
        document_id=paper.document.id,
        passage_id=passage.id,
        quote=QUOTE,
    )


def test_citation_accepts_verbatim_quote_and_rejects_paraphrase(services, project, paper):
    claim = _claim(services, project, paper)
    passage = _passage_with(services, paper.document.id, "triangular mask")
    citation = services.claim_service.cite(
        claim,
        passage,
        QUOTE,
        granularity=CitationGranularity.SENTENCE,
        relation=CitationRelation.QUALIFIES,
    )
    assert citation.claim_id == claim.id
    assert citation.project_id == project.id
    assert citation.document_id == paper.document.id
    assert citation.document_version == paper.document.version == 1
    assert citation.passage_id == passage.id
    assert citation.quote == QUOTE
    assert citation.quote_hash == quote_hash(QUOTE)
    assert citation.page == passage.page
    assert citation.section == passage.section_path
    assert citation.granularity is CitationGranularity.SENTENCE
    assert citation.relation is CitationRelation.QUALIFIES
    assert citation.confidence.value == "HIGH"
    assert citation.verified_at is not None
    assert citation.verified_by == "local-user"
    assert citation.char_start is not None and citation.char_end is not None
    assert citation.char_end >= citation.char_start
    # Persisted through the real store, not just returned from memory.
    stored = services.claim_service.citations.get(citation.id)
    assert stored.quote_hash == citation.quote_hash
    assert stored.document_version == citation.document_version

    with pytest.raises(CitationError, match="verbatim"):
        services.claim_service.cite(
            claim, passage, "the authors mask future tokens before softmax"
        )
    assert len(services.claim_service.list_citations(claim.id)) == 1
    assert all(item["verified"] for item in services.claim_service.verify_citations(claim.id))


def test_citation_binds_source_metadata_and_supersedes(services, project, paper):
    claim = _claim(services, project, paper)
    passage = _passage_with(services, paper.document.id, "triangular mask")
    hit = SourceHit(
        provider="memory",
        external_id="paper-v1",
        title="Causal Attention Revisited",
        paper_version="v1",
    )
    metadata = services.discovery_service.save_source(
        project, hit, document_key=paper.document.document_key
    )
    first = services.claim_service.cite(claim, passage, QUOTE, source_metadata_id=metadata.id)
    second = services.claim_service.cite(
        claim,
        passage,
        QUOTE,
        granularity=CitationGranularity.SUBCLAUSE,
        relation=CitationRelation.SUPPORTS,
        source_metadata_id=metadata.id,
        supersedes_citation_id=first.id,
    )
    assert second.supersedes_citation_id == first.id
    assert second.source_metadata_id == metadata.id
    assert services.claim_service.citations.get(first.id).supersedes_citation_id is None


def test_disagreement_needs_two_anchors(services, project, paper):
    claim = _claim(services, project, paper)
    passage = _passage_with(services, paper.document.id, "triangular mask")
    with pytest.raises(UsageError, match="at least two evidence anchors"):
        services.claim_service.record_disagreement(
            claim, kind=DisagreementKind.PAPER_VS_CODE, detail="something feels off"
        )
    with pytest.raises(CitationError, match="verbatim"):
        services.claim_service.record_disagreement(
            claim,
            kind=DisagreementKind.PAPER_VS_CODE,
            detail="paraphrased evidence",
            paper_quote="a paraphrase that is not in the passage",
            passage=passage,
            run_id="run-1",
        )
    disagreement = services.claim_service.record_disagreement(
        claim,
        kind=DisagreementKind.PAPER_VS_CODE,
        detail="the code masks after the softmax",
        paper_quote=QUOTE,
        passage=passage,
        run_id="run-1",
    )
    assert disagreement.status is DisagreementStatus.OPEN
    assert disagreement.document_id == paper.document.id
    assert disagreement.passage_id == passage.id
    assert disagreement.run_id == "run-1"
    assert disagreement.mapping_id is None
    listed = services.claim_service.list_disagreements(claim.id)
    assert [item.id for item in listed] == [disagreement.id]


def test_mapping_id_is_a_valid_second_anchor(services, project, paper, indexed):
    claim = _claim(services, project, paper)
    passage = _passage_with(services, paper.document.id, "triangular mask")
    mapping = ClaimMapping(
        id="map_evidence_quality",
        project_id=project.id,
        claim_id=claim.id,
        snapshot_id=indexed.snapshot.id,
        status=ClaimMappingStatus.CONFLICTING,
        rule_pack_version="test-1",
    )
    services.mappings.create(mapping)
    disagreement = services.claim_service.record_disagreement(
        claim,
        kind=DisagreementKind.PAPER_VS_CODE,
        detail="mapping and paper disagree on mask placement",
        paper_quote=QUOTE,
        passage=passage,
        mapping=mapping,
    )
    assert disagreement.mapping_id == mapping.id
    assert disagreement.run_id is None


def test_resolution_is_append_only_and_validates_transitions(services, project, paper):
    claim = _claim(services, project, paper)
    passage = _passage_with(services, paper.document.id, "triangular mask")
    original = services.claim_service.record_disagreement(
        claim,
        kind=DisagreementKind.CODE_VS_EXPERIMENT,
        detail="experiment used a different seed",
        paper_quote=QUOTE,
        passage=passage,
        run_id="run-seed-7",
    )
    with pytest.raises(UsageError, match="RESOLVED or DISMISSED"):
        services.claim_service.resolve_disagreement(
            original.id,
            status=DisagreementStatus.OPEN,
            resolution="not a resolution",
        )
    with pytest.raises(UsageError, match="non-empty"):
        services.claim_service.resolve_disagreement(
            original.id, status=DisagreementStatus.DISMISSED, resolution="   "
        )
    resolved = services.claim_service.resolve_disagreement(
        original.id,
        status=DisagreementStatus.RESOLVED,
        resolution="rerun with the declared seed reproduced the paper",
        actor="reviewer",
    )
    assert resolved.id != original.id
    assert resolved.status is DisagreementStatus.RESOLVED
    assert resolved.resolved_by == "reviewer"
    assert resolved.resolved_at is not None
    assert resolved.resolution
    # Original row is unchanged: history is preserved, not overwritten.
    assert services.claim_service.disagreements.get(original.id).status is DisagreementStatus.OPEN
    assert {item.id for item in services.claim_service.list_disagreements(claim.id)} == {
        original.id,
        resolved.id,
    }
    with pytest.raises(ConflictError, match="already RESOLVED"):
        services.claim_service.resolve_disagreement(
            resolved.id, status=DisagreementStatus.DISMISSED, resolution="too late"
        )


def test_versions_alerts_list_claims_citing_a_retracted_source(services, project, paper):
    claim = _claim(services, project, paper)
    passage = _passage_with(services, paper.document.id, "triangular mask")
    key = paper.document.document_key
    metadata = services.discovery_service.save_source(
        project,
        SourceHit(
            provider="memory",
            external_id="paper-v1",
            title="Causal Attention Revisited",
            paper_version="v1",
        ),
        document_key=key,
    )
    services.claim_service.cite(
        claim, passage, QUOTE, source_metadata_id=metadata.id
    )
    retracted = SourceMetadata(
        id="src_retracted_e2e",
        project_id=project.id,
        document_key=key,
        provider="memory",
        external_id="paper-v2-retracted",
        metadata_sha256="b" * 64,
        artifact_id=metadata.artifact_id,
        status=PaperStatus.RETRACTED,
        version_label="v2",
        retraction_date="2026-01-15",
        retraction_reason="statistical errors in table 2",
    )
    services.source_metadata.create(retracted)

    versions = services.discovery_service.versions(project, key)
    assert versions["key"] == key
    assert versions["source_metadata"]
    alerts = versions["status_alerts"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["status"] == PaperStatus.RETRACTED.value
    assert alert["source_metadata_id"] == retracted.id
    assert alert["version_label"] == "v2"
    assert alert["retraction_reason"] == "statistical errors in table 2"
    assert claim.id in alert["claim_ids"]
    assert {"id": paper.document.id, "version": 1} in alert["documents"]

    project_wide = services.discovery_service.versions(project)
    assert any(item["source_metadata_id"] == retracted.id for item in project_wide["status_alerts"])


CLI_PAPER = """# Method

We apply a triangular mask before the softmax to block future positions.
"""


def _run(capsys, home: Path, args: list[str]):
    code = main(["--home", str(home), "--json", *args])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else None
    return code, payload, captured


def test_cli_round_trips_for_citations_disagreements_and_alerts(
    capsys, home: Path, tmp_path: Path
):
    code, payload, captured = _run(capsys, home, ["init", "--project", "evidence"])
    assert code == 0, captured.err
    paper = tmp_path / "paper.md"
    paper.write_text(CLI_PAPER, encoding="utf-8")
    code, added, captured = _run(
        capsys, home, ["research", "add", str(paper), "--project", "evidence"]
    )
    assert code == 0, captured.err
    document_id = added["document"]["id"]
    code, extracted, captured = _run(
        capsys, home, ["research", "extract", document_id]
    )
    assert code == 0, captured.err
    claim = extracted["claims"][0]
    claim_id = claim["id"]
    location = claim["source_locations"][0]
    passage_id = location["passage_id"]
    quote = location["quote"]

    code, cited, captured = _run(
        capsys,
        home,
        [
            "research",
            "claim",
            "cite",
            claim_id,
            "--passage",
            passage_id,
            "--quote",
            quote,
            "--granularity",
            "SENTENCE",
            "--relation",
            "QUALIFIES",
        ],
    )
    assert code == 0, captured.err
    citation_id = cited["citation"]["id"]
    assert cited["citation"]["document_version"] == 1
    assert cited["citation"]["quote_hash"]

    code, citations, captured = _run(
        capsys, home, ["research", "claim", "citations", claim_id]
    )
    assert code == 0, captured.err
    assert [item["id"] for item in citations["citations"]] == [citation_id]
    assert all(item["verified"] for item in citations["verification"])

    code, disagreed, captured = _run(
        capsys,
        home,
        [
            "research",
            "claim",
            "disagree",
            claim_id,
            "--kind",
            "PAPER_VS_EXPERIMENT",
            "--detail",
            "the experiment used a different masking order",
            "--passage-id",
            passage_id,
            "--quote",
            quote,
            "--run-id",
            "run-cli-1",
        ],
    )
    assert code == 0, captured.err
    disagreement_id = disagreed["disagreement"]["id"]
    code, disagreements, captured = _run(
        capsys, home, ["research", "claim", "disagreements", claim_id]
    )
    assert code == 0, captured.err
    assert any(item["id"] == disagreement_id for item in disagreements["disagreements"])

    code, resolved, captured = _run(
        capsys,
        home,
        [
            "research",
            "claim",
            "resolve-disagreement",
            disagreement_id,
            "--status",
            "DISMISSED",
            "--resolution",
            "run was outside the declared protocol",
        ],
    )
    assert code == 0, captured.err
    assert resolved["disagreement"]["status"] == "DISMISSED"
    assert resolved["disagreement"]["resolved_by"] == "local-user"

    code, alerts, captured = _run(capsys, home, ["research", "versions", "--alerts"])
    assert code == 0, captured.err
    assert alerts["status_alerts"] == []

    # Typed failure: a text-only disagreement is refused with a usage error.
    code, _payload, captured = _run(
        capsys,
        home,
        [
            "research",
            "claim",
            "disagree",
            claim_id,
            "--kind",
            "PAPER_VS_CODE",
            "--detail",
            "no anchors at all",
        ],
    )
    assert code == UsageError.exit_code

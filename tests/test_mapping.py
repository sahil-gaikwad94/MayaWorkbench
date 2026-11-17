"""Claim-to-code mapping: strict signals, deterministic rules, manual and model paths."""

from __future__ import annotations

import json

import pytest

from mayaworkbench.domain.enums import (
    ClaimMappingStatus,
    ClaimType,
    EvidenceStrength,
    MappingAuthor,
)
from mayaworkbench.errors import ConflictError, NotFoundError, UsageError
from mayaworkbench.mapping.signals import matched_signals
from mayaworkbench.models.base import ModelCapabilities, ModelResponse, ModelUsage
from mayaworkbench.models.gateway import ModelGateway


def user_claim(
    services,
    project,
    *,
    title,
    signals,
    forbidden=None,
    statement=None,
    target_paths=None,
):
    return services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title=title,
        statement=statement or f"The implementation must provide {title}.",
        implementation_requirement=f"Provide {title}.",
        expected_code_signals=signals,
        forbidden_code_signals=forbidden or [],
        target_paths=target_paths or [],
        accept=True,
    )


def passage_containing(services, document_id: str, needle: str):
    for passage in services.passages.list_by_document(document_id):
        if needle in passage.text:
            return passage
    raise AssertionError(f"no passage contains {needle!r}")


def explicit_claim(services, project, paper, *, title="Causal masking", statement=None):
    passage = passage_containing(services, paper.document.id, "must prevent")
    return services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
        title=title,
        statement=statement or passage.text,
        document_id=paper.document.id,
        passage_id=passage.id,
        quote=passage.text,
        accept=True,
    )


def test_signals_match_whole_tokens_and_ignore_prose():
    assert matched_signals("scores = scores.masked_fill(mask, -inf)", ["masked_fill"]) == [
        "masked_fill"
    ]
    # `mask` must not match inside `masked_fill`.
    assert matched_signals("scores = scores.masked_fill(mask, -inf)", ["mask"]) == ["mask"]
    assert matched_signals("# triu builds the mask", ["triu"]) == ["triu"]
    assert matched_signals("# triu builds the mask", ["triu"], code_only=True) == []
    assert matched_signals('"""triu is used here"""', ["triu"], code_only=True) == []
    assert matched_signals("torch.triu(ones)", ["triu"], code_only=True) == ["triu"]


def test_present_requires_declared_signals_in_executable_definitions(indexed):
    services = indexed.services
    claim = user_claim(
        services,
        indexed.project,
        title="Causal triangular masking",
        signals=["triu", "masked_fill"],
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    assert outcome.mapping.status is ClaimMappingStatus.PRESENT
    assert outcome.mapping.created_by is MappingAuthor.DETERMINISTIC
    assert outcome.mapping.rule_pack_version == "mapping/v1"
    assert "executable code" in outcome.mapping.status_reason
    assert outcome.evidence
    for evidence in outcome.evidence:
        ok, reason = services.mapping_service.verify_evidence(evidence, indexed.snapshot.id)
        assert ok, reason
        assert evidence.excerpt_sha256
    assert any(
        evidence.symbol_id and evidence.relation.value == "IMPLEMENTS"
        for evidence in outcome.evidence
    )
    assert any(
        evidence.strength is EvidenceStrength.STRONG for evidence in outcome.evidence
    )


def test_derived_signals_never_produce_present_or_absent(indexed, paper):
    services = indexed.services
    claim = explicit_claim(services, indexed.project, paper)
    assert claim.expected_code_signals == []
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    assert outcome.mapping.status in {
        ClaimMappingStatus.PARTIAL,
        ClaimMappingStatus.UNCERTAIN,
    }
    assert "no acceptance signals" in outcome.mapping.status_reason or (
        "Derived signals" in outcome.mapping.status_reason
    )
    assert outcome.mapping.created_by is MappingAuthor.DETERMINISTIC


def test_forbidden_signal_in_code_makes_the_mapping_conflicting(indexed):
    services = indexed.services
    claim = user_claim(
        services,
        indexed.project,
        title="Masking without softmax shortcuts",
        signals=["triu"],
        forbidden=["softmax"],
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    assert outcome.mapping.status is ClaimMappingStatus.CONFLICTING
    assert outcome.mapping.counter_evidence
    assert any("forbidden" in reason or "softmax" in reason for reason in outcome.mapping.counter_evidence)
    assert "contradict" in outcome.mapping.status_reason.lower()


def test_missing_declared_signal_is_absent_without_coverage_gaps(indexed):
    services = indexed.services
    claim = user_claim(
        services,
        indexed.project,
        title="A helper that does not exist",
        signals=["never_defined_helper_xyz"],
        statement="We require never_defined_helper_xyz in the router.",
        target_paths=["attention.py"],
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    assert outcome.mapping.status is ClaimMappingStatus.ABSENT
    assert "never_defined_helper_xyz" in outcome.mapping.status_reason or "declared" in (
        outcome.mapping.status_reason
    )
    assert outcome.mapping.gaps


def test_definitional_requirement_without_signals_is_partial(indexed):
    services = indexed.services
    claim = services.claim_service.add_claim(
        indexed.project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="CausalAttention forward pass",
        statement="CausalAttention must compute causal self attention.",
        implementation_requirement="Implement CausalAttention.forward with masked scores.",
        target_symbols=["CausalAttention"],
        accept=True,
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    assert outcome.mapping.status is ClaimMappingStatus.PARTIAL
    assert outcome.mapping.gaps


def test_rejected_claims_are_reported_but_never_present(indexed):
    services = indexed.services
    claim = user_claim(
        services,
        indexed.project,
        title="Rejected masking requirement",
        signals=["triu"],
    )
    services.claim_service.review_claim(claim.id, accepted=False, note="duplicate")
    claim = services.claims.get(claim.id)
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    assert outcome.mapping.status is ClaimMappingStatus.UNCERTAIN
    assert "rejected" in outcome.mapping.status_reason


def test_map_project_records_a_matrix_artifact_and_skips_rejected_claims(indexed):
    services = indexed.services
    good = user_claim(
        services, indexed.project, title="Masking", signals=["triu", "masked_fill"]
    )
    rejected = user_claim(
        services, indexed.project, title="Rejected", signals=["triu"]
    )
    services.claim_service.review_claim(rejected.id, accepted=False, note="not needed")
    run = services.mapping_service.map_project(indexed.project, indexed.snapshot)
    assert run.artifact_id
    matrix = run.matrix
    assert matrix.claims_total == 2
    assert matrix.rejected_claim_ids == [rejected.id]
    assert matrix.mapped_rows
    assert matrix.counts["PRESENT"] == 1
    row = next(row for row in matrix.rows if row.claim_id == good.id)
    assert row.mapping_status == "PRESENT"
    assert row.evidence and all(item.verified for item in row.evidence)
    artifact = services.artifacts.read_text(run.artifact_id)
    assert good.id in artifact


def test_evidence_tampering_is_detected(indexed):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Masking", signals=["triu", "masked_fill"]
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot)
    evidence = outcome.evidence[0]
    services.db.execute(
        "UPDATE mapping_evidence SET excerpt = ? WHERE id = ?",
        ("tampered text", evidence.id),
    )
    changed = services.mappings.list_evidence(outcome.mapping.id)[0]
    ok, reason = services.mapping_service.verify_evidence(changed, indexed.snapshot.id)
    assert ok is False
    assert "hash" in reason or "text" in reason


def test_matrix_marks_unmapped_claims(indexed):
    services = indexed.services
    claim = user_claim(services, indexed.project, title="Unmapped", signals=["triu"])
    matrix = services.mapping_service.build_matrix(indexed.project, indexed.snapshot)
    row = next(row for row in matrix.rows if row.claim_id == claim.id)
    assert row.mapping_status is None
    assert claim.id in matrix.unmapped_claim_ids
    assert matrix.counts["UNMAPPED"] == 1


def test_manual_status_records_human_judgement(indexed):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Manual masking", signals=["triu"]
    )
    file = services.files.get_by_path(indexed.snapshot.id, "attention.py")
    lines = services.files.read_span(file.id, 1, file.line_count)
    start = next(number for number, text in lines if "def forward" in text)
    end = next(number for number, text in lines if "masked_fill" in text)
    outcome = services.mapping_service.set_manual_status(
        claim,
        indexed.snapshot,
        status=ClaimMappingStatus.PRESENT,
        reason="reviewed the forward pass by hand",
        evidence_spans=[f"attention.py:{start}-{end}"],
        actor="reviewer",
    )
    assert outcome.mapping.created_by is MappingAuthor.USER
    assert outcome.mapping.status is ClaimMappingStatus.PRESENT
    assert "reviewed" in outcome.mapping.status_reason
    assert outcome.mapping.status_reason == "reviewed the forward pass by hand"
    assert outcome.evidence
    assert outcome.evidence[0].strength is EvidenceStrength.STRONG


def test_manual_present_needs_evidence_and_declared_signals(indexed):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Manual requirement", signals=["triu"]
    )
    with pytest.raises(UsageError, match="needs --evidence"):
        services.mapping_service.set_manual_status(
            claim,
            indexed.snapshot,
            status=ClaimMappingStatus.PRESENT,
            reason="no evidence cited",
        )
    with pytest.raises(ConflictError, match="do not contain"):
        services.mapping_service.set_manual_status(
            claim,
            indexed.snapshot,
            status=ClaimMappingStatus.PRESENT,
            reason="wrong span",
            evidence_spans=["utils.py:1-3"],
        )
    with pytest.raises(NotFoundError):
        services.mapping_service.set_manual_status(
            claim,
            indexed.snapshot,
            status=ClaimMappingStatus.PARTIAL,
            reason="not in the snapshot",
            evidence_spans=["missing.py:1-2"],
        )


def test_manual_not_applicable_is_allowed_without_evidence(indexed):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Not applicable", signals=["triu"]
    )
    outcome = services.mapping_service.set_manual_status(
        claim,
        indexed.snapshot,
        status=ClaimMappingStatus.NOT_APPLICABLE,
        reason="this claim is about a different repository",
    )
    assert outcome.mapping.status is ClaimMappingStatus.NOT_APPLICABLE
    assert outcome.evidence == []


def test_parse_span_spec_validates_input():
    from mayaworkbench.mapping.service import parse_span_spec

    assert parse_span_spec("attention.py:10-20") == ("attention.py", 10, 20)
    for bad in ("attention.py", "attention.py:0-2", "attention.py:5-2", ":1-2"):
        with pytest.raises(UsageError):
            parse_span_spec(bad)


class AdjudicationProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def capabilities(self):
        return ModelCapabilities(structured_output=True)

    def generate(self, request):
        self.calls += 1
        return ModelResponse(
            text=json.dumps(self.payload),
            structured=self.payload,
            usage=ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            provider=self.name,
            model=self.model,
            provider_request_id="fake-adjudication",
            latency_ms=2,
            used_structured_output=True,
        )


def install_provider(services, payload: dict):
    provider = AdjudicationProvider(payload)
    services.mapping_service.gateway = ModelGateway(
        services.settings, services.invocations, services.events, lambda _settings: provider
    )
    return provider


def test_model_adjudication_can_confirm_present_only_with_code_evidence(
    indexed, virtual_model
):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Model confirmed masking", signals=["triu"]
    )
    provider = install_provider(
        services,
        {
            "status": "PRESENT",
            "rationale": "S1 is the forward pass and contains triu in executable code.",
            "evidence": [
                {
                    "span_ref": "S1",
                    "relation": "IMPLEMENTS",
                    "explanation": "triu builds the causal mask",
                    "matched_signals": ["triu"],
                }
            ],
            "gaps": [],
            "counter_evidence": [],
            "confidence": "HIGH",
        },
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot, use_model=True)
    assert provider.calls == 1
    assert outcome.mapping.status is ClaimMappingStatus.PRESENT
    assert outcome.mapping.created_by is MappingAuthor.MODEL
    assert outcome.model_used is True
    assert outcome.model_rejected is False


def test_model_present_without_signals_is_refused_and_rules_kept(indexed, virtual_model):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Model overreach", signals=["triu"]
    )
    install_provider(
        services,
        {
            "status": "PRESENT",
            "rationale": "Looks implemented.",
            "evidence": [],
            "gaps": [],
            "counter_evidence": [],
            "confidence": "HIGH",
        },
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot, use_model=True)
    assert outcome.model_rejected is True
    assert outcome.mapping.created_by is MappingAuthor.DETERMINISTIC
    assert outcome.mapping.status is ClaimMappingStatus.PRESENT
    assert any("rejected" in note for note in outcome.notes)


def test_model_cannot_cite_a_span_it_was_not_shown(indexed, virtual_model):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="Invented citation", signals=["triu"]
    )
    install_provider(
        services,
        {
            "status": "PARTIAL",
            "rationale": "See span S99.",
            "evidence": [
                {
                    "span_ref": "S99",
                    "relation": "SUPPORTS",
                    "explanation": "invented",
                    "matched_signals": ["triu"],
                }
            ],
            "gaps": [],
            "counter_evidence": [],
            "confidence": "LOW",
        },
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot, use_model=True)
    assert outcome.model_rejected is True
    assert any("S99" in note for note in outcome.notes)


def test_model_absent_is_refused_when_signals_were_found(indexed, virtual_model):
    services = indexed.services
    claim = user_claim(
        services, indexed.project, title="False absence", signals=["triu"]
    )
    install_provider(
        services,
        {
            "status": "ABSENT",
            "rationale": "Not implemented.",
            "evidence": [],
            "gaps": [],
            "counter_evidence": [],
            "confidence": "HIGH",
        },
    )
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot, use_model=True)
    assert outcome.model_rejected is True
    assert outcome.mapping.status is ClaimMappingStatus.PRESENT


def test_model_must_be_callable_when_requested(indexed):
    services = indexed.services
    claim = user_claim(services, indexed.project, title="No model", signals=["triu"])
    assert services.gateway.status().callable is False
    outcome = services.mapping_service.map_claim(claim, indexed.snapshot, use_model=True)
    assert outcome.model_used is False
    assert any("model unavailable" in note for note in outcome.notes)

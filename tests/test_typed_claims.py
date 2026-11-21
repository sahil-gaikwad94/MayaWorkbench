"""WP6 end-to-end: typed claim graph and source discovery with immutable metadata."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import (
    ClaimExplicitness,
    ClaimKind,
    ClaimMappingStatus,
    ClaimType,
)
from mayaworkbench.errors import UsageError
from mayaworkbench.research.discovery import SourceHit


def _claim(services, project, paper, **overrides):
    passage = next(
        p for p in services.passages.list_by_document(paper.document.id)
        if "triangular mask" in p.text
    )
    values = {
        "kind": ClaimKind.PAPER_METHOD,
        "title": "Causal masking before normalization",
        "statement": "Apply the causal mask before normalising attention scores",
        "explicitness": ClaimExplicitness.EXPLICIT,
        "document_id": paper.document.id,
        "passage_id": passage.id,
        "quote": "we apply a triangular mask with torch.triu before the softmax",
        "target_resources": [{"kind": "SYMBOL", "path": "src/model/attention.py", "symbol": "Attention.forward"}],
        "validation_ids": ["invariant-causal-mask"],
        "uncertainty": ["kernel implementation is not specified"],
    }
    values.update(overrides)
    return services.claim_service.add_claim(project.id, **values)


def test_typed_claim_persists_every_graph_field(services, project, paper):
    claim = _claim(services, project, paper)
    stored = services.claims.get(claim.id)
    assert stored.kind is ClaimKind.PAPER_METHOD
    assert stored.claim_type is ClaimType.EXPLICIT_SOURCE_CLAIM
    assert stored.explicitness is ClaimExplicitness.EXPLICIT
    assert stored.mapping_status is ClaimMappingStatus.NOT_CHECKED
    assert stored.target_resources[0].symbol == "Attention.forward"
    assert stored.validation_ids == ["invariant-causal-mask"]
    assert "kernel" in stored.uncertainty[0]
    assert stored.graph_version


def test_incompatible_type_and_kind_are_refused(services, project):
    try:
        services.claim_service.add_claim(
            project.id,
            kind=ClaimKind.PAPER_METHOD,
            claim_type=ClaimType.USER_REQUIREMENT,
            title="Bad combination",
            statement="A paper method cannot be a user requirement",
        )
    except UsageError:
        pass
    else:
        raise AssertionError("incompatible kind/type must be refused")


def test_legacy_claim_kind_is_derived(services, project):
    claim = services.claim_service.add_claim(
        project.id,
        claim_type=ClaimType.INFERRED_REQUIREMENT,
        title="Legacy typed claim",
        statement="Stored rows remain valid",
        derivation_note="A legacy row implies this requirement without a paper quote.",
    )
    assert claim.kind is ClaimKind.INFERRED_REQUIREMENT


def test_discovery_saves_immutable_source_metadata(services, project):
    hit = SourceHit(
        provider="memory",
        external_id="paper-1",
        title="Attention Is All You Need",
        doi="10.48550/arxiv.1706.03762",
    )
    from mayaworkbench.research.discovery import SearchRequest

    services.discovery_service.register_provider(
        type(
            "SeededProvider",
            (),
            {
                "name": "memory",
                "build_request": lambda self, query, *, limit: SearchRequest(provider="memory"),
                "search": lambda self, query, *, limit, fetcher=None: [hit],
            },
        )()
    )
    result = services.discovery_service.search(
        project, "attention", provider="memory", limit=1, save=True
    )
    assert result.saved
    versions = services.discovery_service.versions(project, hit.document_key)
    assert versions["source_metadata"]
    original_hash = versions["source_metadata"][0]["metadata_sha256"]
    again = services.discovery_service.versions(project, hit.document_key)
    assert again["source_metadata"][0]["metadata_sha256"] == original_hash


def test_discovery_rejects_unknown_provider(services, project):
    try:
        services.discovery_service.search(project, "x", provider="not-a-provider")
    except UsageError:
        pass
    else:
        raise AssertionError("unknown provider must be refused")


def test_typed_claim_and_discovery_cli_round_trip(tmp_path, repo, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    code, _ = run(["init", "--project", "typed-project"])
    assert code == 0
    code, payload = run(
        [
            "research",
            "claim",
            "add",
            "--project",
            "typed-project",
            "--kind",
            "VALIDATION_CLAIM",
            "--title",
            "Causality invariant",
            "--statement",
            "Future positions must not affect earlier ones",
            "--validation-id",
            "invariant-causal-mask",
        ]
    )
    assert code == 0
    assert payload["claim"]["kind"] == "VALIDATION_CLAIM"

    code, payload = run(
        ["research", "claims", "--project", "typed-project", "--kind", "VALIDATION_CLAIM"]
    )
    assert code == 0
    assert any(item["kind"] == "VALIDATION_CLAIM" for item in payload["claims"])

    code, payload = run(
        ["research", "search", "--project", "typed-project", "--provider", "memory", "attention"]
    )
    assert code == 0
    assert payload["provider"] == "memory"

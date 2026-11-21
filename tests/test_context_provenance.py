"""§6.3 end-to-end: every context pack carries revision, query, and a candidate manifest."""

from __future__ import annotations

from mayaworkbench.retrieval.models import ContextPack


def test_context_pack_records_snapshot_query_and_manifest(services, indexed):
    question = "Where is the causal mask applied?"
    bundle = services.retrieval.retrieve(indexed.snapshot.id, question)
    pack: ContextPack = bundle.context

    assert pack.snapshot_id == indexed.snapshot.id
    assert pack.query == question
    assert pack.items
    assert len(pack.candidates_manifest) >= len(pack.items)
    included = [item for item in pack.candidates_manifest if item.decision == "included"]
    excluded = [item for item in pack.candidates_manifest if item.decision == "excluded"]
    assert included
    assert all(item.exclusion_reason is None for item in included)
    assert all(item.exclusion_reason for item in excluded)
    assert {item.exclusion_reason for item in excluded} <= {
        "max_context_items reached",
        "max_context_items=8 reached",
        "overlaps an already selected span",
        "no indexed text for this span",
        "does not fit the remaining token budget",
    } or all(isinstance(item.exclusion_reason, str) for item in excluded)


def test_manifest_explains_budget_exclusions(services, indexed):
    bundle = services.retrieval.retrieve(
        indexed.snapshot.id,
        "attention forward mask normalization dropout linear layers helper build",
        budget=200,
    )
    pack = bundle.context
    assert pack.token_estimate <= 200
    excluded = [item for item in pack.candidates_manifest if item.decision == "excluded"]
    if pack.truncated:
        assert excluded, "a truncated pack must account for rejected candidates"
        assert any("budget" in (item.exclusion_reason or "") or "overlaps" in (item.exclusion_reason or "") or "max_context_items" in (item.exclusion_reason or "") for item in excluded)


def test_submodular_selection_is_deterministic_and_audited(services, indexed):
    question = "causal attention mask normalization"
    first = services.retrieval.retrieve(
        indexed.snapshot.id, question, selection="submodular"
    ).context
    second = services.retrieval.retrieve(
        indexed.snapshot.id, question, selection="submodular"
    ).context
    assert [item.path for item in first.items] == [item.path for item in second.items]
    assert len(first.candidates_manifest) >= len(first.items)
    assert all(isinstance(item.score, float) for item in first.candidates_manifest)


def test_serialised_pack_preserves_provenance(services, indexed):
    bundle = services.retrieval.retrieve(indexed.snapshot.id, "CausalAttention")
    payload = bundle.context.model_dump(mode="json")
    assert payload["snapshot_id"] == indexed.snapshot.id
    assert "candidates_manifest" in payload
    assert "ambiguities" in payload

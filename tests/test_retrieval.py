from __future__ import annotations

import pytest

from mayaworkbench.retrieval.service import compound_identifiers, extract_identifiers


def test_identifiers_and_compounds_from_prose():
    identifiers = extract_identifiers("How is causal masking implemented in the attention module?")
    assert "causal" in identifiers
    assert "masking" in identifiers
    compounds = compound_identifiers(["causal", "masking", "future"])
    assert "CausalMasking" in compounds
    assert "causal_masking" in compounds
    assert "causalMasking" in compounds


def test_retrieval_returns_symbols_and_context(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "Where is CausalAttention defined?"
    )
    assert bundle.symbols
    assert bundle.symbols[0].qualified_name == "CausalAttention"
    assert bundle.context.has_evidence()
    assert "attention.py" in bundle.context.files_covered
    assert bundle.diagnostics.identifiers


def test_context_items_correspond_to_real_indexed_lines(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "causal mask in forward", budget=1200
    )
    assert bundle.context.items
    for item in bundle.context.items:
        indexed_file = indexed.services.files.get(item.file_id)
        assert item.start_line >= 1
        assert item.end_line <= indexed_file.line_count
        span = indexed.services.files.read_span(item.file_id, item.start_line, item.end_line)
        rendered = "\n".join(f"{line_no}: {text}" for line_no, text in span)
        assert rendered == item.text


def test_token_budget_is_respected(indexed):
    budget = 600
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "attention forward mask torch tensor", budget=budget
    )
    assert bundle.context.token_budget == budget
    assert bundle.context.token_estimate <= budget
    assert bundle.context.truncated is True


def test_context_records_excluded_candidates(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "attention forward mask torch tensor def class import", budget=500
    )
    assert bundle.context.excluded
    assert all(excluded.reason for excluded in bundle.context.excluded)


def test_low_budget_still_returns_something_or_explains_why(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "attention", budget=220
    )
    assert bundle.context.token_estimate <= 220
    if not bundle.context.items:
        assert bundle.context.excluded


def test_tiny_budget_is_rejected_instead_of_silently_raised(indexed):
    from mayaworkbench.errors import UsageError

    with pytest.raises(UsageError, match="at least 200"):
        indexed.services.retrieval.retrieve(indexed.snapshot.id, "attention", budget=50)


def test_one_accidental_keyword_is_not_sufficient_evidence(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "torch banana plutonium"
    )
    assert bundle.context.items  # torch matches real lines
    assert bundle.diagnostics.evidence_sufficient is False
    assert any("not enough to answer" in note for note in bundle.diagnostics.notes)


def test_nonsense_question_yields_no_evidence(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "zzzqqqxyz plutonium banana"
    )
    assert not bundle.symbols
    assert not bundle.context.has_evidence()
    assert any("no lexical or symbol evidence" in note for note in bundle.diagnostics.notes)


def test_relationship_expansion_adds_related_symbols(indexed):
    bundle = indexed.services.retrieval.retrieve(
        indexed.snapshot.id, "build_attention"
    )
    reasons = [symbol.match_reason for symbol in bundle.symbols]
    assert any("same-file" in reason or "exact name" in reason for reason in reasons)
    assert bundle.symbols

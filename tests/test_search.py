from __future__ import annotations

import pytest

from mayaworkbench.domain.enums import EdgeConfidence


def test_identifier_aware_lexical_search(indexed):
    hits, expression = indexed.services.search.search_text(
        indexed.snapshot.id, "attention", mode="lexical"
    )
    assert hits
    assert expression
    assert any("CausalAttention" in hit.text for hit in hits)


def test_multi_term_question_broadens_beyond_single_line(indexed):
    hits, expression = indexed.services.search.search_text(
        indexed.snapshot.id, "causal masking future positions"
    )
    assert hits
    assert expression and "OR" in expression
    assert len({hit.path for hit in hits}) > 1


def test_exact_search_is_literal(indexed):
    hits, expression = indexed.services.search.search_text(
        indexed.snapshot.id, "Multi-head causal self attention", mode="exact"
    )
    assert expression is None
    assert hits
    assert all("Multi-head causal self attention" in hit.text for hit in hits)


def test_case_sensitivity_is_respected(indexed):
    insensitive, _ = indexed.services.search.search_text(
        indexed.snapshot.id, "causalattention", mode="exact"
    )
    sensitive, _ = indexed.services.search.search_text(
        indexed.snapshot.id, "causalattention", mode="exact", case_sensitive=True
    )
    assert insensitive
    assert sensitive == []


def test_fts_operator_words_are_literal_terms(indexed):
    # These would be FTS5 syntax errors if terms were interpolated unquoted.
    for query in ("OR", "NOT", "NEAR(", "a OR b NOT c"):
        hits, _expression = indexed.services.search.search_text(
            indexed.snapshot.id, query, mode="lexical"
        )
        assert isinstance(hits, list)
    hits, _ = indexed.services.search.search_text(indexed.snapshot.id, "NEAR(")
    assert isinstance(hits, list)


def test_non_ascii_query_does_not_become_noise(indexed):
    hits, expression = indexed.services.search.search_text(
        indexed.snapshot.id, "naïve"
    )
    assert hits == [] or expression
    assert expression is None or "na" not in expression.split(" OR ")


def test_invalid_regex_is_a_usage_error(indexed):
    from mayaworkbench.errors import UsageError

    with pytest.raises(UsageError, match="Invalid regular expression"):
        indexed.services.search.search_text(indexed.snapshot.id, "(", mode="regex")


def test_regex_search(indexed):
    hits, _ = indexed.services.search.search_text(
        indexed.snapshot.id, r"def \w+\(self", mode="regex"
    )
    assert hits
    assert all("def " in hit.text for hit in hits)


def test_path_prefix_filter(indexed):
    hits, _ = indexed.services.search.search_text(
        indexed.snapshot.id, "import", mode="exact", path_prefix="tests"
    )
    assert hits
    assert all(hit.path.startswith("tests/") for hit in hits)


def test_path_prefix_can_name_a_single_file(indexed):
    hits, _ = indexed.services.search.search_text(
        indexed.snapshot.id, "CausalAttention", mode="exact", path_prefix="attention.py"
    )
    assert hits
    assert {hit.path for hit in hits} == {"attention.py"}


def test_lexical_scores_rank_better_matches_higher(indexed):
    hits, _ = indexed.services.search.search_text(
        indexed.snapshot.id, "torch", mode="lexical"
    )
    assert len(hits) > 2
    scores = [hit.score for hit in hits]
    # FTS5 rank is negative-is-better; the exposed score must not invert it.
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_symbol_search_ranks_exact_match_first(indexed):
    hits = indexed.services.search.search_symbols(indexed.snapshot.id, "CausalAttention")
    assert hits
    assert hits[0].qualified_name == "CausalAttention"
    assert hits[0].match_reason == "exact name match"
    assert hits[0].score == 1.0


def test_symbol_search_matches_methods_and_functions(indexed):
    assert indexed.services.search.search_symbols(indexed.snapshot.id, "build_attention")
    assert indexed.services.search.search_symbols(indexed.snapshot.id, "CausalAttention.forward")


def test_symbol_detail_includes_source_and_relationships(indexed):
    hits = indexed.services.search.search_symbols(indexed.snapshot.id, "build_attention")
    detail = indexed.services.search.get_symbol(hits[0].symbol_id)
    assert "def build_attention" in detail.source
    assert "CausalAttention(d_model=d_model" in detail.source
    assert detail.relationships
    calls = [relationship for relationship in detail.relationships if relationship.edge_type == "CALLS"]
    assert calls
    assert any(relationship.confidence == EdgeConfidence.HIGH.value for relationship in calls)


def test_lexical_hits_report_their_exact_line_numbers(indexed):
    hits, _ = indexed.services.search.search_text(
        indexed.snapshot.id, "masked_fill", mode="exact"
    )
    assert hits
    source_lines = (indexed.repo / "attention.py").read_text(encoding="utf-8").splitlines()
    hit = next(hit for hit in hits if hit.path == "attention.py")
    assert "masked_fill" in source_lines[hit.line_no - 1]


def test_symbol_lookup_by_qualified_name(indexed):
    matches = indexed.services.symbols.find_by_qualified(indexed.snapshot.id, "Model.forward")
    assert matches
    assert matches[0].name == "forward"
    assert matches[0].kind.value == "method"

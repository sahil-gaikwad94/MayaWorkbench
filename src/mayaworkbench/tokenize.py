"""Identifier-aware lexical tokenisation.

Code search fails if the tokenizer only understands prose. ``attention`` must
match ``CausalAttentionModule`` and ``attention_mask``; ``kv_cache`` must match
``KVCache``. So each indexed line is expanded into:

* every raw identifier (so a full ``CausalAttentionModule`` query still hits),
* its camelCase / snake_case parts (so partial concept queries hit).

The raw, unmodified line lives in ``file_lines`` for exact and regex search.
Queries are rebuilt from extracted alphanumerics only, so user input can never
inject FTS5 operators or produce a syntax error.
"""

from __future__ import annotations

import re

# Unicode-aware word characters: non-ASCII identifiers and prose ("naïve") must
# survive tokenisation; FTS5's unicode61 tokenizer folds diacritics for matching.
_IDENT_RE = re.compile(r"[^\W]+")
_CAMEL_LOWER_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_QUOTED_RE = re.compile(r'"([^"]*)"')


def split_identifier(token: str) -> list[str]:
    """Split one identifier into searchable parts.

    ``CausalAttentionModule`` -> ``["Causal", "Attention", "Module"]``
    ``KVCache`` -> ``["KV", "Cache"]``
    ``attention_mask`` -> ``["attention", "mask"]``
    """
    parts: list[str] = []
    for chunk in token.split("_"):
        if not chunk:
            continue
        chunk = _CAMEL_ACRONYM.sub(" ", chunk)
        chunk = _CAMEL_LOWER_UPPER.sub(" ", chunk)
        parts.extend(piece for piece in chunk.split() if piece)
    return parts or [token]


def expand_line(line: str) -> str:
    """Return the token text stored in the FTS index for one source line."""
    expanded: list[str] = []
    for match in _IDENT_RE.finditer(line):
        raw = match.group(0)
        expanded.append(raw)
        parts = split_identifier(raw)
        if len(parts) > 1:
            expanded.extend(parts)
    return " ".join(expanded)


def query_terms(query: str) -> tuple[list[str], list[str]]:
    """Extract ``(phrases, terms)`` from a user query.

    Only alphanumerics survive, which makes the generated MATCH expression
    syntactically safe regardless of what the user typed.
    """
    phrases: list[str] = []
    for quoted in _QUOTED_RE.findall(query):
        words: list[str] = []
        for token in _IDENT_RE.findall(quoted):
            words.extend(split_identifier(token))
        if words:
            phrases.append(" ".join(words))
    remainder = _QUOTED_RE.sub(" ", query)
    terms: list[str] = []
    for token in _IDENT_RE.findall(remainder):
        terms.extend(split_identifier(token))
    return _dedupe(phrases), _dedupe(terms)


def quote_term(term: str) -> str:
    """Quote one token so FTS5 operators (OR, NOT, NEAR) stay literal text."""
    return '"' + term.replace('"', '""') + '"'


def build_match_expressions(query: str) -> list[str]:
    """Candidate FTS5 MATCH expressions from most to least strict.

    The retrieval layer tries them in order and stops at the first expression
    that yields hits, so precision is preferred over recall.
    """
    phrases, terms = query_terms(query)
    quoted_terms = [quote_term(term) for term in terms]
    expressions: list[str] = []
    if phrases:
        for phrase in phrases:
            expressions.append(quote_term(phrase))
        if terms:
            expressions.append(
                " AND ".join(quote_term(phrase) for phrase in phrases)
                + " AND "
                + " AND ".join(quoted_terms)
            )
    if quoted_terms:
        expressions.append(" AND ".join(quoted_terms))
    if len(quoted_terms) > 1:
        expressions.append(" OR ".join(quoted_terms))
    return _dedupe(expressions)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "split_identifier",
    "expand_line",
    "query_terms",
    "quote_term",
    "build_match_expressions",
]

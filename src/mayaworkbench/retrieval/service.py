"""Retrieval orchestration.

Given a question, the service:

1. extracts candidate identifiers and quoted phrases;
2. searches symbols and text (BM25, exact fallback, regex when asked);
3. optionally expands one hop through resolved relationships;
4. packs the surviving evidence into a bounded, inspectable context.

Embeddings are deliberately absent: exact identifiers and symbols are the
baseline that must work first (§8.5). Diagnostics name everything that happened.
"""

from __future__ import annotations

import re
from collections import defaultdict

from mayaworkbench.config import RetrievalSettings
from mayaworkbench.errors import UsageError
from mayaworkbench.retrieval.context import ContextPacker
from mayaworkbench.retrieval.models import (
    RetrievalBundle,
    RetrievalDiagnostics,
    SearchHit,
    SearchMode,
    SymbolHit,
)
from mayaworkbench.retrieval.search import SearchService

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_QUOTED_RE = re.compile(r'"([^"]+)"')
MIN_TOKEN_BUDGET = 200

STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "any",
    "are",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "can",
    "could",
    "does",
    "doing",
    "done",
    "each",
    "for",
    "from",
    "get",
    "gets",
    "had",
    "has",
    "have",
    "here",
    "how",
    "into",
    "its",
    "just",
    "like",
    "make",
    "many",
    "more",
    "most",
    "much",
    "must",
    "not",
    "now",
    "only",
    "other",
    "our",
    "out",
    "over",
    "should",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "use",
    "used",
    "uses",
    "using",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "work",
    "works",
    "would",
    "you",
    "your",
    "code",
    "file",
    "files",
    "function",
    "functions",
    "class",
    "classes",
    "module",
    "method",
    "implementation",
    "implemented",
    "repository",
    "repo",
    "project",
    "am",
    "be",
    "been",
    "did",
    "do",
    "does",
    "define",
    "defined",
    "definition",
    "explain",
    "find",
    "is",
    "it",
    "locate",
    "located",
    "mean",
    "means",
    "please",
    "show",
    "tell",
    "was",
    "were",
}


class RetrievalService:
    def __init__(
        self,
        search: SearchService,
        packer: ContextPacker,
        settings: RetrievalSettings,
    ):
        self.search = search
        self.packer = packer
        self.settings = settings

    def retrieve(
        self,
        snapshot_id: str,
        question: str,
        *,
        budget: int | None = None,
        mode: SearchMode = "auto",
        expand_relationships: bool = True,
        selection: str = "greedy",
        use_semantic: bool = False,
        use_ppr: bool = False,
    ) -> RetrievalBundle:
        budget = budget or self.settings.default_token_budget
        if budget < MIN_TOKEN_BUDGET:
            raise UsageError(
                f"Context budget must be at least {MIN_TOKEN_BUDGET} tokens",
                hint="A smaller budget cannot fit a useful source span; pass --budget with a larger value.",
            )
        identifiers = extract_identifiers(question)
        phrases = [phrase.strip() for phrase in _QUOTED_RE.findall(question) if phrase.strip()]
        notes: list[str] = []
        # Filler words ("where is the ... defined") only add noise to BM25; search
        # with the identifiers and quoted phrases instead of the raw sentence.
        search_query = " ".join(
            [f'"{phrase}"' for phrase in phrases] + identifiers
        ) or question

        symbols: dict[str, SymbolHit] = {}
        for identifier in identifiers[:10]:
            for hit in self.search.search_symbols(snapshot_id, identifier, limit=6):
                existing = symbols.get(hit.symbol_id)
                if existing is None or hit.score > existing.score:
                    symbols[hit.symbol_id] = hit
        # Concept questions name things in prose ("causal masking") while code
        # uses one identifier ("CausalMasking"). Try the joined forms too.
        for compound in compound_identifiers(identifiers)[:12]:
            for hit in self.search.search_symbols(snapshot_id, compound, limit=3):
                if hit.match_reason != "exact name match":
                    continue
                existing = symbols.get(hit.symbol_id)
                if existing is None or hit.score > existing.score:
                    symbols[hit.symbol_id] = hit.model_copy(
                        update={"match_reason": f"exact name match for {compound}"}
                    )
        if not symbols and _IDENTIFIER_RE.fullmatch(question.strip()):
            for hit in self.search.search_symbols(snapshot_id, question.strip(), limit=10):
                symbols[hit.symbol_id] = hit

        hits, expression = self.search.search_text(
            snapshot_id, search_query, mode=mode, limit=self.settings.max_hits
        )
        if len(hits) < 3:
            for identifier in identifiers[:6]:
                more, _expr = self.search.search_text(
                    snapshot_id, identifier, mode="lexical", limit=8
                )
                hits = _merge_hits(hits, more)

        expanded = 0
        if expand_relationships and symbols:
            for hit in list(symbols.values())[:3]:
                expanded += self._expand_relationships(snapshot_id, hit, symbols)
            if expanded:
                notes.append(f"expanded {expanded} related symbol(s) one hop through resolved edges")

        ordered_symbols = sorted(symbols.values(), key=lambda hit: (-hit.score, hit.path, hit.start_line))[:24]
        ordered_hits = sorted(hits, key=lambda hit: (-hit.score, hit.path, hit.line_no))[
            : self.settings.max_hits
        ]
        if use_ppr and ordered_symbols:
            from mayaworkbench.retrieval.semantic import ppr_scores

            graph: dict[str, list[str]] = defaultdict(list)
            for symbol in ordered_symbols:
                for relation in self.search.symbols.edges_from(symbol.symbol_id):
                    target_name = str(relation.get("target_name") or "")
                    if target_name:
                        graph[symbol.qualified_name].append(target_name)
            seeds = [symbol.qualified_name for symbol in ordered_symbols[:3]]
            scores = ppr_scores(graph, seeds)
            if scores:
                for symbol in ordered_symbols:
                    symbol.score = round(
                        symbol.score + 0.5 * scores.get(symbol.qualified_name, 0.0), 6
                    )
                ordered_symbols.sort(
                    key=lambda hit: (-hit.score, hit.path, hit.start_line)
                )
                notes.append("damped PPR applied to symbol ranking (optional mode)")
        if use_semantic:
            from mayaworkbench.retrieval.semantic import LexicalReranker

            ranker = LexicalReranker()
            ordered_hits = [
                SearchHit(**item)
                for item in ranker.rerank(
                    question,
                    [hit.model_dump() for hit in ordered_hits],
                    limit=self.settings.max_hits,
                )
            ]
            notes.append("optional deterministic reranker applied to retrieved spans")
        if not ordered_hits and not ordered_symbols:
            notes.append("no lexical or symbol evidence matched this question")

        pack = self.packer.build(
            hits=ordered_hits,
            symbols=ordered_symbols,
            budget=budget,
            snapshot_id=snapshot_id,
            query=question,
            selection=selection,  # type: ignore[arg-type]
        )
        if pack.truncated:
            notes.append(f"context was truncated to a {budget}-token budget")

        # A single accidental keyword match is not evidence for a multi-concept
        # question. Require coverage of the question's terms when no symbol was
        # recognised; otherwise the answer would be built from unrelated lines.
        matched = _matched_identifier_count(identifiers, pack)
        sufficient = pack.has_evidence()
        required = 0 if not identifiers else (1 if len(identifiers) == 1 else min(len(identifiers), 3))
        if sufficient and not ordered_symbols and matched < required:
            sufficient = False
            notes.append(
                f"retrieved matches cover {matched} of {len(identifiers)} question term(s); "
                "this is not enough to answer from evidence"
            )

        diagnostics = RetrievalDiagnostics(
            question=question,
            identifiers=identifiers,
            phrases=phrases,
            fts_expression=expression,
            text_candidates=len(ordered_hits),
            symbol_candidates=len(ordered_symbols),
            mode=mode,
            evidence_sufficient=sufficient,
            notes=notes,
        )
        return RetrievalBundle(
            diagnostics=diagnostics,
            hits=ordered_hits,
            symbols=ordered_symbols,
            context=pack,
        )

    def _expand_relationships(
        self, snapshot_id: str, hit: SymbolHit, symbols: dict[str, SymbolHit]
    ) -> int:
        added = 0
        for direction in ("outgoing", "incoming"):
            edges = (
                self.search.symbols.edges_from(hit.symbol_id)
                if direction == "outgoing"
                else self.search.symbols.edges_to(hit.symbol_id)
            )
            for edge in edges:
                if added >= 6:
                    return added
                if str(edge["confidence"]) == "LOW":
                    continue
                target_id = (
                    edge["target_symbol_id"] if direction == "outgoing" else edge["source_symbol_id"]
                )
                if not target_id or target_id in symbols:
                    continue
                try:
                    symbol = self.search.symbols.get(str(target_id))
                except Exception:  # pragma: no cover - dangling edge
                    continue
                symbols[symbol.id] = SymbolHit(
                    symbol_id=symbol.id,
                    file_id=symbol.file_id,
                    path=symbol.path,
                    kind=symbol.kind.value,
                    name=symbol.name,
                    qualified_name=symbol.qualified_name,
                    signature=symbol.signature,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    docstring=symbol.docstring,
                    score=0.3,
                    match_reason=f"{direction} {edge['edge_type']} of {hit.qualified_name}",
                )
                added += 1
        return added


def compound_identifiers(identifiers: list[str]) -> list[str]:
    """Join adjacent question terms into likely code identifiers."""
    candidates: list[str] = []
    for size in (3, 2):
        for index in range(0, max(0, len(identifiers) - size + 1)):
            parts = identifiers[index : index + size]
            if len(parts) < size:
                continue
            pascal = "".join(part[:1].upper() + part[1:].lower() for part in parts)
            snake = "_".join(part.lower() for part in parts)
            camel = parts[0].lower() + "".join(
                part[:1].upper() + part[1:].lower() for part in parts[1:]
            )
            candidates.extend([pascal, snake, camel])
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if len(candidate) < 6 or candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def extract_identifiers(question: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in _IDENTIFIER_RE.finditer(question):
        token = match.group(0)
        if len(token) < 3:
            continue
        if token.lower() in STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
        if len(result) >= 16:
            break
    return result


def _matched_identifier_count(identifiers: list[str], pack) -> int:  # type: ignore[no-untyped-def]
    if not identifiers or not pack.items:
        return 0
    haystacks = [item.text.lower() for item in pack.items]
    matched = 0
    for identifier in identifiers:
        needle = identifier.split(".")[-1].lower()
        if len(needle) < 3:
            continue
        # Whole-token match: "zzz" inside "zzz_post" is not evidence of "zzz".
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])"
        )
        if any(pattern.search(text) for text in haystacks):
            matched += 1
    return matched


def _merge_hits(primary: list[SearchHit], extra: list[SearchHit]) -> list[SearchHit]:
    merged: dict[tuple[str, int], SearchHit] = {
        (hit.path, hit.line_no): hit for hit in primary
    }
    for hit in extra:
        key = (hit.path, hit.line_no)
        existing = merged.get(key)
        if existing is None or hit.score > existing.score:
            merged[key] = hit
    return list(merged.values())


__all__ = ["RetrievalService", "extract_identifiers", "compound_identifiers", "STOPWORDS"]

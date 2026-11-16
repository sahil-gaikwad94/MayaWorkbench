"""Search services.

Lexical search uses BM25 over identifier-expanded tokens. Exact and regex
search scan the raw per-line text, which keeps identifier-level queries precise
and makes regex behaviour predictable.

Nothing here fabricates relevance: if every query form returns no hits, the
caller receives an empty result and must say so.
"""

from __future__ import annotations

import re
from typing import Any

from mayaworkbench.domain.enums import SymbolKind
from mayaworkbench.errors import UsageError
from mayaworkbench.retrieval.models import (
    SearchHit,
    SearchMode,
    SymbolDetail,
    SymbolHit,
    SymbolRelationship,
)
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import FileStore, SymbolStore, like_pattern
from mayaworkbench.tokenize import build_match_expressions, query_terms, quote_term

MAX_SOURCE_LINES_FOR_SYMBOL = 220


class SearchService:
    def __init__(self, db: Database, files: FileStore, symbols: SymbolStore):
        self.db = db
        self.files = files
        self.symbols = symbols

    # ------------------------------------------------------------------- text
    def search_text(
        self,
        snapshot_id: str,
        query: str,
        *,
        mode: SearchMode = "auto",
        limit: int = 40,
        path_prefix: str | None = None,
        case_sensitive: bool = False,
    ) -> tuple[list[SearchHit], str | None]:
        """Return ``(hits, fts_expression_used)``."""
        if not query.strip():
            return [], None
        limit = max(1, min(limit, 1000))
        if mode in ("auto", "lexical"):
            expressions = (
                build_match_expressions(query) if mode == "auto" else [self._single_expression(query)]
            )
            primary: list[SearchHit] = []
            used: str | None = None
            for expression in expressions:
                if not expression:
                    continue
                primary = self._fts(snapshot_id, expression, limit, path_prefix)
                if primary:
                    used = expression
                    break
            if mode == "lexical":
                return primary, used
            # Multi-concept questions rarely put every term on one line. When the
            # strict AND form is thin, add OR matches at lower weight so BM25
            # coverage decides the rest of the ranking.
            if primary and len(primary) >= 5:
                return primary, used
            broad = self._broad_expression(query)
            if broad and broad != used:
                extra = self._fts(snapshot_id, broad, limit * 2, path_prefix)
                if extra:
                    merged = {(hit.path, hit.line_no): hit for hit in primary}
                    for hit in extra:
                        key = (hit.path, hit.line_no)
                        if key not in merged:
                            merged[key] = hit.model_copy(
                                update={"score": round(hit.score * 0.6, 6)}
                            )
                    if merged:
                        combined = sorted(
                            merged.values(), key=lambda hit: (-hit.score, hit.path, hit.line_no)
                        )[: max(limit, len(primary))]
                        label = f"{used} (broadened to {broad})" if used else broad
                        return combined, label
            if primary:
                return primary, used
        if mode in ("auto", "exact"):
            hits = self._exact(snapshot_id, query, limit, path_prefix, case_sensitive)
            if hits:
                return hits, None
            if mode == "exact":
                return [], None
        if mode in ("auto", "regex"):
            # Validate before hitting SQLite so users get the actual regex error
            # instead of "user-defined function raised exception". In auto mode a
            # query is not necessarily a regex, so an invalid pattern simply ends
            # the fallback chain instead of failing the search.
            try:
                re.compile(query)
            except re.error as exc:
                if mode == "regex":
                    raise UsageError(f"Invalid regular expression: {exc}") from exc
                return [], None
        if mode in ("auto", "regex"):
            hits = self._regex(snapshot_id, query, limit, path_prefix)
            if hits:
                return hits, None
        return [], None

    def _single_expression(self, query: str) -> str:
        expressions = build_match_expressions(query)
        return expressions[0] if expressions else ""

    def _broad_expression(self, query: str) -> str:
        _phrases, terms = query_terms(query)
        return " OR ".join(quote_term(term) for term in terms) if len(terms) > 1 else ""

    def _fts(
        self, snapshot_id: str, expression: str, limit: int, path_prefix: str | None
    ) -> list[SearchHit]:
        params: list[Any] = [expression, snapshot_id]
        path_clause = ""
        if path_prefix:
            path_clause = " AND f.path LIKE ? ESCAPE '\\'"
            params.append(like_pattern(path_prefix.rstrip("/")) + "%")
        params.append(limit)
        rows = self.db.fetchall(
            f"""
            SELECT l.file_id, f.path, l.line_no, l.text, bm25(search_lines) AS rank
            FROM search_lines
            JOIN file_lines l
              ON l.snapshot_id = search_lines.snapshot_id
             AND l.file_id = search_lines.file_id
             AND l.line_no = search_lines.line_no
            JOIN indexed_files f ON f.id = l.file_id
            WHERE search_lines MATCH ? AND l.snapshot_id = ?{path_clause}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        )
        hits: list[SearchHit] = []
        for row in rows:
            # FTS5 bm25() is negative and lower is better. Map it to (0, 1) where
            # a higher score means a better match, without inverting the order.
            quality = max(0.0, -float(row["rank"]))
            hits.append(
                SearchHit(
                    file_id=str(row["file_id"]),
                    path=str(row["path"]),
                    line_no=int(row["line_no"]),
                    text=str(row["text"]),
                    score=round(quality / (1.0 + quality), 6),
                    match_kind="lexical",
                )
            )
        return hits

    def _exact(
        self,
        snapshot_id: str,
        query: str,
        limit: int,
        path_prefix: str | None,
        case_sensitive: bool,
    ) -> list[SearchHit]:
        params: list[Any] = [snapshot_id]
        path_clause = ""
        if path_prefix:
            path_clause = " AND f.path LIKE ? ESCAPE '\\'"
        if case_sensitive:
            condition = "instr(l.text, ?) > 0"
            params.append(query)
        else:
            condition = "lower(l.text) LIKE lower(?) ESCAPE '\\'"
            params.append("%" + like_pattern(query) + "%")
        if path_prefix:
            params.append(like_pattern(path_prefix.rstrip("/")) + "%")
        params.append(limit)
        rows = self.db.fetchall(
            f"""
            SELECT l.file_id, f.path, l.line_no, l.text
            FROM file_lines l
            JOIN indexed_files f ON f.id = l.file_id
            WHERE l.snapshot_id = ? AND {condition}{path_clause}
            ORDER BY f.path, l.line_no
            LIMIT ?
            """,
            params,
        )
        return [
            SearchHit(
                file_id=str(row["file_id"]),
                path=str(row["path"]),
                line_no=int(row["line_no"]),
                text=str(row["text"]),
                score=1.0,
                match_kind="exact",
            )
            for row in rows
        ]

    def _regex(
        self, snapshot_id: str, pattern: str, limit: int, path_prefix: str | None
    ) -> list[SearchHit]:
        params: list[Any] = [snapshot_id, pattern]
        path_clause = ""
        if path_prefix:
            path_clause = " AND f.path LIKE ? ESCAPE '\\'"
            params.append(like_pattern(path_prefix.rstrip("/")) + "%")
        params.append(limit)
        rows = self.db.fetchall(
            f"""
            SELECT l.file_id, f.path, l.line_no, l.text
            FROM file_lines l
            JOIN indexed_files f ON f.id = l.file_id
            WHERE l.snapshot_id = ? AND regexp(?, l.text){path_clause}
            ORDER BY f.path, l.line_no
            LIMIT ?
            """,
            params,
        )
        return [
            SearchHit(
                file_id=str(row["file_id"]),
                path=str(row["path"]),
                line_no=int(row["line_no"]),
                text=str(row["text"]),
                score=0.9,
                match_kind="regex",
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- symbols
    def search_symbols(
        self,
        snapshot_id: str,
        query: str,
        *,
        kind: SymbolKind | None = None,
        limit: int = 20,
    ) -> list[SymbolHit]:
        if not query.strip():
            return []
        limit = max(1, min(limit, 200))
        results: list[SymbolHit] = []
        seen: set[str] = set()
        for symbol in self.symbols.search(snapshot_id, query, kind=kind, limit=limit):
            if symbol.id in seen:
                continue
            seen.add(symbol.id)
            if symbol.qualified_name == query or symbol.name == query:
                score, reason = 1.0, "exact name match"
            elif symbol.qualified_name.lower().startswith(query.lower()):
                score, reason = 0.8, "qualified-name prefix"
            elif symbol.name.lower().startswith(query.lower()):
                score, reason = 0.7, "name prefix"
            else:
                score, reason = 0.45, "substring match"
            results.append(
                SymbolHit(
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
                    score=score,
                    match_reason=reason,
                )
            )
        results.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        return results

    def get_symbol(self, symbol_id: str) -> SymbolDetail:
        symbol = self.symbols.get(symbol_id)
        span = self.files.read_span(symbol.file_id, symbol.start_line, symbol.end_line)
        truncated = False
        if len(span) > MAX_SOURCE_LINES_FOR_SYMBOL:
            span = span[:MAX_SOURCE_LINES_FOR_SYMBOL]
            truncated = True
        source = "\n".join(text for _line, text in span)
        relationships: list[SymbolRelationship] = []
        for edge in self.symbols.edges_from(symbol_id):
            relationships.append(
                SymbolRelationship(
                    edge_type=str(edge["edge_type"]),
                    direction="outgoing",
                    line=int(edge["line"]),
                    confidence=str(edge["confidence"]),
                    target_name=str(edge["target_name"]),
                    path=edge["target_path"],
                    qualified_name=edge["target_qualified_name"],
                    start_line=edge["target_start_line"],
                    kind=edge["target_kind"],
                )
            )
        for edge in self.symbols.edges_to(symbol_id):
            relationships.append(
                SymbolRelationship(
                    edge_type=str(edge["edge_type"]),
                    direction="incoming",
                    line=int(edge["line"]),
                    confidence=str(edge["confidence"]),
                    target_name=str(edge["source_qualified_name"] or edge["target_name"]),
                    path=edge["source_path"],
                    qualified_name=edge["source_qualified_name"],
                    start_line=edge["source_start_line"],
                    kind=edge["source_kind"],
                )
            )
        return SymbolDetail(
            symbol_id=symbol.id,
            path=symbol.path,
            kind=symbol.kind.value,
            name=symbol.name,
            qualified_name=symbol.qualified_name,
            signature=symbol.signature,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            docstring=symbol.docstring,
            decorators=list(symbol.decorators),
            confidence=symbol.confidence.value,
            source=source,
            relationships=relationships,
            truncated_source=truncated,
        )


__all__ = ["SearchService"]

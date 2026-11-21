"""Retrieval data models.

Diagnostics are part of the contract: the selected FTS expression, candidate
counts, and rejected context items stay inspectable so ranking behaviour can be
debugged instead of guessed at (§8.7).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SearchMode = Literal["auto", "lexical", "exact", "regex"]


class SearchHit(BaseModel):
    file_id: str
    path: str
    line_no: int
    text: str
    score: float
    match_kind: Literal["lexical", "exact", "regex"]


class SymbolHit(BaseModel):
    symbol_id: str
    file_id: str
    path: str
    kind: str
    name: str
    qualified_name: str
    signature: str
    start_line: int
    end_line: int
    docstring: str | None = None
    score: float
    match_reason: str


class ContextItem(BaseModel):
    path: str
    file_id: str
    start_line: int
    end_line: int
    text: str
    reason: str
    kind: Literal["symbol", "match", "file_head"]
    score: float
    token_estimate: int
    truncated: bool = False


class ExcludedItem(BaseModel):
    path: str
    start_line: int
    end_line: int
    reason: str


class CandidateDecision(BaseModel):
    """Why a ranked candidate was admitted or excluded from the pack (§6.3)."""

    path: str
    start_line: int
    end_line: int
    score: float
    reason: str
    decision: Literal["included", "excluded"]
    exclusion_reason: str | None = None


class ContextPack(BaseModel):
    snapshot_id: str | None = None
    query: str | None = None
    items: list[ContextItem] = Field(default_factory=list)
    token_estimate: int = 0
    token_budget: int = 0
    estimator: str = ""
    files_covered: list[str] = Field(default_factory=list)
    excluded: list[ExcludedItem] = Field(default_factory=list)
    candidates_manifest: list[CandidateDecision] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    truncated: bool = False

    def has_evidence(self) -> bool:
        return bool(self.items)

    def spans_for(self, path: str) -> list[tuple[int, int, str]]:
        return [
            (item.start_line, item.end_line, item.text) for item in self.items if item.path == path
        ]


class RetrievalDiagnostics(BaseModel):
    question: str
    identifiers: list[str] = Field(default_factory=list)
    phrases: list[str] = Field(default_factory=list)
    fts_expression: str | None = None
    text_candidates: int = 0
    symbol_candidates: int = 0
    mode: str = "auto"
    evidence_sufficient: bool = True
    notes: list[str] = Field(default_factory=list)


class RetrievalBundle(BaseModel):
    diagnostics: RetrievalDiagnostics
    hits: list[SearchHit] = Field(default_factory=list)
    symbols: list[SymbolHit] = Field(default_factory=list)
    context: ContextPack


class SymbolRelationship(BaseModel):
    edge_type: str
    direction: Literal["outgoing", "incoming"]
    line: int
    confidence: str
    target_name: str
    path: str | None = None
    qualified_name: str | None = None
    start_line: int | None = None
    kind: str | None = None


class SymbolDetail(BaseModel):
    symbol_id: str
    path: str
    kind: str
    name: str
    qualified_name: str
    signature: str
    start_line: int
    end_line: int
    docstring: str | None = None
    decorators: list[str] = Field(default_factory=list)
    confidence: str
    source: str
    relationships: list[SymbolRelationship] = Field(default_factory=list)
    truncated_source: bool = False


__all__ = [
    "SearchHit",
    "SymbolHit",
    "ContextItem",
    "ExcludedItem",
    "CandidateDecision",
    "ContextPack",
    "RetrievalDiagnostics",
    "RetrievalBundle",
    "SymbolRelationship",
    "SymbolDetail",
    "SearchMode",
]

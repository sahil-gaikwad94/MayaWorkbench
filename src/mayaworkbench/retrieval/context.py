"""Context packing.

The packer turns ranked search evidence into bounded source spans for a model
or a human. It is a greedy approximation, not an optimum: items are merged when
they overlap, scored, then admitted by score density until the token budget is
exhausted. Every rejection is recorded with a reason, and the estimator used is
named in the output (§8.7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from mayaworkbench.config import RetrievalSettings
from mayaworkbench.domain.models import IndexedFile
from mayaworkbench.retrieval.models import (
    CandidateDecision,
    ContextItem,
    ContextPack,
    ExcludedItem,
    SearchHit,
    SymbolHit,
)
from mayaworkbench.storage.stores import FileStore

MIN_SHRUNK_LINES = 6


@dataclass
class _Candidate:
    file: IndexedFile
    start_line: int
    end_line: int
    reason: str
    kind: str
    score: float
    hit_count: int = 1

    @property
    def span_key(self) -> tuple[str, int, int]:
        return (self.file.id, self.start_line, self.end_line)


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return int(len(text) / chars_per_token) + 8


class ContextPacker:
    def __init__(self, files: FileStore, settings: RetrievalSettings):
        self.files = files
        self.settings = settings

    # ------------------------------------------------------------------- build
    def build(
        self,
        *,
        hits: list[SearchHit],
        symbols: list[SymbolHit],
        budget: int,
        max_items: int | None = None,
        snapshot_id: str | None = None,
        query: str | None = None,
        selection: Literal["greedy", "submodular"] = "greedy",
    ) -> ContextPack:
        budget = max(200, budget)
        context_lines = self.settings.span_context_lines
        max_span = self.settings.max_span_lines
        max_items = max_items or self.settings.max_context_items
        file_cache: dict[str, IndexedFile] = {}

        def file_for(file_id: str) -> IndexedFile:
            if file_id not in file_cache:
                file_cache[file_id] = self.files.get(file_id)
            return file_cache[file_id]

        candidates: list[_Candidate] = []
        for symbol in symbols:
            file = file_for(symbol.file_id)
            start = max(1, symbol.start_line - context_lines)
            end = min(file.line_count, max(symbol.end_line, symbol.start_line) + context_lines)
            if end - start + 1 > max_span:
                end = start + max_span - 1
            candidates.append(
                _Candidate(
                    file=file,
                    start_line=start,
                    end_line=end,
                    reason=f"{symbol.kind} {symbol.qualified_name} ({symbol.match_reason})",
                    kind="symbol",
                    score=symbol.score + 0.35,
                )
            )

        grouped: dict[str, list[SearchHit]] = {}
        for hit in hits:
            grouped.setdefault(hit.file_id, []).append(hit)
        for file_id, file_hits in grouped.items():
            file = file_for(file_id)
            clusters = _cluster_lines([hit.line_no for hit in file_hits], gap=context_lines * 2)
            best_score = max(hit.score for hit in file_hits)
            for cluster in clusters:
                start = max(1, min(cluster) - context_lines)
                end = min(file.line_count, max(cluster) + context_lines)
                if end - start + 1 > max_span:
                    end = start + max_span - 1
                candidates.append(
                    _Candidate(
                        file=file,
                        start_line=start,
                        end_line=end,
                        reason=f"{len(cluster)} matching line(s)",
                        kind="match",
                        score=best_score * 0.9,
                        hit_count=len(cluster),
                    )
                )

        merged = _merge_candidates(candidates, gap=context_lines)
        if selection == "submodular":
            merged = _submodular_order(merged, query or "")
        else:
            merged.sort(
                key=lambda candidate: (-candidate.score, candidate.file.path, candidate.start_line)
            )

        items: list[ContextItem] = []
        excluded: list[ExcludedItem] = []
        manifest: list[CandidateDecision] = []
        ambiguities: list[str] = []
        used_tokens = 0
        truncated = False
        seen_files: list[str] = []
        seen_spans: dict[str, list[tuple[int, int]]] = {}

        def record(
            candidate: _Candidate,
            decision: str,
            *,
            exclusion_reason: str | None = None,
        ) -> None:
            manifest.append(
                CandidateDecision(
                    path=candidate.file.path,
                    start_line=candidate.start_line,
                    end_line=candidate.end_line,
                    score=round(candidate.score, 6),
                    reason=candidate.reason,
                    decision=decision,  # type: ignore[arg-type]
                    exclusion_reason=exclusion_reason,
                )
            )

        for candidate in merged:
            if len(items) >= max_items:
                excluded.append(
                    ExcludedItem(
                        path=candidate.file.path,
                        start_line=candidate.start_line,
                        end_line=candidate.end_line,
                        reason=f"max_context_items={max_items} reached",
                    )
                )
                record(candidate, "excluded", exclusion_reason=f"max_context_items={max_items} reached")
                truncated = True
                continue

            if _overlaps(seen_spans, candidate.file.id, candidate.start_line, candidate.end_line):
                excluded.append(
                    ExcludedItem(
                        path=candidate.file.path,
                        start_line=candidate.start_line,
                        end_line=candidate.end_line,
                        reason="overlaps an already selected span",
                    )
                )
                record(candidate, "excluded", exclusion_reason="overlaps an already selected span")
                ambiguities.append(
                    f"{candidate.file.path}:{candidate.start_line}-{candidate.end_line} "
                    "overlaps an already selected span; a duplicate view was excluded"
                )
                continue

            remaining = budget - used_tokens
            span = self.files.read_span(candidate.file.id, candidate.start_line, candidate.end_line)
            if not span:
                excluded.append(
                    ExcludedItem(
                        path=candidate.file.path,
                        start_line=candidate.start_line,
                        end_line=candidate.end_line,
                        reason="no indexed text for this span",
                    )
                )
                record(candidate, "excluded", exclusion_reason="no indexed text for this span")
                continue
            text = _render_span(span)
            tokens = estimate_tokens(text, self.settings.chars_per_token)
            item_truncated = False
            if tokens > remaining:
                span, text, tokens, item_truncated = _shrink(span, remaining, self.settings.chars_per_token)
                if span is None:
                    excluded.append(
                        ExcludedItem(
                            path=candidate.file.path,
                            start_line=candidate.start_line,
                            end_line=candidate.end_line,
                            reason="does not fit the remaining token budget",
                        )
                    )
                    record(
                        candidate,
                        "excluded",
                        exclusion_reason="does not fit the remaining token budget",
                    )
                    ambiguities.append(
                        f"{candidate.file.path}:{candidate.start_line}-{candidate.end_line} "
                        f"(score {candidate.score:.3f}) was excluded by the token budget; "
                        "ranking evidence is incomplete"
                    )
                    truncated = True
                    continue
            item = ContextItem(
                path=candidate.file.path,
                file_id=candidate.file.id,
                start_line=span[0][0],
                end_line=span[-1][0],
                text=text,
                reason=candidate.reason,
                kind=candidate.kind,  # type: ignore[arg-type]
                score=round(candidate.score, 6),
                token_estimate=tokens,
                truncated=item_truncated,
            )
            items.append(item)
            record(candidate, "included")
            used_tokens += tokens
            if item_truncated:
                truncated = True
            if candidate.file.path not in seen_files:
                seen_files.append(candidate.file.path)
            seen_spans.setdefault(candidate.file.id, []).append((item.start_line, item.end_line))

        return ContextPack(
            snapshot_id=snapshot_id,
            query=query,
            items=items,
            token_estimate=used_tokens,
            token_budget=budget,
            estimator=f"chars/{self.settings.chars_per_token} plus 8 tokens per span",
            files_covered=seen_files,
            excluded=excluded,
            candidates_manifest=manifest,
            ambiguities=ambiguities,
            truncated=truncated,
        )


def _submodular_order(candidates: list[_Candidate], query: str) -> list[_Candidate]:
    """Greedy maximisation of a monotone submodular coverage objective.

    Value = relevance + marginal query-term coverage - redundancy with already
    selected spans. This is a deterministic approximation (not an optimality
    proof); the plain greedy baseline stays the default (§6.2, §6.3).
    """
    tokens = {token.lower() for token in re.findall(r"[A-Za-z_][\w-]{2,}", query)}
    remaining = list(candidates)
    ordered: list[_Candidate] = []
    covered: set[str] = set()
    selected_files: set[str] = set()
    while remaining:
        best_index = 0
        best_value = float("-inf")
        for index, candidate in enumerate(remaining):
            reason_tokens = {
                token.lower() for token in re.findall(r"[A-Za-z_][\w-]{2,}", candidate.reason)
            }
            new_terms = len((reason_tokens & tokens) - covered) / max(1, len(tokens))
            redundancy = 0.4 if candidate.file.path in selected_files else 0.0
            value = candidate.score + 0.5 * new_terms - redundancy
            tie = (candidate.file.path, candidate.start_line, candidate.end_line)
            if value > best_value or (value == best_value and tie < (
                remaining[best_index].file.path,
                remaining[best_index].start_line,
                remaining[best_index].end_line,
            )):
                best_value = value
                best_index = index
        chosen = remaining.pop(best_index)
        ordered.append(chosen)
        covered |= {
            token.lower() for token in re.findall(r"[A-Za-z_][\w-]{2,}", chosen.reason)
        } & tokens
        selected_files.add(chosen.file.path)
    return ordered


def _cluster_lines(lines: list[int], *, gap: int) -> list[list[int]]:
    if not lines:
        return []
    ordered = sorted(set(lines))
    clusters: list[list[int]] = [[ordered[0]]]
    for line in ordered[1:]:
        if line - clusters[-1][-1] <= gap:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    return clusters


def _merge_candidates(candidates: list[_Candidate], *, gap: int) -> list[_Candidate]:
    by_file: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        by_file.setdefault(candidate.file.id, []).append(candidate)
    merged: list[_Candidate] = []
    for file_candidates in by_file.values():
        file_candidates.sort(key=lambda candidate: candidate.start_line)
        current = file_candidates[0]
        for candidate in file_candidates[1:]:
            if candidate.start_line <= current.end_line + gap:
                current = _Candidate(
                    file=current.file,
                    start_line=current.start_line,
                    end_line=max(current.end_line, candidate.end_line),
                    reason=current.reason,
                    kind=current.kind,
                    score=max(current.score, candidate.score),
                    hit_count=current.hit_count + candidate.hit_count,
                )
            else:
                merged.append(current)
                current = candidate
        merged.append(current)
    return merged


def _overlaps(
    seen: dict[str, list[tuple[int, int]]], file_id: str, start: int, end: int
) -> bool:
    for seen_start, seen_end in seen.get(file_id, []):
        if start <= seen_end and seen_start <= end:
            return True
    return False


def _render_span(span: list[tuple[int, str]]) -> str:
    return "\n".join(f"{line_no}: {text}" for line_no, text in span)


def _shrink(
    span: list[tuple[int, str]], remaining_tokens: int, chars_per_token: float
) -> tuple[list[tuple[int, str]] | None, str, int, bool]:
    """Trim a span from the end until it fits the remaining budget."""
    if remaining_tokens <= 0:
        return None, "", 0, False
    kept: list[tuple[int, str]] = []
    tokens = 0
    for line_no, text in span:
        candidate_text = _render_span([*kept, (line_no, text)])
        candidate_tokens = estimate_tokens(candidate_text, chars_per_token)
        if candidate_tokens > remaining_tokens:
            break
        kept.append((line_no, text))
        tokens = candidate_tokens
    if len(kept) < MIN_SHRUNK_LINES:
        return None, "", 0, False
    return kept, _render_span(kept), tokens, True


__all__ = ["ContextPacker", "estimate_tokens"]

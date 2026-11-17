"""Candidate discovery and span analysis for claim mapping.

Discovery searches the pinned snapshot for three kinds of candidate:

* declared target symbols, resolved through the symbol index;
* identifiers mentioned in the claim text;
* exact occurrences of the claim's code signals, expanded to the enclosing
  definition or a small window.

Every candidate is analysed the same way: which signals occur, whether they occur
in executable code or only in prose, which structural constructs are present, and
how strong the span is as evidence. A symbol whose body contains none of the
claim's signals stays a *weak* candidate — this is what keeps name matching from
ever becoming ``PRESENT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import EvidenceStrength, MappingEvidenceRelation, ParseCoverage, SymbolKind
from mayaworkbench.domain.models import IndexedFile, ResearchClaim, Symbol
from mayaworkbench.mapping.signals import (
    classify_constructs,
    derive_identifiers,
    find_signal_hits,
    looks_like_identifier,
    matched_signals,
    requirement_terms,
    split_identifier_tokens,
)
from mayaworkbench.retrieval.search import SearchService
from mayaworkbench.storage.stores import FileStore, SymbolStore

DEFINITIONAL_KINDS = {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}
MAX_SEEDS = 10
MAX_PROBES = 12


@dataclass
class CandidateSpan:
    path: str
    file_id: str
    start_line: int
    end_line: int
    text: str
    symbol_id: str | None = None
    qualified_name: str | None = None
    symbol_kind: str | None = None
    origin: str = "identifier"
    coverage: ParseCoverage = ParseCoverage.FULL
    is_test: bool = False
    windowed: bool = False


@dataclass
class SpanAnalysis:
    candidate: CandidateSpan
    matched_signals: list[str] = field(default_factory=list)
    code_signals: list[str] = field(default_factory=list)
    prose_signals: list[str] = field(default_factory=list)
    forbidden_signals: list[str] = field(default_factory=list)
    term_hits: list[str] = field(default_factory=list)
    constructs: set[str] = field(default_factory=set)
    strength: EvidenceStrength = EvidenceStrength.WEAK
    definitional: bool = False
    relation: MappingEvidenceRelation = MappingEvidenceRelation.SUPPORTS
    explanation: str = ""

    @property
    def location(self) -> str:
        return f"{self.candidate.path}:{self.candidate.start_line}-{self.candidate.end_line}"


@dataclass
class CollectionResult:
    analyses: list[SpanAnalysis] = field(default_factory=list)
    coverage_limited: bool = False
    truncated: bool = False
    searched_terms: list[str] = field(default_factory=list)
    known_candidate_files: int = 0


class EvidenceCollector:
    def __init__(
        self,
        *,
        files: FileStore,
        symbols: SymbolStore,
        search: SearchService,
        settings: Settings,
    ):
        self.files = files
        self.symbols = symbols
        self.search = search
        self.settings = settings

    # ------------------------------------------------------------------ collect
    def collect(
        self,
        snapshot_id: str,
        claim: ResearchClaim,
        *,
        declared_signals: list[str],
        derived_signals: list[str],
    ) -> CollectionResult:
        max_lines = self.settings.research.max_evidence_span_lines
        max_candidates = max(8, self.settings.research.max_evidence_spans * 4)
        analyses: list[SpanAnalysis] = []
        seen: set[tuple[str, int, int]] = set()
        file_cache: dict[str, IndexedFile] = {}
        symbol_cache: dict[str, list[Symbol]] = {}
        truncated = False
        coverage_limited = False
        searched: list[str] = []

        def file_for(file_id: str) -> IndexedFile | None:
            if file_id not in file_cache:
                try:
                    file_cache[file_id] = self.files.get(file_id)
                except Exception:  # noqa: BLE001 - a missing file is a coverage gap
                    return None
            file = file_cache[file_id]
            if file.is_binary or file.status.value == "ERROR":
                return None
            return file

        def symbols_for(file_id: str) -> list[Symbol]:
            if file_id not in symbol_cache:
                symbol_cache[file_id] = self.symbols.list_by_file(file_id)
            return symbol_cache[file_id]

        def enclosing_symbol(file_id: str, line: int) -> Symbol | None:
            matches = [
                symbol
                for symbol in symbols_for(file_id)
                if symbol.start_line <= line <= symbol.end_line
            ]
            if not matches:
                return None
            return min(matches, key=lambda symbol: symbol.end_line - symbol.start_line)

        def add(
            file: IndexedFile,
            start: int,
            end: int,
            *,
            symbol: Symbol | None = None,
            origin: str,
        ) -> None:
            nonlocal truncated
            start = max(1, min(start, max(1, file.line_count)))
            end = max(start, min(end, max(1, file.line_count)))
            key = (file.path, start, end)
            if key in seen:
                return
            if len(seen) >= max_candidates:
                truncated = True
                return
            seen.add(key)
            span = self.files.read_span(file.id, start, end)
            if not span:
                return
            text = "\n".join(line for _number, line in span)
            windowed = False
            if len(span) > max_lines:
                narrowed = self._narrow(
                    span, declared_signals + derived_signals, max_lines=max_lines
                )
                if narrowed is not None:
                    start, end, text = narrowed
                    windowed = True
            analyses.append(
                SpanAnalysis(
                    candidate=CandidateSpan(
                        path=file.path,
                        file_id=file.id,
                        start_line=start,
                        end_line=end,
                        text=text,
                        symbol_id=symbol.id if symbol else None,
                        qualified_name=symbol.qualified_name if symbol else None,
                        symbol_kind=symbol.kind.value if symbol else None,
                        origin=origin,
                        coverage=file.parse_coverage,
                        is_test=file.is_test,
                        windowed=windowed,
                    )
                )
            )

        def add_symbol(symbol: Symbol, origin: str) -> None:
            file = file_for(symbol.file_id)
            if file is None:
                return
            add(file, symbol.start_line, symbol.end_line, symbol=symbol, origin=origin)

        def add_hit(path: str, file_id: str, line: int, origin: str) -> None:
            file = file_for(file_id)
            if file is None:
                return
            symbol = enclosing_symbol(file_id, line)
            if symbol is not None:
                add_symbol(symbol, origin)
                return
            add(file, line - 3, line + 4, origin=origin)

        # 1. Explicit target symbols.
        for target in claim.target_symbols:
            seed = target.strip()
            if not seed:
                continue
            for hit in self.search.search_symbols(snapshot_id, seed, limit=4):
                add_symbol(self.symbols.get(hit.symbol_id), "target_symbol")
            searched.append(seed)

        # 2. Identifiers mentioned in the claim text.
        identifiers = derive_identifiers(
            " ".join(
                [
                    claim.title,
                    claim.statement,
                    claim.implementation_requirement,
                ]
            )
        )
        for identifier in identifiers[:MAX_SEEDS]:
            found = False
            for hit in self.search.search_symbols(snapshot_id, identifier, limit=3):
                add_symbol(self.symbols.get(hit.symbol_id), "identifier")
                found = True
            searched.append(identifier)
            if found:
                continue
            hits, _expression = self.search.search_text(
                snapshot_id, identifier, mode="exact", limit=3
            )
            for hit in hits:
                add_hit(hit.path, hit.file_id, hit.line_no, "identifier")

        # 3. Exact probes for declared signals: this is what makes "not found"
        #    a statement about the whole snapshot rather than about one symbol.
        probe_signals = list(dict.fromkeys([*declared_signals, *derived_signals]))[:MAX_PROBES]
        for signal in probe_signals:
            hits, _expression = self.search.search_text(
                snapshot_id, signal, mode="exact", limit=4, case_sensitive=False
            )
            for hit in hits:
                add_hit(hit.path, hit.file_id, hit.line_no, "signal")

        # 4. Target paths are inspected even when no identifier search found them.
        for path in claim.target_paths:
            file = self.files.get_by_path(snapshot_id, path)
            if file is None or file.is_binary:
                continue
            add(file, 1, min(file.line_count, max_lines), origin="target_path")

        if not analyses:
            # Last resort: requirement terms, so a claim without identifiers still
            # gets a real search rather than an automatic "absent".
            terms = requirement_terms(
                f"{claim.statement} {claim.implementation_requirement}", limit=6
            )
            for term in terms[:3]:
                hits, _expression = self.search.search_text(
                    snapshot_id, term, mode="lexical", limit=4
                )
                searched.append(term)
                for hit in hits:
                    add_hit(hit.path, hit.file_id, hit.line_no, "term")

        claim_terms = requirement_terms(
            f"{claim.statement} {claim.implementation_requirement}")
        analysed = [
            self._analyse(
                analysis,
                claim=claim,
                declared=declared_signals,
                derived=derived_signals,
                terms=claim_terms,
            )
            for analysis in analyses
        ]
        for analysis in analysed:
            if analysis.candidate.coverage is not ParseCoverage.FULL:
                coverage_limited = True
        analysed.sort(
            key=lambda item: (
                _strength_rank(item.strength),
                -len(item.matched_signals),
                item.candidate.path,
                item.candidate.start_line,
            )
        )
        return CollectionResult(
            analyses=analysed,
            coverage_limited=coverage_limited,
            truncated=truncated,
            searched_terms=searched,
            known_candidate_files=len(file_cache),
        )

    # ------------------------------------------------------------------ analysis
    def _analyse(
        self,
        analysis: SpanAnalysis,
        *,
        claim: ResearchClaim,
        declared: list[str],
        derived: list[str],
        terms: list[str],
    ) -> SpanAnalysis:
        candidate = analysis.candidate
        combined = [*declared, *derived]
        hits = find_signal_hits(candidate.text, combined)
        forbidden = find_signal_hits(candidate.text, claim.forbidden_code_signals)
        analysis.matched_signals = list(dict.fromkeys(hit.signal for hit in hits))
        analysis.code_signals = list(
            dict.fromkeys(hit.signal for hit in hits if hit.is_code)
        )
        analysis.prose_signals = [
            signal for signal in analysis.matched_signals if signal not in analysis.code_signals
        ]
        analysis.forbidden_signals = list(
            dict.fromkeys(hit.signal for hit in forbidden)
        )
        analysis.constructs = classify_constructs(
            candidate.text, path=candidate.path, symbol_kind=candidate.symbol_kind
        )
        token_set = {
            token
            for token in split_identifier_tokens(candidate.text)
            if token
        }
        lowered = {token.casefold() for token in token_set}
        analysis.term_hits = [term for term in terms if term in lowered][:12]
        analysis.definitional = (
            candidate.symbol_id is not None
            and candidate.symbol_kind in {kind.value for kind in DEFINITIONAL_KINDS}
        )
        if analysis.forbidden_signals:
            analysis.relation = MappingEvidenceRelation.CONTRADICTS
        elif "test_file" in analysis.constructs or "test_function" in analysis.constructs:
            analysis.relation = MappingEvidenceRelation.TEST_COVERS
        elif analysis.definitional:
            analysis.relation = MappingEvidenceRelation.IMPLEMENTS
        else:
            analysis.relation = MappingEvidenceRelation.SUPPORTS

        if analysis.forbidden_signals and (
            analysis.definitional or candidate.coverage is ParseCoverage.FULL
        ):
            analysis.strength = EvidenceStrength.STRONG
        elif analysis.code_signals and analysis.definitional and candidate.coverage is ParseCoverage.FULL:
            analysis.strength = EvidenceStrength.STRONG
        elif analysis.code_signals and (
            analysis.definitional or candidate.coverage is ParseCoverage.FULL
        ):
            analysis.strength = EvidenceStrength.MODERATE
        else:
            analysis.strength = EvidenceStrength.WEAK

        analysis.explanation = self._explain(analysis, declared=declared)
        return analysis

    def _explain(self, analysis: SpanAnalysis, *, declared: list[str]) -> str:
        candidate = analysis.candidate
        parts: list[str] = []
        if analysis.definitional:
            parts.append(
                f"Definition of `{candidate.qualified_name}` "
                f"({candidate.symbol_kind}) at {candidate.path}:{candidate.start_line}-"
                f"{candidate.end_line}."
            )
        else:
            parts.append(
                f"Code window at {candidate.path}:{candidate.start_line}-{candidate.end_line}."
            )
        if analysis.code_signals:
            parts.append(
                "Signals present in executable code: "
                + ", ".join(f"`{signal}`" for signal in analysis.code_signals)
                + "."
            )
        if analysis.prose_signals:
            parts.append(
                "Signals present only in comments/docstrings/strings: "
                + ", ".join(f"`{signal}`" for signal in analysis.prose_signals)
                + " (not executable evidence)."
            )
        if not analysis.matched_signals:
            parts.append("No claim signal appears in this span's body.")
        if analysis.term_hits:
            parts.append("Requirement terms present: " + ", ".join(analysis.term_hits) + ".")
        if analysis.forbidden_signals:
            parts.append(
                "Forbidden signals present: "
                + ", ".join(f"`{signal}`" for signal in analysis.forbidden_signals)
                + "."
            )
        if candidate.coverage is not ParseCoverage.FULL:
            parts.append(
                f"Parser coverage for this file is {candidate.coverage.value}; the span was read "
                "lexically and may not reflect executable structure."
            )
        if candidate.windowed:
            parts.append(
                f"The span was windowed to at most "
                f"{self.settings.research.max_evidence_span_lines} lines around the first match."
            )
        if declared and not any(signal in analysis.matched_signals for signal in declared):
            parts.append("Declared acceptance signals are absent from this span.")
        return " ".join(parts)

    def _narrow(
        self, span: list[tuple[int, str]], signals: list[str], *, max_lines: int
    ) -> tuple[int, int, str] | None:
        if not span or len(span) <= max_lines:
            return None
        text_lines = [text for _number, text in span]
        hits = find_signal_hits("\n".join(text_lines), signals)
        start_line = span[0][0]
        end_line = span[-1][0]
        if hits:
            anchor = span[0][0] + hits[0].line_no - 1
        else:
            anchor = start_line
        start = max(start_line, anchor - max_lines // 3)
        end = min(end_line, start + max_lines - 1)
        selected = [
            (number, text) for number, text in span if start <= number <= end
        ]
        if not selected:
            return None
        return (
            selected[0][0],
            selected[-1][0],
            "\n".join(text for _number, text in selected),
        )


def _strength_rank(strength: EvidenceStrength) -> int:
    return {
        EvidenceStrength.STRONG: 0,
        EvidenceStrength.MODERATE: 1,
        EvidenceStrength.WEAK: 2,
    }[strength]


def evidence_excerpt(text: str) -> str:
    """Excerpt stored for evidence: the span text, unchanged."""
    return text


__all__ = [
    "CandidateSpan",
    "SpanAnalysis",
    "CollectionResult",
    "EvidenceCollector",
    "evidence_excerpt",
]

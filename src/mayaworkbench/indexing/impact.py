"""Change-impact analysis over the pinned symbol graph.

An impact answer distinguishes *resolved* edges (AST/LSP/build metadata) from
*heuristic* edges (lexical guesses). A lexical co-occurrence is a hint, never a
proof that a test or API depends on a changed symbol. Every answer carries its
limitations explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from mayaworkbench.domain.enums import EdgeConfidence

_CONFIG_RE = re.compile(
    r"(?:os\.environ\[['\"]([^'\"]+)['\"]\]"
    r"|os\.getenv\(['\"]([^'\"]+)['\"]"
    r"|settings\.([A-Za-z_][A-Za-z0-9_]*)"
    r"|config\[['\"]([^'\"]+)['\"]\])"
)
_TEST_PREFIXES = ("test_", "tests/", "test/", "spec/", "_test.")


@dataclass(frozen=True)
class ImpactEntry:
    path: str
    symbol: str
    relation: str
    confidence: str
    provenance: str
    line: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "symbol": self.symbol, "relation": self.relation,
            "confidence": self.confidence, "provenance": self.provenance, "line": self.line,
        }


@dataclass
class ChangeImpact:
    path: str
    affected_symbols: list[ImpactEntry] = field(default_factory=list)
    affected_tests: list[ImpactEntry] = field(default_factory=list)
    config_references: list[ImpactEntry] = field(default_factory=list)
    resolved_edges: int = 0
    heuristic_edges: int = 0
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "affected_symbols": [item.as_dict() for item in self.affected_symbols],
            "affected_tests": [item.as_dict() for item in self.affected_tests],
            "config_references": [item.as_dict() for item in self.config_references],
            "resolved_edges": self.resolved_edges,
            "heuristic_edges": self.heuristic_edges,
            "limitations": self.limitations,
        }


def is_test_path(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.startswith(prefix) or f"/{prefix}" in lowered for prefix in _TEST_PREFIXES) or lowered.endswith("_test.py")


def config_references(text: str) -> list[str]:
    """Extract config/env keys referenced in a source file (sorted, unique)."""
    found: set[str] = set()
    for match in _CONFIG_RE.finditer(text or ""):
        for group in match.groups():
            if group:
                found.add(group)
    return sorted(found)


def build_impact_map(
    *,
    files: Iterable[Any],
    symbols: Iterable[Any],
    edges: Iterable[Any],
) -> dict[str, ChangeImpact]:
    """Build a reverse map from source path to everything that may depend on it."""
    file_rows = list(files)
    symbol_rows = list(symbols)
    edge_rows = list(edges)
    file_by_id = {item.id: item for item in file_rows}
    symbol_by_id = {item.id: item for item in symbol_rows}
    by_path: dict[str, ChangeImpact] = {
        item.path: ChangeImpact(path=item.path) for item in file_rows
    }

    for edge in edge_rows:
        target_file_id = getattr(edge, "target_file_id", None)
        target_symbol_id = getattr(edge, "target_symbol_id", None)
        target = (
            file_by_id.get(target_file_id) if target_file_id else None
        ) or (
            symbol_by_id.get(target_symbol_id) if target_symbol_id else None
        )
        if target is None:
            continue
        path = getattr(target, "path", None)
        impact = by_path.get(path)
        if impact is None:
            continue
        source_symbol = symbol_by_id.get(getattr(edge, "source_symbol_id", "") or "")
        source_file = file_by_id.get(getattr(edge, "source_file_id", "") or "")
        source_path = getattr(source_symbol, "path", None) or getattr(source_file, "path", "?")
        confidence = getattr(edge.confidence, "value", str(edge.confidence))
        resolved = confidence in {EdgeConfidence.HIGH.value, EdgeConfidence.MEDIUM.value} and getattr(
            edge.provenance, "value", str(edge.provenance)
        ) not in {"heuristic", "lexical"}
        entry = ImpactEntry(
            path=source_path,
            symbol=getattr(edge, "target_name", "") or getattr(source_symbol, "name", ""),
            relation=getattr(edge.edge_type, "value", str(edge.edge_type)),
            confidence=confidence,
            provenance=getattr(edge, "provenance", ""),
            line=int(getattr(edge, "line", 0) or 0),
        )
        if is_test_path(source_path):
            impact.affected_tests.append(entry)
        else:
            impact.affected_symbols.append(entry)
        if resolved:
            impact.resolved_edges += 1
        else:
            impact.heuristic_edges += 1

    for item in file_rows:
        impact = by_path[item.path]
        text = ""
        artifact_id = getattr(item, "content_artifact_id", None)
        if artifact_id:  # filled by callers that can read artifacts; best effort
            try:
                from mayaworkbench.storage.artifacts import ArtifactStore

                store = getattr(item, "_artifact_store", None)
                if store is not None:
                    text = store.read_text(artifact_id)
            except Exception:  # pragma: no cover - optional enrichment
                text = ""
        for key in config_references(text):
            impact.config_references.append(
                ImpactEntry(item.path, key, "CONFIG_REFERENCE", "LOW", "lexical")
            )
        impact.limitations.extend(
            [
                "Lexical/LOW-confidence edges are hints, not proven dependencies.",
                "Dynamic dispatch, reflection and generated code may be missing.",
                "Config references were detected from text when content was available.",
            ]
        )
    return by_path


def analyze_change(
    changed_paths: Iterable[str],
    *,
    files: Iterable[Any],
    symbols: Iterable[Any],
    edges: Iterable[Any],
) -> list[dict[str, Any]]:
    """Analyse only the changed paths; unresolved impact is reported honestly."""
    impact = build_impact_map(files=files, symbols=symbols, edges=edges)
    out: list[dict[str, Any]] = []
    for path in changed_paths:
        entry = impact.get(path)
        if entry is None:
            out.append({
                "path": path, "affected_symbols": [], "affected_tests": [],
                "config_references": [], "resolved_edges": 0, "heuristic_edges": 0,
                "limitations": ["the changed path is not in the pinned index"],
            })
        else:
            out.append(entry.as_dict())
    return out


__all__ = [
    "ImpactEntry", "ChangeImpact", "build_impact_map", "analyze_change",
    "config_references", "is_test_path",
]

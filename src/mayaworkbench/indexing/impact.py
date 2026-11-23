"""Change-impact analysis for an indexed snapshot.

Given a set of changed (or to-be-changed) file paths, this module computes the
files, tests and symbols that may be affected by the change, using the
already-resolved import / call / inheritance edges in the index.

Honest scope:

* Python-first. The index's resolved edges exist only where the parser produced
  them; other languages are represented by whatever edges their adapter emitted
  (often none beyond imports). Results are only as good as the index.
* Static. Dynamic dispatch, reflection, string-based dispatch, runtime
  dependency injection and monkey-patching are not resolved, so an affected set
  is a lower bound, never a proof of "nothing else breaks".
* Confidence is propagated as the minimum edge confidence along a dependency
  path (HIGH > MEDIUM > LOW). A LOW edge anywhere in the path makes the whole
  affected file LOW.
* Traversal is bounded by ``max_depth`` to keep the result finite and readable
  on cyclic import graphs.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, Field

from mayaworkbench.domain.enums import EdgeConfidence
from mayaworkbench.domain.models import IndexedFile, Symbol
from mayaworkbench.storage.stores import FileStore, SymbolStore


_CONF_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}


def _min_conf(a: str, b: str) -> str:
    return a if _CONF_RANK.get(a, 0) <= _CONF_RANK.get(b, 0) else b


_EDGE_REASON = {
    "IMPORTS": "imports",
    "CALLS": "calls",
    "INHERITS": "inherits",
    "REFERENCES": "references",
    "DECORATES": "decorates",
}


class AffectedFile(BaseModel):
    path: str
    depth: int
    reason: str
    confidence: str
    via: list[str] = Field(default_factory=list)


class ImpactReport(BaseModel):
    snapshot_id: str
    changed: list[str]
    max_depth: int
    affected_files: list[AffectedFile] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    affected_symbols: list[str] = Field(default_factory=list)
    total_files_indexed: int = 0
    notes: list[str] = Field(default_factory=list)


class ImpactAnalyzer:
    """Compute the change-impact closure for a snapshot."""

    def __init__(self, *, files: FileStore, symbols: SymbolStore) -> None:
        self.files = files
        self.symbols = symbols

    def analyze(
        self,
        snapshot_id: str,
        changed_paths: list[str],
        *,
        max_depth: int = 6,
    ) -> ImpactReport:
        all_files = self.files.iter_files(snapshot_id)
        path_to_file: dict[str, IndexedFile] = {f.path: f for f in all_files}
        file_id_to_path: dict[str, str] = {f.id: f.path for f in all_files}
        is_test: dict[str, bool] = {f.path: bool(f.is_test) for f in all_files}

        all_symbols: list[Symbol] = self.symbols.list_by_snapshot(snapshot_id)
        symbol_to_file: dict[str, str] = {s.id: s.file_id for s in all_symbols}
        file_to_symbol_ids: dict[str, list[str]] = defaultdict(list)
        for s in all_symbols:
            file_to_symbol_ids[s.file_id].append(s.id)
        symbol_qualified_by_id: dict[str, str] = {s.id: s.qualified_name for s in all_symbols}

        edges = self.symbols.list_edges(snapshot_id)

        # Reverse dependency index: target (file or symbol) -> set of
        # (dependent file id, edge type, confidence).
        file_dependents: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        symbol_dependents: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        for edge in edges:
            source_file = edge.get("source_file_id")
            target_file = edge.get("target_file_id")
            target_symbol = edge.get("target_symbol_id")
            edge_type = str(edge.get("edge_type") or "")
            confidence = str(edge.get("confidence") or EdgeConfidence.MEDIUM.value)
            if not source_file:
                continue
            if target_file and target_file != source_file:
                file_dependents[target_file].add((source_file, edge_type, confidence))
            if target_symbol:
                symbol_dependents[target_symbol].add((source_file, edge_type, confidence))

        def dependents_of_file(file_id: str) -> set[tuple[str, str, str]]:
            out: set[tuple[str, str, str]] = set(file_dependents.get(file_id, ()))
            for symbol_id in file_to_symbol_ids.get(file_id, []):
                out |= symbol_dependents.get(symbol_id, set())
            return out

        # Resolve changed paths to file ids.
        changed_file_ids: list[str] = []
        unmatched: list[str] = []
        for path in changed_paths:
            f = path_to_file.get(path)
            if f is not None:
                changed_file_ids.append(f.id)
            else:
                unmatched.append(path)

        # BFS over the reverse-dependency graph.
        result: dict[str, tuple[int, str, str, list[str]]] = {}
        queue: deque[str] = deque()
        for fid in changed_file_ids:
            path = file_id_to_path[fid]
            result[fid] = (0, "changed", "HIGH", [path])
            queue.append(fid)

        while queue:
            current = queue.popleft()
            depth, _reason, confidence, via = result[current]
            if depth >= max_depth:
                continue
            for source_file, edge_type, edge_conf in dependents_of_file(current):
                if source_file == current:
                    continue
                if source_file not in file_id_to_path:
                    continue
                new_depth = depth + 1
                new_conf = _min_conf(confidence, edge_conf)
                reason = _EDGE_REASON.get(edge_type, edge_type.lower() or "depends")
                new_via = via + [file_id_to_path[source_file]]
                if source_file in result:
                    if new_depth < result[source_file][0]:
                        result[source_file] = (new_depth, reason, new_conf, new_via)
                        queue.append(source_file)
                else:
                    result[source_file] = (new_depth, reason, new_conf, new_via)
                    queue.append(source_file)

        affected_files: list[AffectedFile] = []
        for file_id, (depth, reason, confidence, via) in result.items():
            affected_files.append(
                AffectedFile(
                    path=file_id_to_path[file_id],
                    depth=depth,
                    reason="changed" if depth == 0 else reason,
                    confidence=confidence,
                    via=via,
                )
            )
        affected_files.sort(key=lambda item: (item.depth, item.path))

        affected_tests = sorted(
            {item.path for item in affected_files if is_test.get(item.path, False)}
        )

        # Affected symbols: symbols living in affected files (bounded list).
        affected_file_ids = {fid for fid in result}
        affected_symbols: set[str] = set()
        for fid in affected_file_ids:
            for sid in file_to_symbol_ids.get(fid, []):
                qn = symbol_qualified_by_id.get(sid)
                if qn:
                    affected_symbols.add(qn)
        affected_symbols_sorted = sorted(affected_symbols)[:500]

        notes: list[str] = [
            "Python-first static analysis: impact is derived from resolved "
            "import/call/inheritance edges only.",
            "Dynamic dispatch, reflection, string-based dispatch and runtime "
            "dependency injection are not resolved; the affected set is a lower "
            "bound, not a proof.",
            "Confidence is the minimum edge confidence along the dependency path.",
            f"Traversal bounded at depth {max_depth}.",
        ]
        if not changed_file_ids:
            notes.append(
                "No changed path matched an indexed file; the result is empty. "
                "Paths must be snapshot-relative."
            )
        if unmatched:
            notes.append(
                "Changed paths not found in the index: " + ", ".join(sorted(unmatched))
            )

        return ImpactReport(
            snapshot_id=snapshot_id,
            changed=sorted(changed_paths),
            max_depth=max_depth,
            affected_files=affected_files,
            affected_tests=affected_tests,
            affected_symbols=affected_symbols_sorted,
            total_files_indexed=len(all_files),
            notes=notes,
        )

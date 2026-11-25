"""Automatic failure localisation for declared in-loop checks (WP9).

A failing check's output is unstructured text.  This module turns that text into
a small, deterministic set of *locations* (file + line, optionally a symbol and
the failing test that pointed at it) so a repair step can start from evidence
instead of guessing.  It is deliberately conservative: every location carries a
confidence level and a source, and ambiguous or unparseable output yields an
empty list rather than a fabricated span.

The parser understands the output shapes produced by the checkers in
``tasks/checkers.py`` (pytest, cargo, go test, tsc, eslint, ruff, mypy,
flake8/pylint, gradle/maven, make, generic ``file:line``) plus Python
tracebacks.  It performs no I/O and never reads the repository; symbol mapping
is optional and happens only when a caller supplies a symbol store and snapshot
id.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Protocol

_MAX_LOCATIONS = 64
_MAX_MESSAGE = 240


@dataclass(frozen=True)
class LocalizedFailure:
    """One concrete place a failing check pointed at."""

    path: str
    line: int | None = None
    column: int | None = None
    function: str | None = None
    symbol: str | None = None
    test: str | None = None
    message: str = ""
    confidence: str = "medium"
    source: str = "generic"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SymbolStoreLike(Protocol):
    def list_by_snapshot(self, snapshot_id: str) -> list[Any]: ...


# Python tracebacks: ``  File "path", line 12, in name``.
_PY_TRACEBACK_RE = re.compile(
    r'File\s+"([^"]+)"\s*,\s*line\s+(\d+)(?:\s*,\s*in\s+([^\n]+))?'
)
# pytest failure ids and assertion locations: ``tests/test_x.py::test_y`` and
# ``tests/test_x.py:12: AssertionError``.
_PYTEST_ID_RE = re.compile(r"(?m)^\s*(?:FAILED|ERROR)\s+([^\s:]+)::(\S+)")
_PYTEST_LOC_RE = re.compile(r"(?m)^\s*([^\s:]+):(\d+)(?::(\d+))?\s*:\s*(.+?)\s*$")
# cargo: ``--> src/lib.rs:12:5``.
_CARGO_LOC_RE = re.compile(r"-->\s*([^\s:]+):(\d+)(?::(\d+))?")
# tsc: ``src/x.ts(12,5): error TS...``.
_TSC_LOC_RE = re.compile(r"([^\s(]+)\((\d+),(\d+)\)\s*:")
# eslint / ruff / generic ``path:line:col:``.
_COLON_LOC_RE = re.compile(r"(?m)^\s*([^\s:]+):(\d+):(\d+)(?::\s*(.*))?$")
# go: ``./main.go:12:5: ...`` (covered by _COLON_LOC_RE) and ``# path:line``.
_GO_LOC_RE = re.compile(r"(?m)^\s*([^\s:]+):(\d+):\s*(.+)$")
# gradle/maven style ``path:line:`` without a column.
_PLAIN_LOC_RE = re.compile(r"(?m)^\s*([^\s:]+):(\d+)\s*$")


def _normalise_path(raw: str) -> str:
    value = raw.strip().strip("\"'`")
    if value.startswith("./"):
        value = value[2:]
    # Keep the path as recorded by the checker; only drop a leading workspace
    # prefix when it is unambiguous so repository-relative paths survive.
    for prefix in ("/workspace/", "/src/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.replace("\\", "/")


def _add_location(
    found: list[LocalizedFailure],
    seen: set[tuple[str, int | None, int | None, str]],
    *,
    path: str,
    line: int | None = None,
    column: int | None = None,
    function: str | None = None,
    test: str | None = None,
    message: str = "",
    confidence: str = "medium",
    source: str = "generic",
) -> None:
    if len(found) >= _MAX_LOCATIONS:
        return
    normalised_path = _normalise_path(path)
    if not normalised_path or normalised_path.startswith("-"):
        return
    # Reject obvious non-paths (URLs, bare words without a separator or extension).
    if "/" not in normalised_path and "." not in PurePosixPath(normalised_path).name:
        return
    key = (normalised_path, line, column, message[:80])
    if key in seen:
        return
    seen.add(key)
    found.append(
        LocalizedFailure(
            path=normalised_path,
            line=line,
            column=column,
            function=(function.strip() if function else None),
            test=test,
            message=(message or "").strip()[:_MAX_MESSAGE],
            confidence=confidence,
            source=source,
        )
    )


def _map_symbols(
    locations: list[LocalizedFailure],
    *,
    snapshot_id: str | None,
    symbols: SymbolStoreLike | None,
) -> list[LocalizedFailure]:
    if symbols is None or not snapshot_id:
        return locations
    try:
        candidates = list(symbols.list_by_snapshot(snapshot_id))
    except Exception:  # noqa: BLE001 - symbol mapping is an aid, never a gate
        return locations
    by_path: dict[str, list[Any]] = {}
    for symbol in candidates:
        by_path.setdefault(getattr(symbol, "path", ""), []).append(symbol)

    mapped: list[LocalizedFailure] = []
    for location in locations:
        symbol_name = location.symbol
        if location.line is not None:
            choices = by_path.get(location.path, [])
            # Fall back to a basename match when the checker recorded a relative
            # path that differs only by a leading directory.
            if not choices:
                basename = PurePosixPath(location.path).name
                choices = [
                    item
                    for item in candidates
                    if PurePosixPath(getattr(item, "path", "")).name == basename
                ]
            best = None
            best_span = None
            for symbol in choices:
                start = getattr(symbol, "start_line", None)
                end = getattr(symbol, "end_line", None)
                if start is None or end is None or not (start <= location.line <= end):
                    continue
                span = end - start
                if best_span is None or span < best_span:
                    best = symbol
                    best_span = span
            if best is not None:
                symbol_name = getattr(best, "qualified_name", None) or getattr(best, "name", None)
        mapped.append(
            LocalizedFailure(
                path=location.path,
                line=location.line,
                column=location.column,
                function=location.function,
                symbol=symbol_name,
                test=location.test,
                message=location.message,
                confidence=("high" if symbol_name and location.confidence != "low" else location.confidence),
                source=location.source,
            )
        )
    return mapped


def localize_failures(
    text: str,
    *,
    snapshot_id: str | None = None,
    symbols: SymbolStoreLike | None = None,
    max_locations: int = _MAX_LOCATIONS,
) -> list[LocalizedFailure]:
    """Return concrete locations mentioned by ``text``, most useful first."""
    if not text:
        return []
    found: list[LocalizedFailure] = []
    seen: set[tuple[str, int | None, int | None, str]] = set()
    limit = max(1, min(max_locations, _MAX_LOCATIONS))

    # Python tracebacks are the most precise source.
    for match in _PY_TRACEBACK_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            function=match.group(3),
            confidence="high",
            source="traceback",
        )
        if len(found) >= limit:
            break

    # pytest failing test ids: map the test id to its file path.
    for match in _PYTEST_ID_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            test=match.group(2),
            confidence="medium",
            source="pytest",
        )
        if len(found) >= limit:
            break

    # cargo locations.
    for match in _CARGO_LOC_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            column=int(match.group(3)) if match.group(3) else None,
            confidence="high",
            source="cargo",
        )
        if len(found) >= limit:
            break

    # tsc locations.
    for match in _TSC_LOC_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            column=int(match.group(3)),
            confidence="high",
            source="tsc",
        )
        if len(found) >= limit:
            break

    # eslint/ruff/generic colon locations with a column.
    for match in _COLON_LOC_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            column=int(match.group(3)),
            message=match.group(4) or "",
            confidence="medium",
            source="lint",
        )
        if len(found) >= limit:
            break

    # pytest assertion locations without a column.
    for match in _PYTEST_LOC_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            column=int(match.group(3)) if match.group(3) else None,
            message=match.group(4) or "",
            confidence="medium",
            source="pytest",
        )
        if len(found) >= limit:
            break

    # go-style ``path:line: message`` (no column).
    for match in _GO_LOC_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            message=match.group(3) or "",
            confidence="medium",
            source="go",
        )
        if len(found) >= limit:
            break

    # Plain ``path:line`` as a last resort.
    for match in _PLAIN_LOC_RE.finditer(text):
        _add_location(
            found,
            seen,
            path=match.group(1),
            line=int(match.group(2)),
            confidence="low",
            source="generic",
        )
        if len(found) >= limit:
            break

    found = found[:limit]
    return _map_symbols(found, snapshot_id=snapshot_id, symbols=symbols)


__all__ = ["LocalizedFailure", "localize_failures"]

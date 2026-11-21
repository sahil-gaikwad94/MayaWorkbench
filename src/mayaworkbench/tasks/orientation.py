"""Grounded repository orientation for the implementer's first steps.

The model starts each run with a pinned inventory and a plan, but no idea which
of 400 files matter. Without orientation it spends its first tool calls on
``list_files`` and keyword guesses; with orientation it starts from the spans
retrieval already ranked for the task goal, the symbols defined in the paths it
may edit, and the index issues that make a file unreliable evidence.

Everything here is derived from the pinned snapshot, so it describes the base
revision. The agent still reads the live worktree before editing; the payload
says so explicitly to prevent a stale-orientation edit.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from mayaworkbench.domain.models import IndexedFile
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.retrieval.service import RetrievalService
from mayaworkbench.storage.stores import FileStore, IssueStore, SymbolStore

log = get_logger("tasks.orientation")

MAX_ORIENTATION_CHARS = 5200
MAX_SPANS = 8
MAX_SPAN_TEXT = 900
MAX_SYMBOLS = 30
MAX_FILES_SCANNED = 60
MAX_ISSUES = 8
MAX_TEST_FILES = 12


def _within(path: str, prefixes: Iterable[str]) -> bool:
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
        if any(char in prefix for char in "*?["):
            from fnmatch import fnmatch

            if fnmatch(path, prefix):
                return True
    return False


def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the orientation inside a predictable prompt budget.

    Spans are dropped from the tail first (lower-ranked evidence), then symbols,
    then issues. The payload records what was dropped so the loss is visible.
    """
    dropped: dict[str, int] = {}

    def encoded() -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if encoded() <= MAX_ORIENTATION_CHARS:
        return payload
    for section in ("spans", "symbols", "index_issues", "test_files"):
        items = payload.get(section)
        if not isinstance(items, list):
            continue
        while items and encoded() > MAX_ORIENTATION_CHARS:
            items.pop()
            dropped[section] = dropped.get(section, 0) + 1
        if encoded() <= MAX_ORIENTATION_CHARS:
            break
    if dropped:
        payload["truncated"] = dropped
    return payload


def build_orientation(
    *,
    retrieval: RetrievalService | None,
    files: FileStore,
    symbols: SymbolStore,
    issues: IssueStore,
    snapshot_id: str,
    goal: str,
    allowed_paths: list[str],
    excluded_paths: list[str] | None = None,
    token_budget: int = 1100,
) -> dict[str, Any]:
    """Build a bounded, inspectable orientation payload.

    A missing or stale index is reported, never silently replaced with guesses.
    """
    payload: dict[str, Any] = {
        "kind": "repository_orientation",
        "snapshot_id": snapshot_id,
        "description": (
            "Base-revision context from the pinned snapshot's index. Read the live "
            "worktree before editing: the workspace may already differ from this text."
        ),
        "allowed_paths": list(allowed_paths),
        "excluded_paths": list(excluded_paths or []),
        "spans": [],
        "symbols": [],
        "test_files": [],
        "index_issues": [],
        "retrieval_notes": [],
    }
    if retrieval is not None:
        try:
            bundle = retrieval.retrieve(snapshot_id, goal, budget=token_budget, mode="auto")
        except Exception as exc:  # noqa: BLE001 - orientation is best effort
            payload["retrieval_available"] = False
            payload["retrieval_error"] = f"{type(exc).__name__}: {exc}"[:400]
        else:
            payload["retrieval_available"] = True
            for item in bundle.context.items[:MAX_SPANS]:
                text = item.text if len(item.text) <= MAX_SPAN_TEXT else item.text[:MAX_SPAN_TEXT] + "\n[truncated]"
                payload["spans"].append(
                    {
                        "path": item.path,
                        "start_line": item.start_line,
                        "end_line": item.end_line,
                        "kind": item.kind,
                        "reason": item.reason,
                        "text": text,
                    }
                )
            payload["retrieval_notes"] = bundle.diagnostics.notes[:6]
    else:
        payload["retrieval_available"] = False
        payload["retrieval_error"] = "retrieval is not configured"

    try:
        inventory: list[IndexedFile] = files.list_by_snapshot(
            snapshot_id, limit=2000, include_skipped=False
        )
    except Exception as exc:  # noqa: BLE001
        payload["index_available"] = False
        payload["index_error"] = f"{type(exc).__name__}: {exc}"[:400]
        return _truncate(payload)
    payload["index_available"] = bool(inventory)
    if not inventory:
        payload["index_note"] = (
            "this snapshot has no index; run `maya index` for symbol and test orientation"
        )

    scoped = [item for item in inventory if _within(item.path, payload["allowed_paths"])]
    in_scope = {item.path for item in scoped}
    for item in inventory:
        if item.is_test and item.path not in in_scope and len(payload["test_files"]) < MAX_TEST_FILES:
            payload["test_files"].append({"path": item.path, "lines": item.line_count})
    for item in scoped[:MAX_TEST_FILES]:
        if item.is_test:
            entry = {"path": item.path, "lines": item.line_count, "in_scope": True}
            if entry not in payload["test_files"]:
                payload["test_files"].append(entry)

    seen: set[str] = set()
    for item in scoped[:MAX_FILES_SCANNED]:
        try:
            file_symbols = symbols.list_by_file(item.id)
        except Exception:  # noqa: BLE001 - a missing file row is not fatal
            continue
        for symbol in file_symbols:
            if symbol.parent_symbol_id is not None:
                continue
            if symbol.qualified_name in seen:
                continue
            seen.add(symbol.qualified_name)
            payload["symbols"].append(
                {
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind.value,
                    "path": symbol.path,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "signature": symbol.signature[:200],
                }
            )
            if len(payload["symbols"]) >= MAX_SYMBOLS:
                break
        if len(payload["symbols"]) >= MAX_SYMBOLS:
            break

    try:
        for issue in issues.list(snapshot_id, limit=200):
            if issue.path is not None and not _within(issue.path, payload["allowed_paths"]):
                continue
            payload["index_issues"].append(
                {
                    "kind": issue.kind.value,
                    "severity": issue.severity.value,
                    "path": issue.path,
                    "detail": (issue.detail or "")[:220],
                }
            )
            if len(payload["index_issues"]) >= MAX_ISSUES:
                break
    except Exception:  # noqa: BLE001 - issues are advisory
        pass

    if not payload["spans"]:
        payload["retrieval_note"] = (
            "no relevant span was retrieved for the goal; explore with list_files/search_text"
        )
    return _truncate(payload)


__all__ = ["build_orientation", "MAX_ORIENTATION_CHARS"]

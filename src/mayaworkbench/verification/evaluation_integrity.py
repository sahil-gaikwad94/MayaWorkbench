"""Frozen-hash integrity of evaluation-critical files (WP-R2I-2).

A task contract may pin a list of evaluation-critical paths in the ``details``
of a declared check (``evaluation_paths``) together with explicit, scope-bound
treatments (``approved_evaluation_changes``: repo-relative path -> exact
expected sha256). Those declarations are immutable for the lifetime of the
contract: there is no generic "permission to edit evaluation code".

The base bytes come from the pinned snapshot content artifacts, never from the
live checkout. Candidate bytes come from the verification workspace. Paths are
resolved and must stay inside that workspace before anything is hashed, so a
symlink cannot quietly point the check at an easier yardstick outside the tree.

This module performs byte comparison only. It does not claim that a matching
hash proves semantic equivalence, and it never waives a mismatch on the basis
of a model statement.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.errors import VerificationError

UNCHANGED = "UNCHANGED"
APPROVED_TREATMENT = "APPROVED_TREATMENT"
TAMPERING = "TAMPERING"

_STATUSES = (UNCHANGED, APPROVED_TREATMENT, TAMPERING)


class EvaluationChange(BaseModel):
    """The classified result for one declared evaluation-critical path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    status: Literal["UNCHANGED", "APPROVED_TREATMENT", "TAMPERING"]
    base_sha256: str | None = None
    candidate_sha256: str | None = None
    reason: str = ""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_within(root: Path, declared_path: str) -> Path:
    """Resolve a repo-relative path and refuse anything outside ``root``.

    This runs *before* hashing. Symlinks are followed with ``resolve``, so a
    link pointing outside the candidate tree is rejected rather than measured.
    """
    if not isinstance(declared_path, str) or not declared_path:
        raise VerificationError(
            "A declared evaluation path must be a non-empty repo-relative string.",
            hint="evaluation_paths entries are strings relative to the repository root.",
        )
    resolved_root = root.resolve()
    candidate = (resolved_root / declared_path).resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise VerificationError(
            f"Declared evaluation path {declared_path!r} resolves outside the "
            "candidate workspace; refusing to hash it.",
            hint=(
                "Evaluation-critical paths must stay inside the candidate tree; "
                "symlinks that escape it are never accepted."
            ),
        ) from None
    return candidate


def evaluate_harness_integrity(
    *,
    declared_paths: Iterable[str],
    approved_changes: Mapping[str, str] | None,
    base_reader: Callable[[str], bytes | None],
    candidate_root: Path,
) -> list[EvaluationChange]:
    """Compare declared evaluation-critical paths with their pinned snapshot.

    ``base_reader`` returns the bytes recorded for a path in the pinned
    snapshot, or ``None`` when the snapshot has no such file. ``candidate_root``
    is the workspace that the checks ran against. An approval is honoured only
    when it names the exact declared path and its hash equals the candidate
    content hash byte for byte.
    """
    approvals: Mapping[str, str] = approved_changes or {}
    root = Path(candidate_root)

    unique: list[str] = []
    seen: set[str] = set()
    for item in declared_paths:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    changes: list[EvaluationChange] = []
    for path in unique:
        # Path validation and symlink resolution happen before any hashing.
        resolved = _resolve_within(root, path)

        base_bytes = base_reader(path)
        base_hash = _sha256(base_bytes) if base_bytes is not None else None
        try:
            candidate_bytes = resolved.read_bytes()
        except (OSError, ValueError):
            candidate_bytes = None
        candidate_hash = _sha256(candidate_bytes) if candidate_bytes is not None else None

        approved_hash = approvals.get(path)
        if base_hash == candidate_hash:
            status = UNCHANGED
            reason = (
                "declared evaluation-critical path is absent from both the pinned snapshot "
                "and the candidate"
                if candidate_hash is None
                else "candidate bytes are identical to the pinned snapshot"
            )
        elif (
            approved_hash is not None
            and candidate_hash is not None
            and approved_hash == candidate_hash
        ):
            status = APPROVED_TREATMENT
            reason = (
                "content differs from the pinned snapshot and matches the exact hash "
                "pre-approved for this path"
            )
        else:
            status = TAMPERING
            if candidate_hash is None:
                reason = "a declared evaluation-critical file is missing from the candidate tree"
            elif base_hash is None and approved_hash is None:
                reason = (
                    "a declared evaluation-critical path was newly created without an "
                    "exact-hash approval for this path"
                )
            elif base_hash is None:
                reason = (
                    "a declared evaluation-critical path was newly created and its hash "
                    "does not match the approval declared for this path"
                )
            elif approved_hash is None:
                reason = (
                    "content differs from the pinned snapshot without an exact-hash "
                    "approval bound to this path"
                )
            else:
                reason = (
                    "content differs from the pinned snapshot and does not match the "
                    "approval declared for this path"
                )

        changes.append(
            EvaluationChange(
                path=path,
                status=status,
                base_sha256=base_hash,
                candidate_sha256=candidate_hash,
                reason=reason,
            )
        )
    return changes


def as_check_details(changes: Sequence[EvaluationChange]) -> dict[str, Any]:
    """Structured evidence payload for the ``evaluation.integrity`` check."""
    counts = {status: 0 for status in _STATUSES}
    for change in changes:
        counts[change.status] = counts.get(change.status, 0) + 1
    return {
        "status": "FAILED" if counts[TAMPERING] else "PASSED",
        "counts": counts,
        "changes": [item.model_dump(mode="json") for item in changes],
        "declared_paths": [item.path for item in changes],
        "tampering_paths": [item.path for item in changes if item.status == TAMPERING],
        "frozen_harness_manifest": {
            item.path: item.base_sha256
            for item in changes
            if item.base_sha256 is not None
        },
    }


def render_summary(changes: Sequence[EvaluationChange]) -> str:
    """Human-readable one-paragraph summary of the frozen-harness comparison."""
    details = as_check_details(changes)
    counts = details["counts"]
    summary = (
        f"Evaluation integrity: {len(changes)} declared evaluation-critical path(s); "
        f"{counts[UNCHANGED]} unchanged, {counts[APPROVED_TREATMENT]} approved "
        f"treatment(s), {counts[TAMPERING]} tampering - {details['status']}."
    )
    if details["tampering_paths"]:
        summary += " Tampered paths: " + ", ".join(details["tampering_paths"][:20])
        if len(details["tampering_paths"]) > 20:
            summary += f" (+{len(details['tampering_paths']) - 20} more)"
        summary += "."
    return summary


__all__ = [
    "UNCHANGED",
    "APPROVED_TREATMENT",
    "TAMPERING",
    "EvaluationChange",
    "evaluate_harness_integrity",
    "as_check_details",
    "render_summary",
]

"""Advanced memory governance: conflict detection, retention and bundles.

Memory remains user-governed: audits only *report* conflicts, retention only
retracts with a reason, and imported items are always UNCONFIRMED. Nothing here
turns a memory into authority or evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import MemoryStatus
from mayaworkbench.errors import ProjectMemoryError

_TOKEN_RE = re.compile(r"[a-z_][\w-]{3,}")
_NEGATIONS = {"not", "never", "avoid", "do not", "do not use", "must not"}


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def fingerprint(statement: str) -> str:
    return hashlib.sha256(" ".join(sorted(_tokens(statement))).encode()).hexdigest()


def find_conflicts(service: Any, project_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Find pairs of confirmed statements that overlap heavily but disagree."""
    memories = service.list(project_id, status=MemoryStatus.CONFIRMED.value, limit=limit)
    out: list[dict[str, Any]] = []
    for i, left in enumerate(memories):
        left_tokens = _tokens(left.statement)
        if not left_tokens:
            continue
        left_neg = bool(_NEGATIONS & set(left.statement.lower().split()))
        for right in memories[i + 1 :]:
            right_tokens = _tokens(right.statement)
            overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
            if overlap < 0.55:
                continue
            right_neg = bool(_NEGATIONS & set(right.statement.lower().split()))
            if left_neg == right_neg:
                continue
            out.append(
                {
                    "left_id": left.id,
                    "right_id": right.id,
                    "overlap": round(overlap, 3),
                    "reason": "overlapping statements with opposing polarity",
                    "advisory": "Reported only; no memory was changed.",
                }
            )
    return out


def expired_memory_ids(service: Any, project_id: str, *, max_age_days: int = 365) -> list[str]:
    cutoff = utcnow() - timedelta(days=max(1, max_age_days))
    expired: list[str] = []
    for item in service.list(project_id, limit=1000):
        raw = (item.details or {}).get("expires_at")
        if raw:
            try:
                if datetime.fromisoformat(str(raw)) <= utcnow():
                    expired.append(item.id)
                    continue
            except ValueError:
                pass
        if item.status is not MemoryStatus.CONFIRMED:
            continue
        created = item.created_at
        if created is not None and created <= cutoff:
            expired.append(item.id)
    return expired


def apply_retention(
    service: Any, project_id: str, *, max_age_days: int = 365
) -> list[dict[str, str]]:
    retracted: list[dict[str, str]] = []
    for memory_id in expired_memory_ids(service, project_id, max_age_days=max_age_days):
        item = service.get(memory_id)
        if item.status in {MemoryStatus.RETRACTED, MemoryStatus.STALE}:
            continue
        service.retract(memory_id, reason=f"retention window of {max_age_days} days elapsed")
        retracted.append({"id": memory_id, "reason": "retention window elapsed"})
    return retracted


def export_bundle(service: Any, project_id: str) -> dict[str, Any]:
    items = service.list(project_id, limit=1000)
    return {
        "schema": "maya-memory-bundle/v1",
        "exported_at": utcnow().isoformat(),
        "project_id": project_id,
        "memories": [item.model_dump(mode="json") for item in items],
        "conflicts": find_conflicts(service, project_id),
    }


def import_bundle(
    service: Any, project_id: str, bundle: dict[str, Any], *, allow_conflicts: bool = False
) -> dict[str, Any]:
    if bundle.get("schema") != "maya-memory-bundle/v1":
        raise ProjectMemoryError("Unsupported memory bundle schema")
    existing = {fingerprint(item.statement) for item in service.list(project_id, limit=1000)}
    created: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    for raw in bundle.get("memories", []):
        statement = str(raw.get("statement") or "")
        digest = fingerprint(statement)
        if digest in existing:
            skipped.append(digest[:12])
            continue
        if not allow_conflicts and any(
            conflict["left_id"] == raw.get("id") or conflict["right_id"] == raw.get("id")
            for conflict in bundle.get("conflicts", [])
        ):
            conflicts.append(digest[:12])
            continue
        try:
            item = service.add(
                project_id=project_id,
                kind=raw["kind"],
                title=raw.get("title") or "Imported memory",
                statement=statement,
                author_kind="SYSTEM",
                status="UNCONFIRMED",
                details={
                    **raw.get("details", {}),
                    "imported_from_bundle": True,
                    "source_fingerprint": digest,
                },
            )
        except Exception as exc:  # noqa: BLE001
            conflicts.append(f"{digest[:12]}: {str(exc)[:120]}")
            continue
        created.append(item.id)
        existing.add(digest)
    return {"created": created, "skipped": skipped, "conflicts": conflicts}


def find_near_duplicates(
    service: Any,
    project_id: str,
    *,
    threshold: float = 0.78,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Find pairs of memories whose statements are near-identical.

    Unlike ``fingerprint`` (which is exact token-set equality), this uses
    Jaccard similarity on token sets, so paraphrased or slightly reworded
    entries are reported. Reporting only: nothing is merged or retracted.
    """
    if not 0.0 < threshold <= 1.0:
        raise ProjectMemoryError("threshold must be in (0, 1]")
    memories = service.list(project_id, limit=limit)
    out: list[dict[str, Any]] = []
    for i, left in enumerate(memories):
        left_tokens = _tokens(left.statement) | _tokens(left.title)
        if not left_tokens:
            continue
        for right in memories[i + 1 :]:
            right_tokens = _tokens(right.statement) | _tokens(right.title)
            if not right_tokens:
                continue
            union = left_tokens | right_tokens
            if not union:
                continue
            similarity = len(left_tokens & right_tokens) / len(union)
            if similarity < threshold:
                continue
            out.append(
                {
                    "left_id": left.id,
                    "right_id": right.id,
                    "similarity": round(similarity, 3),
                    "left_title": left.title,
                    "right_title": right.title,
                    "same_kind": left.kind == right.kind,
                    "advisory": "Reported only; no memory was changed.",
                }
            )
    out.sort(key=lambda item: (-item["similarity"], item["left_id"], item["right_id"]))
    return out


def reconcile(
    service: Any,
    project_id: str,
    *,
    duplicate_threshold: float = 0.78,
) -> dict[str, Any]:
    """Produce an advisory reconciliation plan for a project's memory.

    The plan groups near-duplicate pairs and existing conflicts into suggested
    actions. It never mutates memory; the user (or a later explicit command)
    decides whether to retract or supersede an item.
    """
    duplicates = find_near_duplicates(
        service, project_id, threshold=duplicate_threshold
    )
    conflicts = find_conflicts(service, project_id)
    plan: list[dict[str, Any]] = []
    for item in duplicates:
        plan.append(
            {
                "action": "merge_or_retract_one",
                "ids": [item["left_id"], item["right_id"]],
                "reason": "near-duplicate statements",
                "similarity": item["similarity"],
            }
        )
    for item in conflicts:
        plan.append(
            {
                "action": "resolve_conflict",
                "ids": [item["left_id"], item["right_id"]],
                "reason": item.get("reason", "conflicting statements"),
                "overlap": item.get("overlap"),
            }
        )
    return {
        "project_id": project_id,
        "duplicate_threshold": duplicate_threshold,
        "near_duplicates": duplicates,
        "conflicts": conflicts,
        "plan": plan,
        "advisory": (
            "Reconciliation is advisory. No memory was changed; confirm or "
            "retract items explicitly."
        ),
    }


__all__ = [
    "fingerprint",
    "find_conflicts",
    "find_near_duplicates",
    "reconcile",
    "expired_memory_ids",
    "apply_retention",
    "export_bundle",
    "import_bundle",
]

"""Experience learning from negative results (WP2-01).

A failed approach is recorded as a scoped, provenance-linked memory. Similarity to a
new task goal *suggests* a prior attempt; it never prohibits a new approach. Only
user-confirmed, non-stale, non-expired negative results are considered — the same
trust rule the memory subsystem already uses for all other knowledge.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import MemoryAuthor, MemoryKind, MemoryStatus
from mayaworkbench.domain.models import MemorySource, ProjectMemory
from mayaworkbench.errors import ProjectMemoryError

_TOKEN_RE = re.compile(r"[A-Za-z_][\w-]{2,}")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "when",
        "then",
        "have",
        "make",
        "should",
        "could",
        "would",
        "about",
        "using",
        "after",
    }
)


class MemoryServiceLike(Protocol):
    def list(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ProjectMemory]: ...

    def add(self, **kwargs: Any) -> ProjectMemory: ...


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall(text)
        if token.lower() not in _STOPWORDS
    }


def record_attempt(
    service: MemoryServiceLike,
    *,
    project_id: str,
    goal: str,
    hypothesis: str,
    outcome: str,
    failure_signature: str = "",
    base_commit: str | None = None,
    patch_hash: str | None = None,
    environment: str = "",
    confidence: str = "MEDIUM",
    expires_at: datetime | None = None,
    sources: list[MemorySource] | None = None,
    repository_id: str | None = None,
) -> ProjectMemory:
    """Record a failed approach as a scoped negative-result memory."""
    if not goal.strip() or not hypothesis.strip():
        raise ProjectMemoryError("An attempt needs a goal and a hypothesis")
    if base_commit is not None and len(base_commit) != 40:
        raise ProjectMemoryError("base_commit must be a full 40-character SHA")
    details: dict[str, Any] = {
        "goal": goal.strip(),
        "hypothesis": hypothesis.strip(),
        "outcome": outcome.strip(),
        "failure_signature": failure_signature.strip(),
        "environment": environment.strip(),
        "base_commit": base_commit,
        "patch_hash": patch_hash,
        "recorded_at": utcnow().isoformat(),
    }
    if expires_at is not None:
        details["expires_at"] = expires_at.isoformat()
    return service.add(
        project_id=project_id,
        kind=MemoryKind.NEGATIVE_RESULT,
        title=f"Failed attempt: {hypothesis.strip()[:120]}",
        statement=(
            f"Goal: {goal.strip()}\nHypothesis: {hypothesis.strip()}\n"
            f"Outcome: {outcome.strip()}"
            + (f"\nFailure signature: {failure_signature.strip()}" if failure_signature else "")
        ),
        author_kind=MemoryAuthor.SYSTEM,
        status=MemoryStatus.UNCONFIRMED,
        scope=f"attempt:{base_commit or 'no-commit'}",
        repository_id=repository_id,
        commit_sha=base_commit,
        confidence=confidence,
        sources=sources or [],
        details=details,
    )


def _not_expired(memory: ProjectMemory, now: datetime) -> bool:
    raw = (memory.details or {}).get("expires_at")
    if not raw:
        return True
    try:
        expires = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return expires > now


def suggest_prior_attempts(
    service: MemoryServiceLike,
    *,
    project_id: str,
    goal: str,
    failure_signature: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return similar prior negative results as suggestions, never prohibitions."""
    if limit < 1:
        raise ProjectMemoryError("limit must be at least 1")
    query = _tokens(goal) | _tokens(failure_signature)
    if not query:
        return []
    now = utcnow()
    candidates = service.list(
        project_id,
        kind=MemoryKind.NEGATIVE_RESULT.value,
        status=MemoryStatus.CONFIRMED.value,
        limit=max(100, limit * 10),
    )
    suggestions: list[dict[str, Any]] = []
    for memory in candidates:
        if memory.status is not MemoryStatus.CONFIRMED:
            continue
        if not _not_expired(memory, now):
            continue
        details = memory.details or {}
        prior = _tokens(str(details.get("goal", ""))) | _tokens(
            str(details.get("hypothesis", ""))
        ) | _tokens(str(details.get("failure_signature", "")))
        if not prior:
            continue
        shared = query & prior
        score = len(shared) / len(query | prior)
        if score < 0.1:
            continue
        suggestions.append(
            {
                "memory_id": memory.id,
                "score": round(score, 3),
                "shared_terms": sorted(shared),
                "goal": details.get("goal", memory.title),
                "hypothesis": details.get("hypothesis", ""),
                "failure_signature": details.get("failure_signature", ""),
                "base_commit": details.get("base_commit"),
                "advisory": "A prior attempt; do not treat this as a prohibition.",
            }
        )
    suggestions.sort(key=lambda item: (-item["score"], item["memory_id"]))
    return suggestions[:limit]


__all__ = ["record_attempt", "suggest_prior_attempts"]

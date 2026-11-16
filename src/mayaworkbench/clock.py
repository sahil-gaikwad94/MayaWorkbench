"""Time helpers.

All persisted timestamps are timezone-aware UTC. Storage uses ISO-8601 strings
(microsecond precision) so ordering is lexicographic and portable.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def duration_ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


__all__ = ["utcnow", "to_iso", "from_iso", "duration_ms"]

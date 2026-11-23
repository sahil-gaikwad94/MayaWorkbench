"""Bounded Server-Sent Events reader shared by the native adapters.

Every provider streams SSE differently, but the framing rules are the same:
``event:`` names, one or more ``data:`` lines, comments, and a blank line that
dispatches the frame. This module owns the framing and the hard bounds so a
hostile or broken upstream cannot make an adapter buffer indefinitely.

It performs no I/O other than reading from the supplied file-like response; it
never opens a socket and never sees a credential.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass

from mayaworkbench.errors import ModelError

#: Hard bounds for a single SSE response.
MAX_SSE_BYTES = 4_000_000
MAX_SSE_EVENTS = 10_000
MAX_SSE_LINE = 65_536

#: Upper bound honoured for a provider ``Retry-After`` header. A provider is
#: never allowed to park a workbench process for an arbitrary time.
MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass(frozen=True)
class SSEFrame:
    """One dispatched SSE frame. ``event`` is ``None`` for unnamed frames."""

    event: str | None
    data: str


def iter_sse_frames(
    response,  # type: ignore[no-untyped-def]
    *,
    max_bytes: int = MAX_SSE_BYTES,
    max_events: int = MAX_SSE_EVENTS,
    max_line: int = MAX_SSE_LINE,
) -> Iterator[SSEFrame]:
    """Yield bounded :class:`SSEFrame` objects from a streaming response.

    ``data: [DONE]`` is yielded like any other frame; each adapter decides
    whether that sentinel is meaningful for it. Malformed UTF-8 is replaced
    rather than raised, because a broken keepalive frame must not fail a call.
    """
    byte_count = 0
    event_count = 0
    event_name: str | None = None
    data_lines: list[str] = []

    def dispatch() -> SSEFrame | None:
        nonlocal data_lines, event_count, event_name
        if not data_lines:
            event_name = None
            return None
        frame = SSEFrame(event=event_name, data="\n".join(data_lines))
        data_lines = []
        event_name = None
        event_count += 1
        if event_count > max_events:
            raise ModelError(
                "The provider stream exceeded the event limit",
                error_class="protocol",
                retryable=False,
            )
        return frame

    while True:
        raw = response.readline(max_line + 1)
        if not raw:
            break
        byte_count += len(raw)
        if byte_count > max_bytes:
            raise ModelError(
                "The provider stream exceeded the byte limit",
                error_class="protocol",
                retryable=False,
            )
        if len(raw) > max_line:
            raise ModelError(
                "The provider stream contained an over-long line",
                error_class="protocol",
                retryable=False,
            )
        text = raw.decode("utf-8", errors="replace")
        # A reader may return several physical lines in one chunk (including
        # the blank line that dispatches a frame). Handle each line so framing
        # never depends on how the transport chunks its reads.
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if not line:
                frame = dispatch()
                if frame is not None:
                    yield frame
                continue
            if line.startswith(":"):
                continue  # comment / keepalive
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or None
                continue
            # ``id:``, ``retry:`` and unknown fields carry no completion data.
    frame = dispatch()
    if frame is not None:
        yield frame


def bounded_read(response, limit: int) -> bytes:  # type: ignore[no-untyped-def]
    """Read at most ``limit`` bytes; reject a response that exceeds it."""
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ModelError(
            "The provider response exceeded the size limit",
            error_class="protocol",
            retryable=False,
        )
    return raw


def parse_retry_after(
    value: str | None,
    *,
    max_seconds: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse an HTTP ``Retry-After`` delay (seconds form only), bounded.

    The HTTP-date form is deliberately ignored: a clock-skewed or hostile
    provider must not be able to derive an unbounded sleep from local time.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds != seconds or seconds <= 0:  # NaN or non-positive
        return None
    return min(seconds, max_seconds)


def retry_after_from_error(exc: urllib.error.HTTPError) -> float | None:
    """Best-effort, bounded ``Retry-After`` extraction from an HTTP error."""
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        return parse_retry_after(headers.get("Retry-After"))
    except Exception:  # pragma: no cover - defensive only
        return None


__all__ = [
    "MAX_SSE_BYTES",
    "MAX_SSE_EVENTS",
    "MAX_SSE_LINE",
    "MAX_RETRY_AFTER_SECONDS",
    "SSEFrame",
    "iter_sse_frames",
    "bounded_read",
    "parse_retry_after",
    "retry_after_from_error",
]

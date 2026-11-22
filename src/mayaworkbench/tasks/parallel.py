"""Bounded, order-preserving parallel computation of pure payloads (WP-ORCH-1).

Worker threads only call ``compute`` on one item each. They perform no I/O and
write to no shared object; every side effect (SQLite, artifacts, events, run
state) stays on the calling thread. Per-item exceptions are captured as data so
callers can turn them into typed failure records instead of crashing the pool.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_payloads(
    items: Sequence[T],
    compute: Callable[[T], R],
    *,
    max_workers: int = 4,
) -> list[tuple[int, Any, str | None]]:
    """Compute one payload per item in a bounded thread pool.

    Results come back in original input order as ``(index, payload, error)``
    triples. A failing ``compute`` never escapes the pool: its exception type
    and message are rendered into the third element. The pool is sized to
    ``min(max_workers, len(items))`` (at least one worker for a non-empty
    input); an empty input short-circuits to ``[]``.
    """
    materialized = list(items)
    if not materialized:
        return []
    workers = max(1, min(int(max_workers), len(materialized)))

    def run(index: int, item: T) -> tuple[int, Any, str | None]:
        try:
            return (index, compute(item), None)
        except Exception as exc:  # noqa: BLE001 - per-item failures are data
            return (index, None, f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, position, item) for position, item in enumerate(materialized)]
        return [future.result() for future in futures]


__all__ = ["parallel_payloads"]

"""Identifier generation.

Persisted entities use ULIDs: lexicographically sortable, timestamp-prefixed,
128-bit identifiers that do not leak row counts the way sequential integers do.
Symbol identifiers are content-derived so that the same symbol in the same
snapshot keeps its identity across re-indexing.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a 26-character Crockford-base32 ULID (48-bit ms time + 80-bit entropy)."""
    ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if ts < 0 or ts >= 1 << 48:
        raise ValueError("timestamp out of ULID range")
    entropy = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(entropy, 16)


def short_hash(value: str | bytes, length: int = 16) -> str:
    """Return a truncated hex SHA-256 digest, used for hashes shown to humans."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()[:length]


def stable_symbol_id(snapshot_id: str, file_path: str, kind: str, qualified_name: str) -> str:
    """Deterministic symbol identifier scoped to a snapshot.

    Scoping to the snapshot means symbol identity never silently spans two
    different code revisions; the same snapshot re-indexed produces the same ID.
    """
    key = "\x1f".join(["sym", snapshot_id, file_path, kind, qualified_name])
    return "sym_" + short_hash(key, 32)


def stable_edge_id(
    snapshot_id: str, source_symbol_id: str | None, source_file_id: str, edge_type: str, target_key: str
) -> str:
    key = "\x1f".join(
        ["edge", snapshot_id, source_symbol_id or "", source_file_id, edge_type, target_key]
    )
    return "edg_" + short_hash(key, 32)


def stable_file_id(snapshot_id: str, path: str) -> str:
    return "fil_" + short_hash("\x1f".join(["file", snapshot_id, path]), 32)


def new_correlation_id() -> str:
    return "cor_" + new_ulid()


def default_host_id() -> str:
    """Stable local host identifier used only for local provenance metadata."""
    seed = f"{os.uname().nodename}:{os.getuid() if hasattr(os, 'getuid') else 0}"
    return short_hash(seed, 16)


__all__ = [
    "new_ulid",
    "short_hash",
    "stable_symbol_id",
    "stable_edge_id",
    "stable_file_id",
    "new_correlation_id",
    "default_host_id",
]

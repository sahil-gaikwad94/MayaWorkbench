"""Version constants.

Every persisted record that depends on a parser, indexer, schema, or retrieval
algorithm records the corresponding version here so that evidence can be
reproduced and stale results can be detected instead of silently reused.
"""

from __future__ import annotations

MAYA_VERSION = "0.1.0"

#: Bump whenever index output changes in a way that invalidates prior indexes.
INDEXER_VERSION = "1.0.0"

#: Version of the persisted domain/contract schemas (Task Contract, Claim, ...).
CONTRACT_VERSION = "1.0"

#: Version of the exported JSON schema bundle.
SCHEMA_BUNDLE_VERSION = "1.0"

#: Version of the deterministic answer protocol used by ``maya ask``.
QA_PROTOCOL_VERSION = "1.0"

__all__ = [
    "MAYA_VERSION",
    "INDEXER_VERSION",
    "CONTRACT_VERSION",
    "SCHEMA_BUNDLE_VERSION",
    "QA_PROTOCOL_VERSION",
]

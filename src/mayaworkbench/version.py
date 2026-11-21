"""Version constants.

Every persisted record that depends on a parser, indexer, schema, or retrieval
algorithm records the corresponding version here so that evidence can be
reproduced and stale results can be detected instead of silently reused.
"""

from __future__ import annotations

MAYA_VERSION = "0.5.0"

#: Bump whenever index output changes in a way that invalidates prior indexes.
INDEXER_VERSION = "1.1.0"

#: Version of the persisted domain/contract schemas (Task Contract, Claim, ...).
CONTRACT_VERSION = "1.0"

#: Version of the exported JSON schema bundle.
SCHEMA_BUNDLE_VERSION = "1.4"

#: Version of the deterministic answer protocol used by ``maya ask``.
QA_PROTOCOL_VERSION = "1.0"

#: Version of the document text-extraction and passage-segmentation contract.
EXTRACTION_VERSION = "1.0.0"

#: Version of the research document/passage/claim persisted protocol.
RESEARCH_PROTOCOL_VERSION = "1.0"

#: Version of the deterministic claim-extraction heuristics.
CLAIM_EXTRACTION_VERSION = "1.0.0"

#: Version of the mapping rule pack. Every mapping records the version that
#: produced it, because a status is only meaningful relative to its rules.
MAPPING_RULE_PACK_VERSION = "mapping/v1"

#: Version of the reproduction readiness/risk scoring model.
READINESS_SCORE_VERSION = "readiness/v1"

#: Version of the research memo layout and interpretation-block protocol.
MEMO_PROTOCOL_VERSION = "1.0"

#: Version of the task workflow, approval and plan persisted protocol.
TASK_PROTOCOL_VERSION = "1.0"

#: Version of the typed tool gateway contract and command policy.
TOOL_GATEWAY_VERSION = "1.1"

#: Version of the bounded agent loop protocol (orientation, finish gate,
#: repair feedback and the durable ledger). A run records this so its behaviour
#: is interpretable after the loop changes.
AGENT_LOOP_VERSION = "1.1.0"

#: Version of the experiment definition/manifest/comparison protocol.
EXPERIMENT_PROTOCOL_VERSION = "1.0"

#: Version of the durable project memory record protocol.
MEMORY_PROTOCOL_VERSION = "1.0"

__all__ = [
    "MAYA_VERSION",
    "INDEXER_VERSION",
    "CONTRACT_VERSION",
    "SCHEMA_BUNDLE_VERSION",
    "QA_PROTOCOL_VERSION",
    "EXTRACTION_VERSION",
    "RESEARCH_PROTOCOL_VERSION",
    "CLAIM_EXTRACTION_VERSION",
    "MAPPING_RULE_PACK_VERSION",
    "READINESS_SCORE_VERSION",
    "MEMO_PROTOCOL_VERSION",
    "TASK_PROTOCOL_VERSION",
    "TOOL_GATEWAY_VERSION",
    "AGENT_LOOP_VERSION",
    "EXPERIMENT_PROTOCOL_VERSION",
    "MEMORY_PROTOCOL_VERSION",
]

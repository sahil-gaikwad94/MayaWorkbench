"""Domain enumerations.

These values are part of the persisted contract: changing a member requires a
schema migration. They are deliberately explicit rather than string literals so
that invalid states fail validation instead of reaching storage.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """`str` enum with a stable serialized value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class SnapshotKind(StrEnum):
    """How the snapshot's content was obtained.

    ``COMMITTED`` reads immutable Git objects at a resolved commit.
    ``WORKTREE`` captures tracked and un-tracked working-tree content plus a
    patch artifact, and pins each file by content hash.
    """

    COMMITTED = "committed"
    WORKTREE = "worktree"


class SnapshotStatus(StrEnum):
    CREATED = "CREATED"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"


class FileStatus(StrEnum):
    """Indexing outcome for one file."""

    PARSED = "PARSED"
    LEXICAL_ONLY = "LEXICAL_ONLY"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class ParseCoverage(StrEnum):
    """How thoroughly the indexer understood a file."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    LEXICAL_ONLY = "LEXICAL_ONLY"
    NONE = "NONE"


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    CONSTANT = "constant"
    TYPE_ALIAS = "type_alias"


class EdgeType(StrEnum):
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    INHERITS = "INHERITS"
    DECORATES = "DECORATES"


class EdgeConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IssueKind(StrEnum):
    EXCLUDED_DIRECTORY = "EXCLUDED_DIRECTORY"
    EXCLUDED_GLOB = "EXCLUDED_GLOB"
    SKIPPED_BINARY = "SKIPPED_BINARY"
    SKIPPED_TOO_LARGE = "SKIPPED_TOO_LARGE"
    SKIPPED_SECRET = "SKIPPED_SECRET"
    SKIPPED_SYMLINK = "SKIPPED_SYMLINK"
    SKIPPED_SUBMODULE = "SKIPPED_SUBMODULE"
    SKIPPED_UNREADABLE = "SKIPPED_UNREADABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PARSE_ERROR = "PARSE_ERROR"
    ENCODING_ERROR = "ENCODING_ERROR"
    UNRESOLVED_IMPORT = "UNRESOLVED_IMPORT"
    SECRET_REDACTED = "SECRET_REDACTED"
    MUTATED_DURING_INDEX = "MUTATED_DURING_INDEX"
    LINE_TRUNCATED = "LINE_TRUNCATED"
    SYMBOL_LIMIT_REACHED = "SYMBOL_LIMIT_REACHED"


class IssueSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ArtifactKind(StrEnum):
    FILE_CONTENT = "FILE_CONTENT"
    SNAPSHOT_PATCH = "SNAPSHOT_PATCH"
    UNTRACKED_MANIFEST = "UNTRACKED_MANIFEST"
    FILE_MANIFEST = "FILE_MANIFEST"
    INDEX_MAP = "INDEX_MAP"
    QA_ANSWER = "QA_ANSWER"
    CONTEXT_PACK = "CONTEXT_PACK"
    OTHER = "OTHER"


class ActorKind(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    MODEL = "MODEL"
    TOOL = "TOOL"


class QaStatus(StrEnum):
    """Outcome of a repository question.

    ``ANSWERED`` requires at least one citation that validates against the
    pinned snapshot. Nothing is ever reported as answered on model confidence
    alone.
    """

    ANSWERED = "ANSWERED"
    ANSWERED_WITH_UNCERTAINTY = "ANSWERED_WITH_UNCERTAINTY"
    NO_MODEL = "NO_MODEL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CITATIONS_REJECTED = "CITATIONS_REJECTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    FAILED = "FAILED"


class EventType(StrEnum):
    """Append-only workflow event names from the specification (§17.2)."""

    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_UPDATED = "PROJECT_UPDATED"
    REPOSITORY_ATTACHED = "REPOSITORY_ATTACHED"
    SNAPSHOT_PINNED = "SNAPSHOT_PINNED"
    INDEX_STARTED = "INDEX_STARTED"
    INDEX_COMPLETED = "INDEX_COMPLETED"
    INDEX_FAILED = "INDEX_FAILED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    MODEL_INVOCATION_STARTED = "MODEL_INVOCATION_STARTED"
    MODEL_INVOCATION_COMPLETED = "MODEL_INVOCATION_COMPLETED"
    MODEL_INVOCATION_FAILED = "MODEL_INVOCATION_FAILED"
    QA_ANSWERED = "QA_ANSWERED"
    QA_EVIDENCE_ONLY = "QA_EVIDENCE_ONLY"
    QA_REJECTED = "QA_REJECTED"


class RetentionClass(StrEnum):
    """How long an artifact should live.

    Recorded from Phase 1 so retention policy is explicit before any hosted
    deployment.
    """

    PROJECT = "PROJECT"
    TASK = "TASK"
    EPHEMERAL = "EPHEMERAL"


__all__ = [
    "StrEnum",
    "SnapshotKind",
    "SnapshotStatus",
    "FileStatus",
    "ParseCoverage",
    "SymbolKind",
    "EdgeType",
    "EdgeConfidence",
    "IssueKind",
    "IssueSeverity",
    "ArtifactKind",
    "ActorKind",
    "QaStatus",
    "EventType",
    "RetentionClass",
]

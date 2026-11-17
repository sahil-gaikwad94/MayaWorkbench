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
    SOURCE_DOCUMENT = "SOURCE_DOCUMENT"
    SOURCE_TEXT = "SOURCE_TEXT"
    EXTRACTION_REPORT = "EXTRACTION_REPORT"
    CLAIM_SET = "CLAIM_SET"
    CLAIM_MATRIX = "CLAIM_MATRIX"
    RESEARCH_MEMO = "RESEARCH_MEMO"
    TASK_CONTRACT = "TASK_CONTRACT"
    IMPLEMENTATION_PLAN = "IMPLEMENTATION_PLAN"
    TASK_PATCH = "TASK_PATCH"
    COMMAND_OUTPUT = "COMMAND_OUTPUT"
    TOOL_OUTPUT = "TOOL_OUTPUT"
    TASK_SUMMARY = "TASK_SUMMARY"
    DIFF_REVIEW = "DIFF_REVIEW"
    TASK_EXPORT = "TASK_EXPORT"
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
    SOURCE_INGESTED = "SOURCE_INGESTED"
    SOURCE_EXTRACTION_FAILED = "SOURCE_EXTRACTION_FAILED"
    CLAIMS_EXTRACTED = "CLAIMS_EXTRACTED"
    CLAIM_CREATED = "CLAIM_CREATED"
    CLAIM_UPDATED = "CLAIM_UPDATED"
    CLAIM_REVIEWED = "CLAIM_REVIEWED"
    CLAIM_DAG_VALIDATED = "CLAIM_DAG_VALIDATED"
    CLAIM_MAPPING_CREATED = "CLAIM_MAPPING_CREATED"
    CLAIM_MAPPING_UPDATED = "CLAIM_MAPPING_UPDATED"
    CLAIM_MAPPING_REJECTED = "CLAIM_MAPPING_REJECTED"
    RESEARCH_MEMO_CREATED = "RESEARCH_MEMO_CREATED"
    # Phase 3 — controlled engineering agent
    TASK_CREATED = "TASK_CREATED"
    CONTRACT_CREATED = "CONTRACT_CREATED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_APPROVED = "PLAN_APPROVED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    TASK_STATE_CHANGED = "TASK_STATE_CHANGED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_BLOCKED = "TASK_BLOCKED"
    TASK_CANCELLED = "TASK_CANCELLED"
    WORKTREE_CREATED = "WORKTREE_CREATED"
    WORKTREE_REMOVED = "WORKTREE_REMOVED"
    PATCH_APPLIED = "PATCH_APPLIED"
    TOOL_INVOKED = "TOOL_INVOKED"
    TOOL_BLOCKED = "TOOL_BLOCKED"
    COMMAND_STARTED = "COMMAND_STARTED"
    COMMAND_COMPLETED = "COMMAND_COMPLETED"
    COMMAND_TIMED_OUT = "COMMAND_TIMED_OUT"
    COMMAND_BLOCKED = "COMMAND_BLOCKED"
    DIFF_CAPTURED = "DIFF_CAPTURED"
    DIFF_REVIEWED = "DIFF_REVIEWED"
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_LEASE_ACQUIRED = "WORKER_LEASE_ACQUIRED"
    WORKER_LEASE_LOST = "WORKER_LEASE_LOST"
    WORKER_FINISHED = "WORKER_FINISHED"


# ------------------------------------------------------------ task workflow
class TaskType(StrEnum):
    """Task templates. Phase 3 implements the engineering-change template."""

    ENGINEERING_CHANGE = "ENGINEERING_CHANGE"
    PAPER_TO_CODE = "PAPER_TO_CODE"
    REPOSITORY_QA = "REPOSITORY_QA"
    RESEARCH_ANALYSIS = "RESEARCH_ANALYSIS"


class WorkflowState(StrEnum):
    """Lifecycle states from §10.4, restricted to Phase 3 transitions.

    ``PAUSED`` is added deliberately: Phase 3 requires pause/resume, and a
    terminal-only pause would make resumption a new run.
    """

    CREATED = "CREATED"
    CONTRACTED = "CONTRACTED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    PAUSED = "PAUSED"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_LIMITATIONS = "COMPLETED_WITH_LIMITATIONS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    NEEDS_HUMAN_DECISION = "NEEDS_HUMAN_DECISION"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset(
    {
        WorkflowState.COMPLETED,
        WorkflowState.COMPLETED_WITH_LIMITATIONS,
        WorkflowState.FAILED,
        WorkflowState.BLOCKED,
        WorkflowState.CANCELLED,
        WorkflowState.NEEDS_HUMAN_DECISION,
    }
)


class ApprovalAction(StrEnum):
    """What a durable approval authorizes."""

    WRITE_ACCESS = "WRITE_ACCESS"
    COMMAND_EXECUTION = "COMMAND_EXECUTION"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


class ExecutionProfile(StrEnum):
    READ_ONLY = "READ_ONLY"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    CPU_SMOKE = "CPU_SMOKE"
    USER_APPROVED_LOCAL = "USER_APPROVED_LOCAL"


class NetworkPolicy(StrEnum):
    DENY_BY_DEFAULT = "DENY_BY_DEFAULT"
    ALLOW_WITH_APPROVAL = "ALLOW_WITH_APPROVAL"


class DependencyPolicy(StrEnum):
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class WorkspaceStatus(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    FINALIZED = "FINALIZED"
    REMOVED = "REMOVED"
    FAILED = "FAILED"


class ToolCallStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    TIMED_OUT = "TIMED_OUT"


class WorkerJobStatus(StrEnum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ------------------------------------------------------------------- research
class DocumentMediaKind(StrEnum):
    """How a research document's text is extracted.

    The media kind is detected from the bytes and the filename, never guessed
    from a file extension alone: a ``.pdf`` that is not a PDF is treated as
    text and the mismatch is recorded as an extraction warning.
    """

    MARKDOWN = "MARKDOWN"
    TEXT = "TEXT"
    PDF = "PDF"


class DocumentOrigin(StrEnum):
    """Where the registered bytes came from."""

    FILE = "FILE"
    URL = "URL"
    DOI = "DOI"


class ExtractionStatus(StrEnum):
    REGISTERED = "REGISTERED"
    EXTRACTED = "EXTRACTED"
    FAILED = "FAILED"


class PassageKind(StrEnum):
    """Structural role of one source passage.

    ``HEADING`` passages exist so section structure is itself citable; the
    remaining kinds are content blocks. ``EQUATION`` is only assigned when the
    block carries an explicit equation marker (``$$``, ``\\begin{``, a numbered
    display, or a math-dense standalone block in extracted PDF text).
    """

    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    TABLE = "TABLE"
    CODE = "CODE"
    EQUATION = "EQUATION"
    CAPTION = "CAPTION"


class ClaimType(StrEnum):
    """Claim categories from the specification (§9.2). Never merged."""

    EXPLICIT_SOURCE_CLAIM = "EXPLICIT_SOURCE_CLAIM"
    INFERRED_REQUIREMENT = "INFERRED_REQUIREMENT"
    USER_REQUIREMENT = "USER_REQUIREMENT"
    IMPLEMENTATION_DECISION = "IMPLEMENTATION_DECISION"


class ClaimAuthor(StrEnum):
    """Who produced a claim. Heuristic output is never treated as the paper's word."""

    USER = "USER"
    HEURISTIC = "HEURISTIC"
    MODEL = "MODEL"


class ClaimConfidence(StrEnum):
    """Confidence that a claim is a fair reading of its source passage.

    This is extraction confidence, not mapping confidence and not a probability.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ClaimReviewStatus(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ClaimMappingStatus(StrEnum):
    """Claim-to-code status from §9.4.

    ``PRESENT`` requires evidence beyond a name match: at least one verified
    definition-level code span and full coverage of the claim's declared
    acceptance signals. Rules always live in a versioned rule pack.
    """

    PRESENT = "PRESENT"
    PARTIAL = "PARTIAL"
    ABSENT = "ABSENT"
    UNCERTAIN = "UNCERTAIN"
    CONFLICTING = "CONFLICTING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MappingEvidenceRelation(StrEnum):
    """How one code span relates to a claim requirement."""

    IMPLEMENTS = "IMPLEMENTS"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    TEST_COVERS = "TEST_COVERS"


class EvidenceStrength(StrEnum):
    """Strength of a code span as evidence for a claim requirement."""

    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


class MappingAuthor(StrEnum):
    """Who assigned a claim mapping status."""

    DETERMINISTIC = "DETERMINISTIC"
    MODEL = "MODEL"
    USER = "USER"


class RiskCategory(StrEnum):
    """Reproduction risk dimensions from §9.6. Kept separate on purpose."""

    PAPER_DETAIL = "PAPER_DETAIL"
    CODE_MAPPING = "CODE_MAPPING"
    DATA_ACCESS = "DATA_ACCESS"
    COMPUTE = "COMPUTE"
    LICENSING = "LICENSING"
    EVALUATION = "EVALUATION"


class RiskSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"


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
    "DocumentMediaKind",
    "DocumentOrigin",
    "ExtractionStatus",
    "PassageKind",
    "ClaimType",
    "ClaimAuthor",
    "ClaimConfidence",
    "ClaimReviewStatus",
    "ClaimMappingStatus",
    "MappingEvidenceRelation",
    "EvidenceStrength",
    "MappingAuthor",
    "RiskCategory",
    "RiskSeverity",
    "TaskType",
    "WorkflowState",
    "TERMINAL_STATES",
    "ApprovalAction",
    "ApprovalStatus",
    "ExecutionProfile",
    "NetworkPolicy",
    "DependencyPolicy",
    "WorkspaceStatus",
    "ToolCallStatus",
    "WorkerJobStatus",
]

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


class RelationSource(StrEnum):
    """How a symbol/edge relation was discovered (WP5).

    Python AST and optional Tree-sitter CST adapters produce AST relations;
    the regex fallback produces HEURISTIC relations. Remaining values describe
    future adapters and language-server data.
    """

    AST_IMPORT = "AST_IMPORT"
    #: A symbol/definition recovered from a real concrete syntax tree. Distinct
    #: from AST_IMPORT so AST-backed non-Python symbols are not mislabelled as
    #: import edges. Added 2026-11-22 for the optional Tree-sitter adapters.
    AST_SYMBOL = "AST_SYMBOL"
    TYPE_REFERENCE = "TYPE_REFERENCE"
    CALL_SITE = "CALL_SITE"
    BUILD_DEPENDENCY = "BUILD_DEPENDENCY"
    CONFIG_REFERENCE = "CONFIG_REFERENCE"
    HEURISTIC = "HEURISTIC"


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
    INDEX_FILE_CACHE = "INDEX_FILE_CACHE"
    QA_ANSWER = "QA_ANSWER"
    CONTEXT_PACK = "CONTEXT_PACK"
    SOURCE_DOCUMENT = "SOURCE_DOCUMENT"
    SOURCE_TEXT = "SOURCE_TEXT"
    SOURCE_METADATA = "SOURCE_METADATA"
    EXTRACTION_REPORT = "EXTRACTION_REPORT"
    SOURCE_OCR = "SOURCE_OCR"
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
    VERIFICATION_PLAN = "VERIFICATION_PLAN"
    VERIFICATION_CHECK = "VERIFICATION_CHECK"
    VERIFICATION_REPORT = "VERIFICATION_REPORT"
    BASELINE_CAPTURE = "BASELINE_CAPTURE"
    TEST_INTEGRITY_REPORT = "TEST_INTEGRITY_REPORT"
    MODEL_REVIEW = "MODEL_REVIEW"
    EVIDENCE_BUNDLE = "EVIDENCE_BUNDLE"
    # Phase 5 — Experiment Lab and durable memory
    EXPERIMENT_PROTOCOL = "EXPERIMENT_PROTOCOL"
    EXPERIMENT_MANIFEST = "EXPERIMENT_MANIFEST"
    EXPERIMENT_OUTPUT = "EXPERIMENT_OUTPUT"
    EXPERIMENT_METRICS = "EXPERIMENT_METRICS"
    EXPERIMENT_REPORT = "EXPERIMENT_REPORT"
    EXPERIMENT_BUNDLE = "EXPERIMENT_BUNDLE"
    PROJECT_MEMORY = "PROJECT_MEMORY"
    # WP R2I — backend-computed, inspectable four-tier reproduction report.
    # Additive only: no migration is required for an enum member.
    REPRODUCIBILITY_REPORT = "REPRODUCIBILITY_REPORT"
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
    # Research evidence quality — append-only citation/disagreement records.
    CLAIM_CITATION_RECORDED = "CLAIM_CITATION_RECORDED"
    CLAIM_DISAGREEMENT_RECORDED = "CLAIM_DISAGREEMENT_RECORDED"
    CLAIM_DISAGREEMENT_RESOLVED = "CLAIM_DISAGREEMENT_RESOLVED"
    RESEARCH_MEMO_CREATED = "RESEARCH_MEMO_CREATED"
    REPRODUCIBILITY_REPORT_CREATED = "REPRODUCIBILITY_REPORT_CREATED"
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
    # Phase 6 — deterministic feedback inside the agent loop
    AGENT_CHECK_COMPLETED = "AGENT_CHECK_COMPLETED"
    AGENT_REPAIR_REQUESTED = "AGENT_REPAIR_REQUESTED"
    # Phase 4 — verification and evidence
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_CHECK_COMPLETED = "VERIFICATION_CHECK_COMPLETED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_OVERRIDDEN = "VERIFICATION_OVERRIDDEN"
    MODEL_REVIEW_COMPLETED = "MODEL_REVIEW_COMPLETED"
    EVIDENCE_BUNDLE_EXPORTED = "EVIDENCE_BUNDLE_EXPORTED"
    # Phase 5 — Experiment Lab and durable memory
    EXPERIMENT_CREATED = "EXPERIMENT_CREATED"
    EXPERIMENT_APPROVED = "EXPERIMENT_APPROVED"
    EXPERIMENT_REVOKED = "EXPERIMENT_REVOKED"
    EXPERIMENT_RUN_STARTED = "EXPERIMENT_RUN_STARTED"
    EXPERIMENT_RUN_COMPLETED = "EXPERIMENT_RUN_COMPLETED"
    EXPERIMENT_RUN_FAILED = "EXPERIMENT_RUN_FAILED"
    EXPERIMENT_RUN_BLOCKED = "EXPERIMENT_RUN_BLOCKED"
    EXPERIMENT_METRICS_INGESTED = "EXPERIMENT_METRICS_INGESTED"
    EXPERIMENT_COMPARED = "EXPERIMENT_COMPARED"
    EXPERIMENT_BUNDLE_EXPORTED = "EXPERIMENT_BUNDLE_EXPORTED"
    MEMORY_CREATED = "MEMORY_CREATED"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    MEMORY_DELETED = "MEMORY_DELETED"
    MEMORY_STALE = "MEMORY_STALE"
    # WP4 — Engineering Workspace
    WORKSPACE_CREATED = "WORKSPACE_CREATED"
    RESOURCE_ATTACHED = "RESOURCE_ATTACHED"
    ENVIRONMENT_SPECIFIED = "ENVIRONMENT_SPECIFIED"
    WORKSPACE_SESSION_OPENED = "WORKSPACE_SESSION_OPENED"
    WORKSPACE_SESSION_CLOSED = "WORKSPACE_SESSION_CLOSED"
    EVIDENCE_ATTACHED = "EVIDENCE_ATTACHED"


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
    AWAITING_PLAN_REVIEW = "AWAITING_PLAN_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ENVIRONMENT_PROVISIONING = "ENVIRONMENT_PROVISIONING"
    IMPLEMENTING = "IMPLEMENTING"
    REPAIRING = "REPAIRING"
    PAUSED = "PAUSED"
    REVIEWING = "REVIEWING"
    EVIDENCE_BUILDING = "EVIDENCE_BUILDING"
    PUBLISHING_DRAFT = "PUBLISHING_DRAFT"
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


class ResourceKind(StrEnum):
    """Typed attachments to an Engineering Workspace."""

    REPOSITORY = "REPOSITORY"
    DOCUMENT_SET = "DOCUMENT_SET"
    DATASET = "DATASET"
    NOTEBOOK = "NOTEBOOK"
    SERVICE = "SERVICE"
    DATABASE_SCHEMA = "DATABASE_SCHEMA"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    EXPERIMENT_TRACKER = "EXPERIMENT_TRACKER"
    CONNECTOR = "CONNECTOR"


class WorkspaceSessionState(StrEnum):
    OPEN = "OPEN"
    STOPPED = "STOPPED"
    EXPIRED = "EXPIRED"


class EvidenceStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    CONTESTED = "CONTESTED"


class ToolCapability(StrEnum):
    """Typed grants a task contract can give to the tool gateway.

    An empty grant list means "legacy contract": the gateway still enforces
    approvals and path/command policy, but does not narrow the old tool set. A
    non-empty list is a closed allowlist.
    """

    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    PROCESS_EXECUTE = "process.execute"
    PROCESS_SESSION = "process.session"
    NETWORK_FETCH = "network.fetch"
    EXPERIMENT_RUN = "experiment.run"
    CONNECTOR_READ = "connector.read"
    ADVANCED_TOOL = "tool.advanced"
    SKILL_USE = "skill.use"


class ExecutionTrust(StrEnum):
    """Where and under what boundary project code executes.

    ``LOCAL_TRUSTED`` is the only profile currently implemented. It is the
    local user's privileges, not a sandbox. The isolated profiles are declared
    so policy can refuse an untrusted task instead of silently downgrading it.
    """

    READ_ONLY_ANALYSIS = "READ_ONLY_ANALYSIS"
    LOCAL_TRUSTED = "LOCAL_TRUSTED"
    ROOTLESS_CONTAINER = "ROOTLESS_CONTAINER"
    REMOTE_MICROVM = "REMOTE_MICROVM"
    GPU_RUNNER = "GPU_RUNNER"


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
    """Legacy claim categories from the specification (§9.2). Never merged.

    Kept as the persisted compatibility axis of the typed claim graph: every
    typed :class:`ClaimKind` still maps to exactly one of these values so older
    callers and stored rows remain valid.
    """

    EXPLICIT_SOURCE_CLAIM = "EXPLICIT_SOURCE_CLAIM"
    INFERRED_REQUIREMENT = "INFERRED_REQUIREMENT"
    USER_REQUIREMENT = "USER_REQUIREMENT"
    IMPLEMENTATION_DECISION = "IMPLEMENTATION_DECISION"


class ClaimKind(StrEnum):
    """Typed claim kinds of the versioned research-to-implementation graph.

    These are the six kinds from audit §7.1. They are *not* merged with each
    other, and a paper claim never silently becomes a requirement.
    """

    PAPER_METHOD = "PAPER_METHOD"
    PAPER_RESULT = "PAPER_RESULT"
    REPRODUCTION_REQUIREMENT = "REPRODUCTION_REQUIREMENT"
    USER_REQUIREMENT = "USER_REQUIREMENT"
    INFERRED_REQUIREMENT = "INFERRED_REQUIREMENT"
    VALIDATION_CLAIM = "VALIDATION_CLAIM"


#: The legacy type a typed claim presents to older validation/storage code.
LEGACY_CLAIM_TYPE_FOR_KIND: dict[ClaimKind, ClaimType] = {
    ClaimKind.PAPER_METHOD: ClaimType.EXPLICIT_SOURCE_CLAIM,
    ClaimKind.PAPER_RESULT: ClaimType.EXPLICIT_SOURCE_CLAIM,
    ClaimKind.REPRODUCTION_REQUIREMENT: ClaimType.IMPLEMENTATION_DECISION,
    ClaimKind.USER_REQUIREMENT: ClaimType.USER_REQUIREMENT,
    ClaimKind.INFERRED_REQUIREMENT: ClaimType.INFERRED_REQUIREMENT,
    ClaimKind.VALIDATION_CLAIM: ClaimType.USER_REQUIREMENT,
}

#: The closest typed kind for a legacy claim type (used for stored rows and
#: callers that have not opted into ``--kind`` yet).
KIND_FOR_LEGACY_CLAIM_TYPE: dict[ClaimType, ClaimKind] = {
    ClaimType.EXPLICIT_SOURCE_CLAIM: ClaimKind.PAPER_METHOD,
    ClaimType.INFERRED_REQUIREMENT: ClaimKind.INFERRED_REQUIREMENT,
    ClaimType.USER_REQUIREMENT: ClaimKind.USER_REQUIREMENT,
    ClaimType.IMPLEMENTATION_DECISION: ClaimKind.REPRODUCTION_REQUIREMENT,
}


class ClaimExplicitness(StrEnum):
    """Whether a claim states what the source says or is Maya's inference."""

    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"


class ClaimTargetKind(StrEnum):
    """What a claim's ``target_resources`` entry points at."""

    SYMBOL = "SYMBOL"
    FILE = "FILE"
    DIRECTORY = "DIRECTORY"
    TEST = "TEST"
    DATA = "DATA"


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
    #: No mapping check has been performed. Never emitted by deterministic
    #: rules and never used as a synonym for UNCERTAIN or ABSENT.
    NOT_CHECKED = "NOT_CHECKED"


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


class PaperStatus(StrEnum):
    """Editorial status of a discovered/source paper version.

    ``ACTIVE`` is the ordinary state. The other values are recorded facts about
    the source, not verdicts about any claim that cites it: they feed readiness
    and reproduction *inputs* and never fabricate a claim decision.
    """

    ACTIVE = "ACTIVE"
    CORRECTED = "CORRECTED"
    RETRACTED = "RETRACTED"
    SUPERSEDED = "SUPERSEDED"


class DisagreementKind(StrEnum):
    """Which two evidence axes disagree."""

    PAPER_VS_CODE = "PAPER_VS_CODE"
    PAPER_VS_EXPERIMENT = "PAPER_VS_EXPERIMENT"
    CODE_VS_EXPERIMENT = "CODE_VS_EXPERIMENT"
    SOURCE_VERSION_CONFLICT = "SOURCE_VERSION_CONFLICT"


class DisagreementStatus(StrEnum):
    """Resolution state of one evidence disagreement."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class CitationGranularity(StrEnum):
    """Smallest textual unit a citation quote pins."""

    PASSAGE = "PASSAGE"
    SENTENCE = "SENTENCE"
    SUBCLAUSE = "SUBCLAUSE"
    EQUATION = "EQUATION"
    TABLE_CELL = "TABLE_CELL"


class CitationRelation(StrEnum):
    """How the quoted text relates to the claim it supports."""

    SUPPORTS = "SUPPORTS"
    QUALIFIES = "QUALIFIES"
    CONTRADICTS = "CONTRADICTS"


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


class VerificationGate(StrEnum):
    """Gates A-G from the specification (§13.2).

    A gate is a question about evidence, not a checklist of intentions. A gate
    with no applicable evidence is reported as ``SKIPPED``/``UNVERIFIED`` rather
    than passed.
    """

    SCOPE = "A_SCOPE"
    CHANGE = "B_CHANGE"
    FUNCTIONAL = "C_FUNCTIONAL"
    ML_INVARIANT = "D_ML_INVARIANT"
    REVIEW = "E_REVIEW"
    EXPERIMENT = "F_EXPERIMENT"
    REPORT = "G_REPORT"


class CheckKind(StrEnum):
    """What produced a verification check's outcome."""

    DETERMINISTIC = "DETERMINISTIC"
    COMMAND = "COMMAND"
    TEST_INTEGRITY = "TEST_INTEGRITY"
    ML_INVARIANT = "ML_INVARIANT"
    MODEL_REVIEW = "MODEL_REVIEW"
    HUMAN = "HUMAN"


class CheckOutcome(StrEnum):
    """Outcome of one verification check (§13.1).

    ``PASSED`` means the check ran and met its condition. ``SKIPPED``,
    ``BLOCKED``, ``INCONCLUSIVE`` and ``UNVERIFIED`` are distinct on purpose:
    they have different causes and different consequences for a decision.
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNVERIFIED = "UNVERIFIED"


class EvidenceProvenance(StrEnum):
    """Where a claim or evidence item originated (§13.1)."""

    REPOSITORY = "REPOSITORY"
    DOCUMENT = "DOCUMENT"
    PATCH = "PATCH"
    COMMAND_OUTPUT = "COMMAND_OUTPUT"
    TEST_RESULT = "TEST_RESULT"
    EXPERIMENT_ARTIFACT = "EXPERIMENT_ARTIFACT"
    MODEL_INTERPRETATION = "MODEL_INTERPRETATION"
    HUMAN_NOTE = "HUMAN_NOTE"
    ARTIFACT = "ARTIFACT"


class VerificationMethod(StrEnum):
    """How a claim was checked (§13.1)."""

    SOURCE_INSPECTION = "SOURCE_INSPECTION"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    DETERMINISTIC_TEST = "DETERMINISTIC_TEST"
    NUMERICAL_INVARIANT = "NUMERICAL_INVARIANT"
    RUNTIME_OBSERVATION = "RUNTIME_OBSERVATION"
    EXPERIMENT = "EXPERIMENT"
    INDEPENDENT_MODEL_REVIEW = "INDEPENDENT_MODEL_REVIEW"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    NOT_CHECKED = "NOT_CHECKED"


class EvidenceRelation(StrEnum):
    """How one evidence node relates to another."""

    PROVES = "PROVES"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    DERIVED_FROM = "DERIVED_FROM"
    RECORDED_IN = "RECORDED_IN"


class CriterionStatus(StrEnum):
    """Evidence status of one acceptance criterion."""

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"


class TaskDecision(StrEnum):
    """Final task decisions (§13.5). Never inferred from model confidence."""

    ACCEPTED = "ACCEPTED"
    COMPLETED_WITH_LIMITATIONS = "COMPLETED_WITH_LIMITATIONS"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NEEDS_HUMAN_DECISION = "NEEDS_HUMAN_DECISION"


class FindingSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKER = "BLOCKER"


# -------------------------------------------------- experiments (Phase 5)
class ExperimentRole(StrEnum):
    """Which arm of a comparison an experiment belongs to (§14.2)."""

    BASELINE = "BASELINE"
    CANDIDATE = "CANDIDATE"
    ABLATION = "ABLATION"


class ExperimentProfile(StrEnum):
    """Compute profiles from §14.5.

    Phase 5 executes ``CPU_SMOKE`` and ``SMALL_VALIDATION`` locally. ``STATIC``
    means "provenance only, metrics supplied externally" (a user-owned GPU run);
    ``FULL_EXPERIMENT`` and ``LARGE_TRAINING`` are declared but refused locally
    rather than silently treated as a small run.
    """

    STATIC = "STATIC"
    CPU_SMOKE = "CPU_SMOKE"
    SMALL_VALIDATION = "SMALL_VALIDATION"
    FULL_EXPERIMENT = "FULL_EXPERIMENT"
    LARGE_TRAINING = "LARGE_TRAINING"


class ExperimentStatus(StrEnum):
    """Approval state of an experiment definition."""

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"


class ExperimentRunStatus(StrEnum):
    """Lifecycle of one execution attempt of one seed."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class MetricDirection(StrEnum):
    """Which direction is better. This is a declared property, never inferred."""

    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    NEUTRAL = "NEUTRAL"


class MetricAggregation(StrEnum):
    """How multiple raw observations inside one run become one replicate value."""

    LAST = "LAST"
    MEAN = "MEAN"
    MEDIAN = "MEDIAN"
    MAX = "MAX"
    MIN = "MIN"
    SUM = "SUM"


class MetricsSourceKind(StrEnum):
    """How a run's metrics are obtained.

    ``IMPORTED`` means the metrics were produced outside this workbench (for
    example a user-owned GPU run) and ingested with their provenance; it is
    never presented as a locally executed measurement.
    """

    JSON_FILE = "JSON_FILE"
    STDOUT_JSON = "STDOUT_JSON"
    STDOUT_JSONL = "STDOUT_JSONL"
    IMPORTED = "IMPORTED"


class ExperimentDesign(StrEnum):
    """Statistical design chosen from the observed pairing structure."""

    PAIRED = "PAIRED"
    UNPAIRED = "UNPAIRED"


class ExperimentVerdict(StrEnum):
    """Outcome of a fair comparison. Never a claim of scientific truth."""

    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    NO_DETECTABLE_DIFFERENCE = "NO_DETECTABLE_DIFFERENCE"
    CANDIDATE_ADVANTAGE = "CANDIDATE_ADVANTAGE"
    BASELINE_ADVANTAGE = "BASELINE_ADVANTAGE"
    CANDIDATE_NON_INFERIOR = "CANDIDATE_NON_INFERIOR"


class ProtocolComparison(StrEnum):
    """Classification of one protocol field difference (§14.3)."""

    MATCH = "MATCH"
    EXPECTED_TREATMENT = "EXPECTED_TREATMENT"
    ACCEPTABLE_VARIATION = "ACCEPTABLE_VARIATION"
    FAIRNESS_VIOLATION = "FAIRNESS_VIOLATION"


class MemoryKind(StrEnum):
    """Durable project knowledge categories (§15.1)."""

    FACT = "FACT"
    DECISION = "DECISION"
    CONVENTION = "CONVENTION"
    NEGATIVE_RESULT = "NEGATIVE_RESULT"
    EXPERIMENT_RESULT = "EXPERIMENT_RESULT"
    LIMITATION = "LIMITATION"


class MemoryStatus(StrEnum):
    """Epistemic status of a memory record.

    ``UNCONFIRMED`` is where model-authored or inferred items start; repetition
    never promotes one to ``CONFIRMED``. ``STALE`` means the pinned revision it
    described is no longer the current one.
    """

    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED"
    STALE = "STALE"
    RETRACTED = "RETRACTED"


class MemoryAuthor(StrEnum):
    """Who asserted a memory item."""

    USER = "USER"
    MODEL = "MODEL"
    SYSTEM = "SYSTEM"


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
    "ClaimKind",
    "LEGACY_CLAIM_TYPE_FOR_KIND",
    "KIND_FOR_LEGACY_CLAIM_TYPE",
    "ClaimExplicitness",
    "ClaimTargetKind",
    "ClaimAuthor",
    "ClaimConfidence",
    "ClaimReviewStatus",
    "ClaimMappingStatus",
    "PaperStatus",
    "DisagreementKind",
    "DisagreementStatus",
    "CitationGranularity",
    "CitationRelation",
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
    "ExecutionTrust",
    "ToolCapability",
    "RelationSource",
    "ResourceKind",
    "WorkspaceSessionState",
    "EvidenceStatus",
    "NetworkPolicy",
    "DependencyPolicy",
    "WorkspaceStatus",
    "ToolCallStatus",
    "WorkerJobStatus",
    "VerificationGate",
    "CheckKind",
    "CheckOutcome",
    "EvidenceProvenance",
    "VerificationMethod",
    "EvidenceRelation",
    "CriterionStatus",
    "TaskDecision",
    "FindingSeverity",
    "ExperimentRole",
    "ExperimentProfile",
    "ExperimentStatus",
    "ExperimentRunStatus",
    "MetricDirection",
    "MetricAggregation",
    "MetricsSourceKind",
    "ExperimentDesign",
    "ExperimentVerdict",
    "ProtocolComparison",
    "MemoryKind",
    "MemoryStatus",
    "MemoryAuthor",
]

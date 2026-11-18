"""Domain records.

These models are the persisted contract. They are deliberately independent of
FastAPI, SQLite, and provider SDKs: the storage layer maps them to rows, and the
CLI/API layer maps them to presentation.

Invariants worth knowing:

* A :class:`RepositorySnapshot` is immutable in content terms. Its status and
  index metadata change, but ``commit_sha``/``patch_hash``/file hashes never do.
* ``dirty`` records whether the *user's working tree* had uncommitted changes at
  capture time, independent of whether those changes were included (§8.1).
* Citations are validated against indexed files, never accepted from a model.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ApprovalAction,
    ApprovalStatus,
    ArtifactKind,
    CheckKind,
    CheckOutcome,
    ClaimAuthor,
    ClaimConfidence,
    ClaimMappingStatus,
    ClaimReviewStatus,
    ClaimType,
    CriterionStatus,
    DocumentMediaKind,
    DocumentOrigin,
    EdgeConfidence,
    EdgeType,
    EventType,
    EvidenceRelation,
    EvidenceStrength,
    FindingSeverity,
    ExtractionStatus,
    FileStatus,
    IssueKind,
    IssueSeverity,
    MappingAuthor,
    MappingEvidenceRelation,
    ParseCoverage,
    PassageKind,
    QaStatus,
    RetentionClass,
    RiskCategory,
    RiskSeverity,
    SnapshotKind,
    TaskDecision,
    TaskType,
    ToolCallStatus,
    VerificationGate,
    VerificationMethod,
    WorkflowState,
    WorkerJobStatus,
    WorkspaceStatus,
    SnapshotStatus,
    SymbolKind,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class Record(BaseModel):
    """Base for persisted records: strict, no silent extra fields."""

    model_config = ConfigDict(extra="forbid")


class Project(Record):
    id: str
    name: str = Field(min_length=1, max_length=200)
    slug: str
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9._-]{0,62}$")
        return value


class Repository(Record):
    id: str
    project_id: str
    name: str
    root_path: str
    vcs: Literal["git"] = "git"
    remote_url: str | None = None
    default_branch: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class RepositorySnapshot(Record):
    """A pinned, content-identified view of a repository."""

    id: str
    repository_id: str
    kind: SnapshotKind
    status: SnapshotStatus = SnapshotStatus.CREATED
    # Git identity
    commit_sha: str | None = None
    tree_sha: str | None = None
    branch: str | None = None
    base_commit_sha: str | None = None
    dirty: bool = False
    includes_worktree_patch: bool = False
    patch_hash: str | None = None
    patch_artifact_id: str | None = None
    untracked_manifest_artifact_id: str | None = None
    included_paths: list[str] = Field(default_factory=list)
    submodules: dict[str, str] = Field(default_factory=dict)
    # Index bookkeeping
    indexer_version: str
    indexed_at: datetime | None = None
    file_count: int = 0
    symbol_count: int = 0
    total_bytes: int = 0
    error: str | None = None
    index_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def identity_label(self) -> str:
        """Human label that never claims more precision than we have."""
        sha = (self.commit_sha or "unknown")[:12]
        if self.kind == SnapshotKind.WORKTREE and self.patch_hash:
            return f"{sha}+patch:{self.patch_hash[:12]}"
        return sha


class IndexedFile(Record):
    id: str
    snapshot_id: str
    path: str
    directory: str
    extension: str
    language: str
    size_bytes: int
    line_count: int
    content_sha256: str
    git_blob_sha: str | None = None
    mode: str | None = None
    status: FileStatus
    parse_coverage: ParseCoverage
    is_test: bool = False
    is_doc: bool = False
    is_config: bool = False
    is_binary: bool = False
    skip_reason: str | None = None
    content_artifact_id: str | None = None


class Symbol(Record):
    id: str
    snapshot_id: str
    file_id: str
    path: str
    kind: SymbolKind
    name: str
    qualified_name: str
    signature: str = ""
    start_line: int
    end_line: int
    docstring: str | None = None
    decorators: list[str] = Field(default_factory=list)
    confidence: EdgeConfidence = EdgeConfidence.HIGH
    parent_symbol_id: str | None = None

    @field_validator("start_line", "end_line")
    @classmethod
    def _positive_lines(cls, value: int) -> int:
        if value < 1:
            raise ValueError("line numbers are 1-based")
        return value

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


class SymbolEdge(Record):
    id: str
    snapshot_id: str
    source_symbol_id: str | None
    source_file_id: str
    target_symbol_id: str | None = None
    target_file_id: str | None = None
    edge_type: EdgeType
    target_name: str
    line: int
    confidence: EdgeConfidence = EdgeConfidence.MEDIUM
    provenance: str = "ast"


class IndexIssue(Record):
    id: str
    snapshot_id: str
    file_id: str | None = None
    path: str | None = None
    kind: IssueKind
    severity: IssueSeverity = IssueSeverity.WARNING
    detail: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Artifact(Record):
    id: str
    sha256: str
    size_bytes: int
    media_type: str
    kind: ArtifactKind
    rel_path: str
    retention: RetentionClass = RetentionClass.PROJECT
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)


class WorkflowEvent(Record):
    event_id: str
    occurred_at: datetime = Field(default_factory=utcnow)
    event_type: EventType
    project_id: str
    repository_id: str | None = None
    snapshot_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    entity_type: str
    entity_id: str
    actor_kind: ActorKind = ActorKind.SYSTEM
    actor_name: str = "maya"
    correlation_id: str | None = None
    schema_version: str = "1.0"
    payload: dict[str, Any] = Field(default_factory=dict)


class ModelInvocation(Record):
    """Provenance for one model call. Never contains prompts or secrets."""

    id: str
    project_id: str
    snapshot_id: str | None = None
    purpose: str
    provider: str
    model: str
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "REFUSED"] = "STARTED"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    retry_count: int = 0
    error_class: str | None = None
    provider_request_id: str | None = None
    cost_usd: float | None = None
    cost_basis: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Citation(Record):
    """A source reference produced by the model and validated by us."""

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = ""
    reason: str = ""
    valid: bool = True
    validation: str = "unchecked"

    @field_validator("end_line")
    @classmethod
    def _range_order(cls, value: int, info: Any) -> int:
        start = info.data.get("start_line")
        if start is not None and value < start:
            raise ValueError("end_line must be >= start_line")
        return value

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


class QaRecord(Record):
    id: str
    project_id: str
    snapshot_id: str
    question: str
    status: QaStatus
    answer_markdown: str
    citations: list[Citation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    model_invocation_id: str | None = None
    artifact_id: str | None = None
    context_token_estimate: int = 0
    token_budget: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------- research
class SourceLocation(Record):
    """A verified pointer into one source passage.

    ``quote_hash`` is the SHA-256 of the whitespace-normalised quote, and the
    quote must appear verbatim inside the referenced passage. Verification
    re-reads the stored document text rather than trusting this record.
    """

    passage_id: str
    quote_hash: str
    page: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: str = ""
    equation: str | None = None
    quote: str = ""

    @property
    def label(self) -> str:
        where = f"p.{self.page}" if self.page else "no page"
        if self.section:
            return f"{where} · {self.section}"
        return where


class ResearchDocument(Record):
    """One immutable content version of a research document.

    Versions of the same paper share a ``document_key`` and increment
    ``version``; ``supersedes_document_id`` links the chain. Extraction metadata
    is recorded so a memo can state which tool produced its quotes.
    """

    id: str
    project_id: str
    document_key: str
    version: int = Field(default=1, ge=1)
    supersedes_document_id: str | None = None
    title: str = Field(min_length=1, max_length=400)
    authors: list[str] = Field(default_factory=list)
    organization: str | None = None
    published_at: str | None = None
    media_kind: DocumentMediaKind
    origin: DocumentOrigin
    origin_uri: str | None = None
    source_url: str | None = None
    doi: str | None = None
    license: str | None = None
    content_sha256: str
    size_bytes: int = Field(ge=0)
    document_artifact_id: str | None = None
    text_artifact_id: str | None = None
    extraction_artifact_id: str | None = None
    extraction_status: ExtractionStatus = ExtractionStatus.REGISTERED
    extraction_tool: str = ""
    extraction_version: str = ""
    extraction_warnings: list[str] = Field(default_factory=list)
    missing_pages: list[int] = Field(default_factory=list)
    page_count: int | None = Field(default=None, ge=1)
    section_count: int = 0
    passage_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    extracted_at: datetime | None = None

    @property
    def identity_label(self) -> str:
        return f"v{self.version} · {self.content_sha256[:12]}"


class SourcePassage(Record):
    """A citable chunk of extracted document text.

    ``char_start``/``char_end`` index the stored extracted-text artifact, so a
    quote can be re-verified against the exact bytes the extractor produced.
    """

    id: str
    document_id: str
    ordinal: int = Field(ge=0)
    kind: PassageKind
    page: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section_path: str = ""
    section_title: str | None = None
    heading_level: int | None = Field(default=None, ge=1, le=6)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text: str = Field(min_length=1)
    text_sha256: str
    quote_hash: str
    equation_label: str | None = None
    chunk_index: int = Field(default=0, ge=0)

    @field_validator("char_end")
    @classmethod
    def _char_order(cls, value: int, info: Any) -> int:
        start = info.data.get("char_start")
        if start is not None and value < start:
            raise ValueError("char_end must be >= char_start")
        return value

    @property
    def label(self) -> str:
        parts = []
        if self.page:
            parts.append(f"p.{self.page}")
        if self.section_path:
            parts.append(self.section_path.split(" > ")[-1])
        parts.append(f"#{self.ordinal}")
        return " · ".join(parts)


class ResearchClaim(Record):
    """One claim node in the research DAG (§9.3).

    Categories are never merged. ``EXPLICIT_SOURCE_CLAIM`` must carry at least
    one verified source location; inferred and decision claims must carry a
    derivation note and use ``basis_passage_ids`` instead of quoting the paper as
    if it stated them.
    """

    id: str
    project_id: str
    document_id: str | None = None
    parent_claim_id: str | None = None
    ordinal: int = 0
    claim_type: ClaimType
    title: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=8000)
    implementation_requirement: str = ""
    source_locations: list[SourceLocation] = Field(default_factory=list)
    basis_passage_ids: list[str] = Field(default_factory=list)
    expected_code_signals: list[str] = Field(default_factory=list)
    forbidden_code_signals: list[str] = Field(default_factory=list)
    target_paths: list[str] = Field(default_factory=list)
    target_symbols: list[str] = Field(default_factory=list)
    missing_details: list[str] = Field(default_factory=list)
    validation_methods: list[str] = Field(default_factory=list)
    derivation_note: str = ""
    created_by: ClaimAuthor = ClaimAuthor.USER
    created_by_detail: str = ""
    confidence: ClaimConfidence = ClaimConfidence.MEDIUM
    review_status: ClaimReviewStatus = ClaimReviewStatus.NEEDS_REVIEW
    review_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def is_reviewed(self) -> bool:
        return self.review_status != ClaimReviewStatus.NEEDS_REVIEW


class ClaimDependency(Record):
    """A directed ``claim_id requires depends_on_claim_id`` edge.

    Dependencies order implementation work. They are validated as a DAG; a cycle
    is reported as a modelling issue and never silently broken.
    """

    id: str
    claim_id: str
    depends_on_claim_id: str
    note: str = ""


class ResearchExtraction(Record):
    """One claim-extraction run over one immutable document version."""

    id: str
    document_id: str
    method: str
    algorithm_version: str
    extraction_key: str
    model_invocation_id: str | None = None
    claims_created: int = 0
    claims_preserved: int = 0
    claims_dropped: int = 0
    artifact_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ClaimRevision(Record):
    """Append-only record of a manual correction to a claim."""

    id: str
    claim_id: str
    revision: int = Field(ge=1)
    changed_at: datetime = Field(default_factory=utcnow)
    changed_by: str = "local-user"
    reason: str = ""
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class ClaimRiskFactor(Record):
    """An explicit, categorised reproduction risk recorded against a claim."""

    id: str
    claim_id: str
    category: RiskCategory
    detail: str = Field(min_length=1, max_length=2000)
    severity: RiskSeverity = RiskSeverity.WARNING
    created_at: datetime = Field(default_factory=utcnow)


class MappingEvidence(Record):
    """One exact code span offered as evidence for a claim mapping.

    ``excerpt`` is the verbatim text of ``path:start_line-end_line``; re-reading
    the pinned index must reproduce ``excerpt_sha256`` exactly.
    """

    id: str
    mapping_id: str
    snapshot_id: str
    file_id: str | None = None
    symbol_id: str | None = None
    qualified_name: str | None = None
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    relation: MappingEvidenceRelation = MappingEvidenceRelation.IMPLEMENTS
    strength: EvidenceStrength = EvidenceStrength.WEAK
    excerpt: str
    excerpt_sha256: str
    explanation: str = ""
    source: str = "AST"
    matched_signals: list[str] = Field(default_factory=list)

    @field_validator("end_line")
    @classmethod
    def _range_order(cls, value: int, info: Any) -> int:
        start = info.data.get("start_line")
        if start is not None and value < start:
            raise ValueError("end_line must be >= start_line")
        return value

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


class ClaimMapping(Record):
    """The current claim-to-code verdict for one claim and one snapshot."""

    id: str
    project_id: str
    claim_id: str
    snapshot_id: str
    status: ClaimMappingStatus
    status_reason: str = ""
    rationale: str = ""
    gaps: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    validation_method: str = ""
    rule_pack_version: str
    created_by: MappingAuthor = MappingAuthor.DETERMINISTIC
    model_invocation_id: str | None = None
    review_status: ClaimReviewStatus = ClaimReviewStatus.NEEDS_REVIEW
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ResearchMemo(Record):
    """A versioned research memo. Earlier versions are never rewritten."""

    id: str
    project_id: str
    document_id: str | None = None
    snapshot_id: str | None = None
    version: int = Field(default=1, ge=1)
    supersedes_memo_id: str | None = None
    title: str
    body_markdown: str
    artifact_id: str | None = None
    source_interpretation_sha256: str
    readiness: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------- task workflow
class Task(Record):
    """A user's requested objective and its current state pointer."""

    id: str
    project_id: str
    title: str = Field(min_length=1, max_length=300)
    task_type: TaskType
    goal: str = Field(min_length=1, max_length=20_000)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TaskScope(Record):
    """The repository slice a run may change.

    Paths are repository-relative POSIX paths. Absolute paths, ``..`` and null
    bytes are rejected here, and again by the workspace policy layer, which also
    resolves symlinks before any read or write.
    """

    allowed_paths: list[str] = Field(min_length=1)
    excluded_paths: list[str] = Field(default_factory=list)
    base_commit_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    include_working_tree_patch: bool = False

    @field_validator("allowed_paths", "excluded_paths")
    @classmethod
    def _path_shape(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            if not value or "\x00" in value:
                raise ValueError("scope paths must be non-empty and contain no null bytes")
            if value.startswith("/") or value.startswith("\\"):
                raise ValueError(f"scope paths must be repository-relative: {value!r}")
            parts = value.replace("\\", "/").split("/")
            if any(part in {"", "."} for part in parts):
                raise ValueError(f"scope path is not normalised: {value!r}")
            if any(part == ".." for part in parts):
                raise ValueError(f"scope paths must not escape the repository: {value!r}")
            cleaned.append("/".join(parts))
        return cleaned


class InvariantSpec(Record):
    """One ML-specific invariant template and its parameters.

    The template is rendered into a self-contained script and executed through
    the same command policy as any other check. Parameters are validated here,
    never interpolated into a shell.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    module: str = Field(min_length=1, max_length=300)
    callable: str | None = Field(default=None, max_length=200)
    input_shape: list[int] = Field(default_factory=lambda: [1, 4, 32], min_length=1, max_length=6)
    dtype: str = "float32"
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input_shape")
    @classmethod
    def _shape_positive(cls, values: list[int]) -> list[int]:
        if any(value < 1 or value > 1_000_000 for value in values):
            raise ValueError("input_shape dimensions must be between 1 and 1,000,000")
        return values

    @field_validator("dtype")
    @classmethod
    def _dtype_allowed(cls, value: str) -> str:
        allowed = {"float32", "float64", "float16", "bfloat16"}
        if value not in allowed:
            raise ValueError(f"dtype must be one of {sorted(allowed)}")
        return value


class CheckSpec(Record):
    """A declared verification check, pinned in the task contract.

    A command check is an argv array, never shell text. An invariant check names
    a built-in template whose generated script runs through the command policy.
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,80}$")
    gate: VerificationGate
    kind: CheckKind
    title: str = Field(min_length=1, max_length=300)
    required: bool = True
    argv: list[str] | None = None
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    criteria: list[str] = Field(default_factory=list)
    invariant: InvariantSpec | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("criteria")
    @classmethod
    def _criteria_shape(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.match(r"^AC[0-9]{1,4}$", value):
                raise ValueError(
                    "criteria references use the AC<n> ids derived from the contract's "
                    "acceptance criteria in order"
                )
        return values

    @model_validator(mode="after")
    def _kind_payload(self) -> "CheckSpec":
        if self.kind == CheckKind.COMMAND:
            if not self.argv or not all(isinstance(arg, str) and arg for arg in self.argv):
                raise ValueError("a COMMAND check requires a non-empty argv array")
            if self.invariant is not None:
                raise ValueError("a COMMAND check cannot also declare an invariant")
        elif self.kind == CheckKind.ML_INVARIANT:
            if self.invariant is None:
                raise ValueError("an ML_INVARIANT check requires an invariant specification")
            if not self.invariant.callable:
                raise ValueError(
                    "an ML_INVARIANT check requires invariant.callable; every template "
                    "calls the named target"
                )
            if self.argv:
                raise ValueError("an ML_INVARIANT check derives its command from the template")
        else:
            raise ValueError("CheckSpec supports COMMAND and ML_INVARIANT kinds only")
        return self


class TaskConstraints(Record):
    """Budgets and permissions a run may not exceed."""

    network_access: str = "DENY_BY_DEFAULT"
    dependency_install: str = "APPROVAL_REQUIRED"
    compute_profile: str = "CPU_SMOKE"
    max_runtime_seconds: int = Field(default=1800, ge=30, le=86_400)
    max_model_tokens: int = Field(default=50_000, ge=1_000, le=2_000_000)
    max_model_cost_usd: float = Field(default=0.0, ge=0.0)
    max_repair_attempts: int = Field(default=2, ge=0, le=10)
    max_steps: int = Field(default=30, ge=1, le=200)
    max_tool_calls: int = Field(default=120, ge=1, le=1000)
    max_output_bytes: int = Field(default=200_000, ge=1024, le=10_000_000)
    max_processes: int = Field(default=128, ge=8, le=4096)
    allow_file_deletion: bool = False
    # An unknown model price is not zero. Set this only when the user accepts
    # spend that the contract cannot bound.
    allow_unknown_cost: bool = False


class ResearchContextRef(Record):
    """Version-pinned research interpretation a task implements (MWB-014).

    Stored on the contract so a paper-to-code task can be reconstructed from the
    contract alone: exact document versions, claim graph and mapping artifact.
    """

    document_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    mapping_artifact_id: str | None = None
    mapping_sha256: str | None = None


class TaskContract(Record):
    """Immutable, hash-addressed authorization for one task objective."""

    id: str
    task_id: str
    project_id: str
    version: int = Field(ge=1)
    task_type: TaskType
    goal: str
    repository_id: str
    snapshot_id: str
    scope: TaskScope
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    acceptance_criteria: list[str] = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    research_context: ResearchContextRef | None = None
    verification_checks: list[CheckSpec] = Field(default_factory=list)
    required_approvals: list[ApprovalAction] = Field(
        default_factory=lambda: [ApprovalAction.WRITE_ACCESS, ApprovalAction.COMMAND_EXECUTION]
    )
    contract_version: str = "1.0"
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str = "local-user"
    created_at: datetime = Field(default_factory=utcnow)


class PlanStep(Record):
    description: str = Field(min_length=1, max_length=4000)
    files: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class TaskPlan(Record):
    """A reviewable implementation plan. It authorizes nothing by itself."""

    id: str
    task_id: str
    run_id: str
    project_id: str
    version: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=8000)
    steps: list[PlanStep] = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    estimated_steps: int = Field(default=1, ge=1, le=200)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_invocation_id: str | None = None
    artifact_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class WorkflowRun(Record):
    """One execution attempt for a task. Terminal runs are never reopened."""

    id: str
    task_id: str
    contract_id: str
    project_id: str
    repository_id: str
    snapshot_id: str
    state: WorkflowState = WorkflowState.CREATED
    previous_state: WorkflowState | None = None
    base_commit_sha: str
    plan_id: str | None = None
    workspace_id: str | None = None
    step_count: int = 0
    tool_call_count: int = 0
    model_tokens: int = 0
    model_cost_usd: float = 0.0
    pause_requested: bool = False
    cancel_requested: bool = False
    finish_summary: str | None = None
    limitations: list[str] = Field(default_factory=list)
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ApprovalRequest(Record):
    """A durable, scoped, single-use human authorization."""

    id: str
    run_id: str
    task_id: str
    project_id: str
    action: ApprovalAction
    status: ApprovalStatus = ApprovalStatus.PENDING
    resource_scope: dict[str, Any] = Field(default_factory=dict)
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(min_length=16, max_length=128)
    requested_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    consumed_at: datetime | None = None
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ExecutionWorkspace(Record):
    """A task-owned isolated Git worktree."""

    id: str
    run_id: str
    task_id: str
    project_id: str
    path: str
    base_commit_sha: str
    status: WorkspaceStatus = WorkspaceStatus.CREATING
    patch_artifact_id: str | None = None
    diff_sha256: str | None = None
    file_count: int = 0
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    finalized_at: datetime | None = None
    removed_at: datetime | None = None


class ToolCall(Record):
    """Audit record for one gateway invocation."""

    id: str
    run_id: str
    task_id: str
    project_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ToolCallStatus = ToolCallStatus.STARTED
    approval_id: str | None = None
    output_artifact_id: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DiffFile(Record):
    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    binary: bool = False


class TaskDiff(Record):
    """The complete candidate patch, including untracked files."""

    id: str
    run_id: str
    task_id: str
    project_id: str
    workspace_id: str
    base_commit_sha: str
    patch_artifact_id: str
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[DiffFile] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    scope_violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    original_checkout_unchanged: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class ReviewFinding(Record):
    severity: str = "INFO"
    code: str
    message: str
    path: str | None = None
    line: int | None = None


class TaskReview(Record):
    """Deterministic diff review for Phase 3. Not a verification decision."""

    id: str
    run_id: str
    task_id: str
    project_id: str
    diff_id: str
    scope_ok: bool
    secrets_found: bool
    tests_modified: list[str] = Field(default_factory=list)
    dependency_changes: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    report_artifact_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ------------------------------------------------------- verification (Phase 4)
class VerificationRun(Record):
    """One verification attempt over one workflow run.

    Attempts are append-only: re-running verification never rewrites the checks
    a report was based on.
    """

    id: str
    run_id: str
    task_id: str
    project_id: str
    attempt: int = Field(ge=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["RUNNING", "COMPLETED", "FAILED"] = "RUNNING"
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationFinding(Record):
    """One finding produced by a check or reviewer.

    ``source`` distinguishes deterministic evidence from model interpretation;
    a model finding can never be reported as a deterministic result.
    """

    severity: FindingSeverity = FindingSeverity.WARNING
    code: str = Field(min_length=3, max_length=80, pattern=r"^[A-Z][A-Z0-9_]{2,79}$")
    message: str = Field(min_length=1, max_length=4000)
    path: str | None = Field(default=None, max_length=1000)
    line: int | None = Field(default=None, ge=1)
    criterion_ref: str | None = Field(default=None, pattern=r"^AC[0-9]{1,4}$")
    source: str = Field(default="deterministic", max_length=40)
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationCheck(Record):
    """The recorded result of one check. Evidence, not a verdict by itself."""

    id: str
    verification_id: str
    run_id: str
    task_id: str
    project_id: str
    key: str
    gate: VerificationGate
    kind: CheckKind
    title: str
    method: VerificationMethod
    outcome: CheckOutcome
    required: bool = True
    criteria: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    duration_ms: int | None = None
    artifact_id: str | None = None
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    findings: list[VerificationFinding] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    schema_version: str = "1.0"

    @property
    def blocking(self) -> bool:
        return self.required and self.outcome in {
            CheckOutcome.FAILED,
            CheckOutcome.BLOCKED,
            CheckOutcome.INCONCLUSIVE,
            CheckOutcome.UNVERIFIED,
        }


class CriterionAssessment(Record):
    """Evidence status for one acceptance criterion, by its AC<n> id."""

    criterion_id: str = Field(pattern=r"^AC[0-9]{1,4}$")
    text: str
    status: CriterionStatus = CriterionStatus.UNVERIFIED
    check_keys: list[str] = Field(default_factory=list)
    note: str = ""


class VerificationReport(Record):
    """The decision and its evidence. Never rewritten after creation."""

    id: str
    verification_id: str
    run_id: str
    task_id: str
    project_id: str
    decision: TaskDecision
    summary: str
    criteria: list[CriterionAssessment] = Field(default_factory=list)
    check_ids: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    report_artifact_id: str | None = None
    json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_invocation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class EvidenceLink(Record):
    """One edge in the evidence graph.

    Kinds are a small closed vocabulary so a report can be verified mechanically:
    ``CRITERION``, ``CHECK``, ``ARTIFACT``, ``FILE_SPAN``, ``COMMAND``,
    ``MODEL_REVIEW``, ``HUMAN_NOTE``, ``CLAIM``.
    """

    id: str
    verification_id: str
    run_id: str
    report_id: str | None = None
    from_kind: str = Field(max_length=40)
    from_ref: str = Field(max_length=1000)
    relation: EvidenceRelation
    to_kind: str = Field(max_length=40)
    to_ref: str = Field(max_length=1000)
    note: str = ""
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utcnow)


class WorkerJob(Record):
    """Durable queue entry for the separate local worker process."""

    id: str
    run_id: str
    task_id: str
    project_id: str
    status: WorkerJobStatus = WorkerJobStatus.QUEUED
    attempts: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    enqueued_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "Record",
    "Project",
    "Repository",
    "RepositorySnapshot",
    "IndexedFile",
    "Symbol",
    "SymbolEdge",
    "IndexIssue",
    "Artifact",
    "WorkflowEvent",
    "ModelInvocation",
    "Citation",
    "QaRecord",
    "SourceLocation",
    "ResearchDocument",
    "SourcePassage",
    "ResearchClaim",
    "ClaimDependency",
    "ResearchExtraction",
    "ClaimRevision",
    "ClaimRiskFactor",
    "MappingEvidence",
    "ClaimMapping",
    "ResearchMemo",
    "Task",
    "TaskScope",
    "TaskConstraints",
    "TaskContract",
    "PlanStep",
    "TaskPlan",
    "WorkflowRun",
    "ApprovalRequest",
    "ExecutionWorkspace",
    "ToolCall",
    "DiffFile",
    "TaskDiff",
    "ReviewFinding",
    "TaskReview",
    "WorkerJob",
    "InvariantSpec",
    "CheckSpec",
    "VerificationRun",
    "VerificationFinding",
    "VerificationCheck",
    "CriterionAssessment",
    "VerificationReport",
    "EvidenceLink",
    "ArtifactKind",
    "EventType",
    "QaStatus",
    "ClaimMappingStatus",
    "ClaimType",
]

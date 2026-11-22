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
    CitationGranularity,
    CitationRelation,
    DisagreementKind,
    DisagreementStatus,
    ClaimAuthor,
    ClaimConfidence,
    ClaimExplicitness,
    ClaimKind,
    ClaimMappingStatus,
    ClaimReviewStatus,
    ClaimTargetKind,
    ClaimType,
    KIND_FOR_LEGACY_CLAIM_TYPE,
    LEGACY_CLAIM_TYPE_FOR_KIND,
    CriterionStatus,
    DocumentMediaKind,
    DocumentOrigin,
    EdgeConfidence,
    EdgeType,
    EventType,
    EvidenceRelation,
    EvidenceStrength,
    ExperimentDesign,
    ExperimentProfile,
    ExperimentRole,
    ExperimentRunStatus,
    ExperimentStatus,
    ExperimentVerdict,
    EvidenceStatus,
    FindingSeverity,
    ExtractionStatus,
    FileStatus,
    IssueKind,
    IssueSeverity,
    MappingAuthor,
    MappingEvidenceRelation,
    MemoryAuthor,
    MemoryKind,
    MemoryStatus,
    MetricAggregation,
    MetricDirection,
    MetricsSourceKind,
    ParseCoverage,
    PassageKind,
    PaperStatus,
    QaStatus,
    ProtocolComparison,
    ResourceKind,
    RetentionClass,
    RiskCategory,
    RiskSeverity,
    SnapshotKind,
    TaskDecision,
    ToolCapability,
    WorkspaceSessionState,
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

#: Version of the typed claim graph and the immutable discovery metadata that
#: grounds it. Recorded on every claim and every saved source-metadata artifact.
CLAIM_GRAPH_VERSION = "1.0"


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


class ClaimTarget(Record):
    """A typed resource a claim is about (audit §7.1 ``target_resources``)."""

    kind: ClaimTargetKind
    path: str = Field(min_length=1, max_length=1000)
    symbol: str | None = Field(default=None, max_length=300)

    @field_validator("path")
    @classmethod
    def _clean_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("target path must not be empty")
        return cleaned

    @property
    def label(self) -> str:
        return f"{self.kind.value}:{self.path}" + (f":{self.symbol}" if self.symbol else "")


class ResearchClaim(Record):
    """One typed node in the versioned research DAG (§9.3, audit §7.1).

    ``kind`` is the typed claim graph axis; ``claim_type`` is retained as the
    legacy compatibility axis and is always derived consistently from ``kind``
    (and vice versa). Paper claims carry a verbatim verified quote; inferred
    claims carry a derivation note and cite basis passages instead. Every node
    records how explicit it is, what it targets, what validates it, what remains
    uncertain and whether a code mapping has been checked at all.
    """

    id: str
    project_id: str
    document_id: str | None = None
    parent_claim_id: str | None = None
    ordinal: int = 0
    claim_type: ClaimType
    kind: ClaimKind = ClaimKind.PAPER_METHOD
    graph_version: str = CLAIM_GRAPH_VERSION
    title: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=8000)
    implementation_requirement: str = ""
    source_locations: list[SourceLocation] = Field(default_factory=list)
    basis_passage_ids: list[str] = Field(default_factory=list)
    explicitness: ClaimExplicitness = ClaimExplicitness.EXPLICIT
    target_resources: list[ClaimTarget] = Field(default_factory=list)
    validation_ids: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    mapping_status: ClaimMappingStatus = ClaimMappingStatus.NOT_CHECKED
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

    @model_validator(mode="before")
    @classmethod
    def _derive_typed_fields(cls, data: Any) -> Any:
        """Keep ``kind`` and ``claim_type`` consistent for old and new callers.

        A row or caller that supplies only the legacy ``claim_type`` gets the
        closest typed ``kind``; a caller that supplies only ``kind`` gets the
        legacy type older validation code expects. Supplying both with
        contradictory values is an error, never a silent rewrite.
        """
        if not isinstance(data, dict):
            return data
        raw_kind = data.get("kind")
        raw_type = data.get("claim_type")
        kind = ClaimKind(raw_kind) if raw_kind not in (None, "") else None
        legacy = ClaimType(raw_type) if raw_type not in (None, "") else None
        if kind is not None and legacy is not None:
            if LEGACY_CLAIM_TYPE_FOR_KIND[kind] is not legacy:
                raise ValueError(
                    f"kind {kind.value} is incompatible with claim_type {legacy.value}"
                )
        elif kind is None and legacy is None:
            # Let normal validation report the missing required claim_type.
            return data
        result = dict(data)
        if kind is None and legacy is not None:
            kind = KIND_FOR_LEGACY_CLAIM_TYPE[legacy]
            result["kind"] = kind.value
        elif legacy is None and kind is not None:
            result["claim_type"] = LEGACY_CLAIM_TYPE_FOR_KIND[kind].value
        if data.get("explicitness") in (None, ""):
            if kind is ClaimKind.INFERRED_REQUIREMENT:
                explicitness = ClaimExplicitness.INFERRED
            elif kind in (ClaimKind.PAPER_METHOD, ClaimKind.PAPER_RESULT):
                explicitness = ClaimExplicitness.EXPLICIT
            elif data.get("basis_passage_ids") or data.get("derivation_note"):
                explicitness = ClaimExplicitness.INFERRED
            else:
                explicitness = ClaimExplicitness.EXPLICIT
            result["explicitness"] = explicitness.value
        return result

    def is_reviewed(self) -> bool:
        return self.review_status != ClaimReviewStatus.NEEDS_REVIEW


class SourceMetadata(Record):
    """Immutable, content-addressed provenance for a discovered source.

    The record is a small index over an artifact holding the exact provider
    payload. There is deliberately no update path: retrieval time, URL/DOI,
    paper version and content hash describe what was actually observed.
    ``status`` and the correction/retraction fields are recorded facts about the
    source version; a non-ACTIVE status downgrades readiness inputs but never
    fabricates a claim verdict.
    """

    id: str
    project_id: str
    document_key: str
    provider: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=500)
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    doi: str | None = None
    url: str | None = None
    published: str | None = None
    paper_version: str | None = None
    retrieval_url: str | None = None
    metadata_sha256: str = Field(min_length=64, max_length=64)
    retrieved_at: datetime = Field(default_factory=utcnow)
    artifact_id: str = Field(min_length=1)
    protocol_version: str = CLAIM_GRAPH_VERSION
    status: PaperStatus = PaperStatus.ACTIVE
    version_label: str | None = None
    correction_of: str | None = None
    corrected_by: str | None = None
    retraction_date: str | None = None
    retraction_reason: str | None = None
    relation_note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_alerting(self) -> bool:
        """Whether this source status warrants a version alert."""
        return self.status is not PaperStatus.ACTIVE


class ClaimCitation(Record):
    """One version-bound, granular citation of a verified source quote.

    A citation is only written after its quote has been verified verbatim against
    the pinned passage by the claim service. ``quote_hash`` is the hash of the
    whitespace-normalised quote and ``document_version`` pins the immutable
    document version the passage belongs to. ``supersedes_citation_id`` links a
    replacement citation to the older one without rewriting it.
    """

    id: str
    project_id: str
    claim_id: str
    document_id: str
    document_version: int = Field(ge=1)
    source_metadata_id: str | None = None
    passage_id: str
    quote: str = Field(min_length=1)
    quote_hash: str = Field(min_length=64, max_length=64)
    page: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: str = ""
    equation: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    granularity: CitationGranularity = CitationGranularity.PASSAGE
    relation: CitationRelation = CitationRelation.SUPPORTS
    confidence: ClaimConfidence = ClaimConfidence.HIGH
    verified_at: datetime | None = None
    verified_by: str = "local-user"
    supersedes_citation_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("char_end")
    @classmethod
    def _char_order(cls, value: int | None, info: Any) -> int | None:
        start = info.data.get("char_start")
        if value is not None and start is not None and value < start:
            raise ValueError("char_end must be >= char_start")
        return value


class EvidenceDisagreement(Record):
    """A typed disagreement between evidence axes, with resolution state.

    At least two evidence anchors (verified quote, mapping id/evidence span,
    experiment run id) must be named before a disagreement is recorded. A
    resolution appends a new record instead of rewriting an earlier one, so the
    original OPEN record and its history are preserved.
    """

    id: str
    project_id: str
    claim_id: str
    document_id: str | None = None
    passage_id: str | None = None
    mapping_id: str | None = None
    run_id: str | None = None
    kind: DisagreementKind
    detail: str = Field(min_length=1, max_length=4000)
    status: DisagreementStatus = DisagreementStatus.OPEN
    resolution: str = ""
    created_by: str = "local-user"
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolved_by: str | None = None


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
    #: Durable Engineering Workspace scope. When set, ``resource_ids`` must be
    #: attachments of that workspace, so a task cannot silently reach a raw path.
    workspace_id: str | None = None
    resource_ids: list[str] = Field(default_factory=list)

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
    #: Closed allowlist of tool capabilities. Empty means a legacy contract
    #: where the existing approval/path/command policy remains authoritative.
    capabilities: list[str] = Field(default_factory=list)
    #: Execution boundary requested for this task. Isolated profiles block
    #: until a verified runtime backend is available.
    execution_trust: str = "LOCAL_TRUSTED"
    allow_file_deletion: bool = False
    # An unknown model price is not zero. Set this only when the user accepts
    # spend that the contract cannot bound.
    allow_unknown_cost: bool = False

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        allowed = {item.value for item in ToolCapability}
        invalid = sorted(set(value) - allowed)
        if invalid:
            raise ValueError(f"unknown tool capabilities: {', '.join(invalid)}")
        return list(dict.fromkeys(value))


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
    #: Gate F: when true, verification refuses to accept the run without a
    #: comparable experiment attached to the task. Evidence is required, not a
    #: particular experimental result.
    require_experiment: bool = False
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


# --------------------------------------------------- experiments (Phase 5)
METRIC_KEY_RE = r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$"

#: Protocol fields that may be declared as an intended treatment. A key is a
#: dotted path into :class:`ExperimentProtocol` (or ``command``), so a user can
#: say exactly which dimension the intervention changed.
TREATMENT_DIMENSION_PATHS = (
    "command",
    "repository.commit",
    "repository.patch",
    "dataset.id",
    "dataset.version",
    "dataset.fingerprint",
    "splits",
    "preprocessing.code_hash",
    "preprocessing.description",
    "evaluation.script_path",
    "evaluation.script_hash",
    "model.identity",
    "model.checkpoint_hash",
    "config.hash",
    "config.resolved",
    "environment.lockfile_path",
    "environment.lockfile_hash",
    "environment.image_digest",
    "environment.python_version",
    "environment.framework_versions",
    "hardware.profile",
    "hardware.device",
    "hardware.cpu_count",
    "hardware.memory_mb",
    "determinism.seeded",
    "determinism.notes",
    "batch_size",
    "sequence_length",
    "stopping_rule",
    "runtime_limit_seconds",
)


class MetricSpec(Record):
    """One declared metric: direction, unit and aggregation are part of the
    definition, because a comparison that infers them after seeing the numbers
    is not a pre-registered comparison."""

    key: str = Field(pattern=METRIC_KEY_RE)
    direction: MetricDirection
    unit: str = Field(default="", max_length=80)
    aggregation: MetricAggregation = MetricAggregation.LAST
    primary: bool = False
    description: str = Field(default="", max_length=2000)


class ExperimentDataset(Record):
    id: str = Field(default="", max_length=500)
    version: str = Field(default="", max_length=200)
    fingerprint: str = Field(default="", max_length=200)
    fingerprint_method: str = Field(default="", max_length=200)
    path: str | None = Field(default=None, max_length=2000)


class ExperimentPreprocessing(Record):
    code_hash: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)


class ExperimentEvaluation(Record):
    script_path: str | None = Field(default=None, max_length=2000)
    script_hash: str = Field(default="", max_length=200)


class ExperimentModelRef(Record):
    identity: str = Field(default="", max_length=500)
    checkpoint_hash: str = Field(default="", max_length=200)


class ExperimentConfigRef(Record):
    resolved: dict[str, Any] = Field(default_factory=dict)
    hash: str = Field(default="", max_length=200)


class ExperimentEnvironment(Record):
    lockfile_path: str | None = Field(default=None, max_length=2000)
    lockfile_hash: str = Field(default="", max_length=200)
    image_digest: str = Field(default="", max_length=200)
    python_version: str = Field(default="", max_length=80)
    framework_versions: dict[str, str] = Field(default_factory=dict)


class ExperimentHardware(Record):
    profile: str = Field(default="", max_length=200)
    device: str = Field(default="", max_length=80)
    cpu_count: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=1)


class ExperimentDeterminism(Record):
    seeded: bool = True
    notes: str = Field(default="", max_length=2000)


class ExperimentProtocol(Record):
    """Everything that must match (or be declared as the treatment) before two
    experiment arms may be compared. Fields left empty are *unknown*, and a
    comparison reports them as unknown rather than treating them as equal."""

    dataset: ExperimentDataset = Field(default_factory=ExperimentDataset)
    splits: dict[str, str] = Field(default_factory=dict)
    preprocessing: ExperimentPreprocessing = Field(default_factory=ExperimentPreprocessing)
    evaluation: ExperimentEvaluation = Field(default_factory=ExperimentEvaluation)
    model: ExperimentModelRef = Field(default_factory=ExperimentModelRef)
    config: ExperimentConfigRef = Field(default_factory=ExperimentConfigRef)
    environment: ExperimentEnvironment = Field(default_factory=ExperimentEnvironment)
    hardware: ExperimentHardware = Field(default_factory=ExperimentHardware)
    determinism: ExperimentDeterminism = Field(default_factory=ExperimentDeterminism)
    batch_size: int | None = Field(default=None, ge=1)
    sequence_length: int | None = Field(default=None, ge=1)
    stopping_rule: str = Field(default="", max_length=1000)
    runtime_limit_seconds: int | None = Field(default=None, ge=1)


class MetricsSource(Record):
    """Where a run's metrics come from. ``path`` is workspace-relative and is
    validated at execution time."""

    kind: MetricsSourceKind = MetricsSourceKind.STDOUT_JSON
    path: str | None = Field(default=None, max_length=1000)


class ExperimentBudget(Record):
    max_runs: int = Field(default=10, ge=1, le=100)
    max_total_runtime_seconds: int = Field(default=3600, ge=1, le=7 * 86_400)
    max_artifact_bytes: int = Field(default=5_000_000, ge=1024, le=200_000_000)
    max_metric_observations: int = Field(default=10_000, ge=1, le=1_000_000)


class ExperimentDefinition(Record):
    """An immutable, approval-gated experiment declaration.

    The definition is the pre-registration: role, command, seeds, metrics,
    directions, protocol and budgets. It is hashed in full (including the hash
    of the protocol), so any later edit produces a different definition that
    requires its own approval.
    """

    id: str
    project_id: str
    repository_id: str
    snapshot_id: str
    base_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    task_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    hypothesis: str = Field(min_length=1, max_length=8000)
    role: ExperimentRole
    profile: ExperimentProfile
    command: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    seeds: list[int] = Field(min_length=1, max_length=100)
    metrics: list[MetricSpec] = Field(min_length=1)
    metrics_source: MetricsSource = Field(default_factory=MetricsSource)
    protocol: ExperimentProtocol = Field(default_factory=ExperimentProtocol)
    treatment_dimensions: list[str] = Field(
        default_factory=lambda: ["command", "repository.commit", "repository.patch"]
    )
    allowed_variation: list[str] = Field(default_factory=list)
    budget: ExperimentBudget = Field(default_factory=ExperimentBudget)
    min_seeds: int = Field(default=2, ge=1, le=100)
    non_inferiority_margin: float | None = Field(default=None, ge=0.0)
    status: ExperimentStatus = ExperimentStatus.DRAFT
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revoked_reason: str | None = Field(default=None, max_length=2000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str = "local-user"
    created_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seeds")
    @classmethod
    def _unique_seeds(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("seeds must be unique")
        if any(seed < 0 for seed in value):
            raise ValueError("seeds must be non-negative")
        return value

    @field_validator("metrics")
    @classmethod
    def _metrics_shape(cls, value: list[MetricSpec]) -> list[MetricSpec]:
        keys = [metric.key for metric in value]
        if len(set(keys)) != len(keys):
            raise ValueError("metric keys must be unique")
        primaries = [metric for metric in value if metric.primary]
        if len(primaries) != 1:
            raise ValueError("exactly one metric must be primary")
        return value

    @property
    def primary_metric(self) -> MetricSpec:
        return next(metric for metric in self.metrics if metric.primary)


class ExperimentManifest(Record):
    """Immutable pre-run provenance record.

    It is built and hashed *before* the command starts, so a run cannot rewrite
    what it was asked to do after seeing the result. Outcomes live on
    :class:`ExperimentRun`, never inside the manifest.
    """

    id: str
    experiment_id: str
    project_id: str
    snapshot_id: str
    role: ExperimentRole
    seed: int = Field(ge=0)
    attempt: int = Field(ge=1)
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_id: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    snapshot_kind: str = Field(max_length=40)
    includes_worktree_patch: bool = False
    patch_hash: str | None = Field(default=None, max_length=200)
    protocol: ExperimentProtocol = Field(default_factory=ExperimentProtocol)
    command: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    verified_inputs: dict[str, Any] = Field(default_factory=dict)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = "1.0"
    created_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class ExperimentRun(Record):
    """One execution attempt of one seed. Append-only after completion."""

    id: str
    experiment_id: str
    project_id: str
    task_id: str | None = None
    manifest_id: str | None = None
    seed: int = Field(ge=0)
    attempt: int = Field(ge=1)
    status: ExperimentRunStatus = ExperimentRunStatus.PENDING
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    blocked: bool = False
    cancelled: bool = False
    duration_ms: int | None = None
    cpu_user_ms: int | None = None
    cpu_system_ms: int | None = None
    max_rss_kb: int | None = None
    output_artifact_id: str | None = None
    metrics_artifact_id: str | None = None
    metrics_ingested: bool = False
    observation_count: int = 0
    metrics_error: str | None = None
    error: str | None = None
    termination_reason: str = ""
    workspace_kept: bool = False
    workspace_path: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status == ExperimentRunStatus.COMPLETED and self.metrics_ingested


class MetricObservation(Record):
    """One raw metric value. Replicate aggregation happens at comparison time
    from these rows, so the raw evidence is never discarded."""

    id: str
    experiment_id: str
    run_id: str
    project_id: str
    metric_key: str = Field(pattern=METRIC_KEY_RE)
    value: float
    sample_index: int = Field(default=0, ge=0)
    step: float | None = None
    source: str = Field(default="STDOUT_JSON", max_length=40)
    recorded_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("metric observations must be finite numbers")
        return value


class ProtocolFinding(Record):
    """One protocol-field difference between two experiment arms."""

    path: str = Field(min_length=1, max_length=200)
    classification: ProtocolComparison
    baseline: Any = None
    candidate: Any = None
    note: str = Field(default="", max_length=2000)


class MetricComparison(Record):
    """Per-metric statistics from one fair comparison.

    Optional numbers are ``None`` when the data cannot support them (for
    example a p-value with one observation). Nothing is fabricated.
    """

    key: str = Field(pattern=METRIC_KEY_RE)
    direction: MetricDirection
    unit: str = ""
    aggregation: MetricAggregation = MetricAggregation.LAST
    primary: bool = False
    baseline_values: list[float] = Field(default_factory=list)
    candidate_values: list[float] = Field(default_factory=list)
    baseline_mean: float | None = None
    candidate_mean: float | None = None
    baseline_sd: float | None = None
    candidate_sd: float | None = None
    baseline_median: float | None = None
    candidate_median: float | None = None
    difference: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    t_statistic: float | None = None
    degrees_of_freedom: float | None = None
    p_value: float | None = None
    p_value_adjusted: float | None = None
    effect_size: float | None = None
    effect_size_ci_low: float | None = None
    effect_size_ci_high: float | None = None
    method: str = Field(default="", max_length=80)
    favors: str = Field(default="", max_length=20)
    within_margin: bool | None = None
    notes: list[str] = Field(default_factory=list)


class ExperimentComparison(Record):
    """A persisted, hash-addressed comparison of two experiment definitions."""

    id: str
    project_id: str
    task_id: str | None = None
    baseline_experiment_id: str
    candidate_experiment_id: str
    baseline_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: ExperimentVerdict
    design: ExperimentDesign
    comparable: bool
    findings: list[ProtocolFinding] = Field(default_factory=list)
    metrics: list[MetricComparison] = Field(default_factory=list)
    baseline_run_ids: list[str] = Field(default_factory=list)
    candidate_run_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    report_artifact_id: str | None = None
    json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = "1.0"
    created_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class MemorySource(Record):
    """Typed provenance for one memory item."""

    kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,40}$")
    ref: str = Field(min_length=1, max_length=1000)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=2000)


class ProjectMemory(Record):
    """Durable project knowledge (§15).

    Model-authored items start ``UNCONFIRMED``; only a user decision confirms
    one. Commit-scoped items go ``STALE`` when the pinned revision is no longer
    the repository's current commit; a failure is never generalised into a rule.
    """

    id: str
    project_id: str
    kind: MemoryKind
    status: MemoryStatus = MemoryStatus.UNCONFIRMED
    author_kind: MemoryAuthor = MemoryAuthor.USER
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=8000)
    scope: str = Field(default="project", max_length=500)
    repository_id: str | None = None
    snapshot_id: str | None = None
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    confidence: str = Field(default="MEDIUM", pattern=r"^(LOW|MEDIUM|HIGH)$")
    sources: list[MemorySource] = Field(default_factory=list)
    supersedes_id: str | None = None
    last_validated_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class Workspace(Record):
    """Durable multi-resource scope (WP4). Distinct from a task-owned worktree."""

    id: str
    project_id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    title: str = Field(default="", max_length=200)
    description: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str = "USER"


class ResourceAttachment(Record):
    """A typed link from a Workspace to a repository, dataset, service, etc."""

    id: str
    workspace_id: str
    kind: ResourceKind
    name: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=1000)
    revision: str | None = None
    hash_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    details: dict[str, Any] = Field(default_factory=dict)
    attached_at: datetime = Field(default_factory=utcnow)


class EnvironmentSpec(Record):
    """Declarative toolchain/lockfile description attached to a workspace."""

    id: str
    workspace_id: str
    name: str = Field(min_length=1, max_length=120)
    ecosystem: str = Field(min_length=1, max_length=60)
    descriptor_path: str | None = None
    lockfile_path: str | None = None
    runtime: str = "LOCAL_TRUSTED"
    toolchain: dict[str, Any] = Field(default_factory=dict)
    hash_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utcnow)


class WorkspaceSession(Record):
    """Client-side record of a long-running process in a workspace."""

    id: str
    workspace_id: str
    runtime_id: str | None = None
    command: list[str] = Field(default_factory=list)
    state: WorkspaceSessionState = WorkspaceSessionState.OPEN
    last_seq: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(Record):
    """A verifiable statement linked to a workspace resource and artifact."""

    id: str
    workspace_id: str
    resource_id: str | None = None
    claim: str = Field(min_length=1, max_length=4000)
    artifact_id: str | None = None
    hash_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED
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
    "ClaimTarget",
    "ResearchClaim",
    "SourceMetadata",
    "ClaimCitation",
    "EvidenceDisagreement",
    "CLAIM_GRAPH_VERSION",
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
    "Workspace",
    "ResourceAttachment",
    "EnvironmentSpec",
    "WorkspaceSession",
    "EvidenceItem",
    "InvariantSpec",
    "CheckSpec",
    "VerificationRun",
    "VerificationFinding",
    "VerificationCheck",
    "CriterionAssessment",
    "VerificationReport",
    "EvidenceLink",
    "MetricSpec",
    "ExperimentDataset",
    "ExperimentPreprocessing",
    "ExperimentEvaluation",
    "ExperimentModelRef",
    "ExperimentConfigRef",
    "ExperimentEnvironment",
    "ExperimentHardware",
    "ExperimentDeterminism",
    "ExperimentProtocol",
    "MetricsSource",
    "ExperimentBudget",
    "ExperimentDefinition",
    "ExperimentManifest",
    "ExperimentRun",
    "MetricObservation",
    "ProtocolFinding",
    "MetricComparison",
    "ExperimentComparison",
    "MemorySource",
    "ProjectMemory",
    "ArtifactKind",
    "EventType",
    "QaStatus",
    "ClaimMappingStatus",
    "ClaimType",
]

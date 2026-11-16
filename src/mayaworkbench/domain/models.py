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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EdgeConfidence,
    EdgeType,
    EventType,
    FileStatus,
    IssueKind,
    IssueSeverity,
    ParseCoverage,
    QaStatus,
    RetentionClass,
    SnapshotKind,
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
    "ArtifactKind",
    "EventType",
    "QaStatus",
]

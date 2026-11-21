"""Computed research contracts.

The persisted entities live in :mod:`mayaworkbench.domain.models`. This module
holds derived views and model-output schemas, which are versioned evidence in
their own right: a memo cites a readiness assessment by score version, and a
claim-extraction payload is validated before any of it is stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ClaimConfidence,
    ClaimType,
    DocumentMediaKind,
    DocumentOrigin,
    RiskCategory,
    RiskSeverity,
)
from mayaworkbench.domain.models import CLAIM_GRAPH_VERSION, MappingEvidence, ResearchClaim


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- ingestion
class ExtractionReport(Contract):
    """Persisted as a ``EXTRACTION_REPORT`` artifact for every document."""

    protocol_version: str
    document_id: str
    media_kind: DocumentMediaKind
    origin: DocumentOrigin
    tool: str
    tool_version: str
    content_sha256: str
    text_sha256: str
    char_count: int
    page_count: int | None = None
    missing_pages: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    section_count: int = 0
    passage_counts: dict[str, int] = Field(default_factory=dict)
    metadata_sources: dict[str, str] = Field(default_factory=dict)
    body_start: int = 0
    created_at: datetime = Field(default_factory=utcnow)


@dataclass
class IngestResult:
    document: ResearchDocument
    passages: list[SourcePassage]
    created: bool
    warnings: list[str] = field(default_factory=list)
    report: ExtractionReport | None = None


# ----------------------------------------------------------------- claims
class ClaimExtractionSummary(Contract):
    document_id: str
    method: str
    algorithm_version: str
    claims: list[ResearchClaim] = Field(default_factory=list)
    claims_created: int = 0
    claims_preserved: int = 0
    claims_dropped: int = 0
    warnings: list[str] = Field(default_factory=list)
    model_used: bool = False
    model_invocation_id: str | None = None
    extraction_id: str | None = None
    artifact_id: str | None = None
    eligible_sentences: int = 0


class ModelClaimRisk(Contract):
    category: RiskCategory
    detail: str = Field(min_length=1, max_length=2000)
    severity: RiskSeverity = RiskSeverity.WARNING


class ModelClaim(Contract):
    """One claim as returned by a model. Validated, then given stable IDs."""

    ref: str = Field(min_length=1, max_length=40)
    claim_type: Literal["EXPLICIT_SOURCE_CLAIM", "INFERRED_REQUIREMENT"]
    title: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=8000)
    implementation_requirement: str = ""
    passage_ids: list[str] = Field(default_factory=list)
    quote: str = ""
    derivation_note: str = ""
    expected_code_signals: list[str] = Field(default_factory=list)
    forbidden_code_signals: list[str] = Field(default_factory=list)
    missing_details: list[str] = Field(default_factory=list)
    validation_methods: list[str] = Field(default_factory=list)
    risk_factors: list[ModelClaimRisk] = Field(default_factory=list)
    confidence: ClaimConfidence = ClaimConfidence.MEDIUM
    parent_ref: str | None = None
    depends_on_refs: list[str] = Field(default_factory=list)


class ClaimExtractionPayload(Contract):
    claims: list[ModelClaim] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# -------------------------------------------------------------------- DAG
class DagIssue(Contract):
    code: Literal[
        "MISSING_DEPENDENCY",
        "SELF_DEPENDENCY",
        "DEPENDENCY_CYCLE",
        "MISSING_PARENT",
        "PARENT_CYCLE",
        "CROSS_DOCUMENT_DEPENDENCY",
        "CROSS_DOCUMENT_PARENT",
        "DUPLICATE_CLAIM_ID",
        "CROSS_PROJECT_CLAIM",
    ]
    severity: Literal["ERROR", "WARNING"]
    claim_ids: list[str] = Field(default_factory=list)
    detail: str


class ClaimDagValidation(Contract):
    protocol_version: str
    project_id: str
    document_id: str | None = None
    graph_version: str = CLAIM_GRAPH_VERSION
    claim_count: int = 0
    edge_count: int = 0
    claim_kind_counts: dict[str, int] = Field(default_factory=dict)
    issues: list[DagIssue] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    topological_order: list[str] = Field(default_factory=list)
    unorderable: list[str] = Field(default_factory=list)
    depth: dict[str, int] = Field(default_factory=dict)
    roots: list[str] = Field(default_factory=list)
    leaves: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)

    @property
    def errors(self) -> list[DagIssue]:
        return [issue for issue in self.issues if issue.severity == "ERROR"]

    @property
    def warnings(self) -> list[DagIssue]:
        return [issue for issue in self.issues if issue.severity == "WARNING"]


# ----------------------------------------------------------------- mapping
class VerifiedEvidence(Contract):
    evidence: MappingEvidence
    verified: bool
    verification: str


class ClaimMatrixRow(Contract):
    claim_id: str
    document_id: str | None = None
    ordinal: int = 0
    title: str
    claim_type: ClaimType
    review_status: str
    mapping_status: str | None = None
    status_reason: str = ""
    rationale: str = ""
    created_by: str | None = None
    rule_pack_version: str | None = None
    validation_method: str = ""
    gaps: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    evidence: list[VerifiedEvidence] = Field(default_factory=list)

    @property
    def evidence_verified(self) -> bool:
        return bool(self.evidence) and all(item.verified for item in self.evidence)


class ClaimToCodeMatrix(Contract):
    protocol_version: str
    project_id: str
    snapshot_id: str
    snapshot_identity: str
    repository_name: str
    rule_pack_version: str
    generated_at: datetime = Field(default_factory=utcnow)
    rows: list[ClaimMatrixRow] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    claims_total: int = 0
    unmapped_claim_ids: list[str] = Field(default_factory=list)
    rejected_claim_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def mapped_rows(self) -> list[ClaimMatrixRow]:
        return [row for row in self.rows if row.mapping_status is not None]


# --------------------------------------------------------------- readiness
class ReadinessFactor(Contract):
    key: str
    label: str
    weight: float = Field(ge=0.0, le=1.0)
    state: Literal["KNOWN", "UNKNOWN"]
    risk: float | None = Field(default=None, ge=0.0, le=1.0)
    explanation: str = ""
    evidence: list[str] = Field(default_factory=list)


class ReadinessAssessment(Contract):
    """Reproduction readiness / risk, never a probability.

    ``overall_risk`` is a weighted heuristic over *known* factors. ``None`` means
    the recorded information is insufficient; it is never silently treated as
    zero risk.
    """

    score_version: str
    weights: dict[str, float] = Field(default_factory=dict)
    factors: list[ReadinessFactor] = Field(default_factory=list)
    overall_risk: float | None = Field(default=None, ge=0.0, le=1.0)
    label: Literal["LOW_RISK", "MODERATE_RISK", "HIGH_RISK", "INSUFFICIENT_INFORMATION"]
    blockers: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    claim_counts: dict[str, int] = Field(default_factory=dict)
    mapping_counts: dict[str, int] = Field(default_factory=dict)
    known_weight: float = 0.0
    is_probability: bool = False

    @property
    def known_factors(self) -> list[ReadinessFactor]:
        return [factor for factor in self.factors if factor.state == "KNOWN"]


# -------------------------------------------------------------------- memo
@dataclass
class MemoBuild:
    memo: Any
    markdown: str
    artifact_id: str
    readiness: ReadinessAssessment
    dag: ClaimDagValidation
    matrix: ClaimToCodeMatrix | None = None
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------ model schemas
CLAIM_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims", "notes"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "ref",
                    "claim_type",
                    "title",
                    "statement",
                    "implementation_requirement",
                    "passage_ids",
                    "quote",
                    "derivation_note",
                    "expected_code_signals",
                    "forbidden_code_signals",
                    "missing_details",
                    "validation_methods",
                    "risk_factors",
                    "confidence",
                    "parent_ref",
                    "depends_on_refs",
                ],
                "properties": {
                    "ref": {"type": "string"},
                    "claim_type": {
                        "type": "string",
                        "enum": ["EXPLICIT_SOURCE_CLAIM", "INFERRED_REQUIREMENT"],
                    },
                    "title": {"type": "string"},
                    "statement": {"type": "string"},
                    "implementation_requirement": {"type": "string"},
                    "passage_ids": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                    "derivation_note": {"type": "string"},
                    "expected_code_signals": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "forbidden_code_signals": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing_details": {"type": "array", "items": {"type": "string"}},
                    "validation_methods": {"type": "array", "items": {"type": "string"}},
                    "risk_factors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["category", "detail", "severity"],
                            "properties": {
                                "category": {
                                    "type": "string",
                                    "enum": [category.value for category in RiskCategory],
                                },
                                "detail": {"type": "string"},
                                "severity": {
                                    "type": "string",
                                    "enum": [severity.value for severity in RiskSeverity],
                                },
                            },
                        },
                    },
                    "confidence": {
                        "type": "string",
                        "enum": [confidence.value for confidence in ClaimConfidence],
                    },
                    "parent_ref": {"type": "string"},
                    "depends_on_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}


__all__ = [
    "Contract",
    "ExtractionReport",
    "IngestResult",
    "ClaimExtractionSummary",
    "ModelClaim",
    "ModelClaimRisk",
    "ClaimExtractionPayload",
    "CLAIM_EXTRACTION_SCHEMA",
    "DagIssue",
    "ClaimDagValidation",
    "VerifiedEvidence",
    "ClaimMatrixRow",
    "ClaimToCodeMatrix",
    "ReadinessFactor",
    "ReadinessAssessment",
    "MemoBuild",
]

"""Mapping contracts and the model-adjudication schema.

The adjudication schema only lets a model choose from the spans it was shown:
evidence is addressed by ``span_ref`` (``S1``), never by an arbitrary path. The
service then re-reads those spans from the pinned index and recomputes signal
coverage before accepting any status.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.domain.enums import ClaimMappingStatus

Confidence = Literal["HIGH", "MEDIUM", "LOW"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelEvidence(Contract):
    span_ref: str = Field(min_length=1, max_length=20)
    relation: Literal["IMPLEMENTS", "SUPPORTS", "CONTRADICTS", "TEST_COVERS"] = "SUPPORTS"
    explanation: str = Field(min_length=1, max_length=2000)
    matched_signals: list[str] = Field(default_factory=list)


class MappingAdjudicationPayload(Contract):
    status: Literal[
        "PRESENT",
        "PARTIAL",
        "ABSENT",
        "UNCERTAIN",
        "CONFLICTING",
    ]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence: list[ModelEvidence] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    confidence: Confidence = "MEDIUM"


MAPPING_ADJUDICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "rationale", "evidence", "gaps", "counter_evidence", "confidence"],
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                ClaimMappingStatus.PRESENT.value,
                ClaimMappingStatus.PARTIAL.value,
                ClaimMappingStatus.ABSENT.value,
                ClaimMappingStatus.UNCERTAIN.value,
                ClaimMappingStatus.CONFLICTING.value,
            ],
        },
        "rationale": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["span_ref", "relation", "explanation", "matched_signals"],
                "properties": {
                    "span_ref": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": [
                            "IMPLEMENTS",
                            "SUPPORTS",
                            "CONTRADICTS",
                            "TEST_COVERS",
                        ],
                    },
                    "explanation": {"type": "string"},
                    "matched_signals": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "counter_evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
    },
}


__all__ = [
    "Contract",
    "ModelEvidence",
    "MappingAdjudicationPayload",
    "MAPPING_ADJUDICATION_SCHEMA",
]

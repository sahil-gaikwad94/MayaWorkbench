"""Reproduction readiness / risk assessment (§9.6).

This is a transparent weighted heuristic, not a probability:

* each factor is reported separately, with its weight, state, risk value and the
  evidence that produced it;
* a factor with no recorded information is ``UNKNOWN`` — it never contributes
  zero risk;
* if too little is known, the overall result is ``INSUFFICIENT_INFORMATION``
  rather than a confident-looking number;
* a low risk score is never proof that reproduction will succeed.

The score version is recorded on every result so a memo can cite the model that
produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mayaworkbench.domain.enums import (
    ClaimMappingStatus,
    ClaimReviewStatus,
    ClaimType,
    DocumentMediaKind,
    RiskCategory,
    RiskSeverity,
)
from mayaworkbench.domain.models import (
    ClaimMapping,
    ClaimRiskFactor,
    MappingEvidence,
    RepositorySnapshot,
    ResearchClaim,
    ResearchDocument,
)
from mayaworkbench.research.models import ClaimDagValidation, ReadinessAssessment, ReadinessFactor
from mayaworkbench.version import READINESS_SCORE_VERSION

WEIGHTS: dict[str, float] = {
    "paper_detail": 0.22,
    "code_mapping": 0.30,
    "data_access": 0.14,
    "compute": 0.10,
    "licensing": 0.08,
    "evaluation": 0.16,
}

FACTOR_LABELS = {
    "paper_detail": "Missing paper detail",
    "code_mapping": "Code mapping uncertainty",
    "data_access": "Data access",
    "compute": "Compute availability",
    "licensing": "Licensing and reuse",
    "evaluation": "Evaluation gaps",
}

MAPPING_RISK = {
    ClaimMappingStatus.PRESENT: 0.0,
    ClaimMappingStatus.PARTIAL: 0.45,
    ClaimMappingStatus.UNCERTAIN: 0.75,
    ClaimMappingStatus.ABSENT: 0.95,
    ClaimMappingStatus.CONFLICTING: 1.0,
}

SEVERITY_RISK = {
    RiskSeverity.INFO: 0.15,
    RiskSeverity.WARNING: 0.6,
    RiskSeverity.BLOCKER: 1.0,
}

PERMISSIVE_LICENSE_MARKERS = (
    "mit",
    "apache",
    "bsd",
    "cc-by",
    "cc by",
    "creative commons attribution",
    "public domain",
    "mpl",
    "isc",
)
RESTRICTIVE_LICENSE_MARKERS = (
    "non-commercial",
    "noncommercial",
    "by-nc",
    "by nc",
    "nc-nd",
    "nc-sa",
    "no derivatives",
    "research only",
    "academic use",
    "proprietary",
    "restricted",
    "internal use",
    "no redistribution",
)

ASSUMPTIONS = [
    "Mapping statuses describe the inspected snapshot only; they are not claims about other revisions.",
    "PRESENT means the required behaviour appears implemented; it does not mean it was experimentally validated.",
    "ABSENT describes the inspected scope and parser coverage; it is not proof the behaviour is nowhere.",
    "The readiness score is a weighted heuristic over recorded facts. It is not a probability.",
    "An overall value is reported only when at least 75% of the factor weight is known.",
    "A low risk value is not evidence that reproduction will succeed.",
]

MINIMUM_KNOWN_WEIGHT = 0.75


@dataclass
class ReadinessInput:
    project_id: str
    claims: list[ResearchClaim] = field(default_factory=list)
    mappings: list[ClaimMapping] = field(default_factory=list)
    evidence: list[MappingEvidence] = field(default_factory=list)
    risk_factors: list[ClaimRiskFactor] = field(default_factory=list)
    documents: list[ResearchDocument] = field(default_factory=list)
    snapshot: RepositorySnapshot | None = None
    dag: ClaimDagValidation | None = None
    index_issue_counts: dict[str, int] = field(default_factory=dict)
    coverage_counts: dict[str, int] = field(default_factory=dict)
    unverified_evidence: int = 0


def assess_readiness(data: ReadinessInput) -> ReadinessAssessment:
    claims = data.claims
    claim_count = len(claims)
    mapping_by_claim = {mapping.claim_id: mapping for mapping in data.mappings}
    factors: list[ReadinessFactor] = [
        _paper_detail(data, claims),
        _code_mapping(data, claims, mapping_by_claim),
        _risk_factor_factor(
            "data_access",
            data,
            RiskCategory.DATA_ACCESS,
            guidance=(
                "No data requirements or access constraints are recorded. Record them with "
                "`maya research claim add ... --risk DATA_ACCESS:WARNING:<detail>`."
            ),
        ),
        _risk_factor_factor(
            "compute",
            data,
            RiskCategory.COMPUTE,
            guidance=(
                "No compute requirements are recorded. Record them with "
                "`--risk COMPUTE:WARNING:<detail>`."
            ),
        ),
        _licensing(data),
        _evaluation(data, claims, mapping_by_claim),
    ]

    known = [factor for factor in factors if factor.state == "KNOWN" and factor.risk is not None]
    known_weight = round(sum(factor.weight for factor in known), 6)
    overall: float | None = None
    if known_weight >= MINIMUM_KNOWN_WEIGHT:
        overall = round(
            sum(factor.weight * float(factor.risk) for factor in known) / known_weight, 4
        )
        if overall <= 0.25:
            label = "LOW_RISK"
        elif overall <= 0.55:
            label = "MODERATE_RISK"
        else:
            label = "HIGH_RISK"
    else:
        label = "INSUFFICIENT_INFORMATION"

    blockers: list[str] = []
    for mapping in data.mappings:
        claim = next((item for item in claims if item.id == mapping.claim_id), None)
        if mapping.status is ClaimMappingStatus.CONFLICTING:
            blockers.append(
                f"CONFLICTING mapping for claim {mapping.claim_id} ({claim.title if claim else '?'}): "
                f"{mapping.status_reason[:300]}"
            )
        elif (
            mapping.status is ClaimMappingStatus.ABSENT
            and claim is not None
            and claim.review_status is ClaimReviewStatus.ACCEPTED
        ):
            blockers.append(
                f"Accepted claim {claim.id} ({claim.title}) has no implementation evidence."
            )
    for factor in data.risk_factors:
        if factor.severity is RiskSeverity.BLOCKER:
            blockers.append(
                f"BLOCKER · {factor.category.value} · {factor.detail[:300]}"
            )
    if data.dag is not None:
        for issue in data.dag.errors:
            blockers.append(f"DAG {issue.code}: {issue.detail[:300]}")
    if data.unverified_evidence:
        blockers.append(
            f"{data.unverified_evidence} mapping evidence span(s) no longer match the pinned index."
        )

    limitations: list[str] = []
    for document in data.documents:
        if document.media_kind is DocumentMediaKind.PDF:
            limitations.append(
                f"{document.title}: PDF text extraction relies on the text layer"
                + (
                    f"; {len(document.missing_pages)} page(s) produced no text"
                    if document.missing_pages
                    else ""
                )
                + "."
            )
        limitations.extend(
            f"{document.title}: {warning}" for warning in document.extraction_warnings[:6]
        )
    if data.snapshot is not None:
        if data.snapshot.includes_worktree_patch:
            limitations.append(
                "The mapped snapshot includes an uncommitted patch "
                f"({(data.snapshot.patch_hash or '')[:12]}); it is not a committed revision."
            )
        lexical = data.coverage_counts.get("LEXICAL_ONLY", 0)
        partial = data.coverage_counts.get("PARTIAL", 0)
        if lexical or partial:
            limitations.append(
                f"Snapshot coverage: {partial} partially parsed and {lexical} lexically-only "
                "file(s); structural evidence for those files is weaker."
            )
        if data.index_issue_counts:
            top = sorted(data.index_issue_counts.items(), key=lambda item: -item[1])[:5]
            limitations.append(
                "Index issues: " + ", ".join(f"{kind}×{count}" for kind, count in top) + "."
            )
    else:
        limitations.append(
            "No repository snapshot was assessed; code mapping and evaluation factors are unknown."
        )
    if data.unverified_evidence:
        limitations.append(
            "Some stored mapping evidence failed verification and must not be presented as "
            "evidence."
        )

    claim_counts: dict[str, int] = {}
    for claim in claims:
        claim_counts[claim.claim_type.value] = claim_counts.get(claim.claim_type.value, 0) + 1
    mapping_counts: dict[str, int] = {}
    for mapping in data.mappings:
        mapping_counts[mapping.status.value] = mapping_counts.get(mapping.status.value, 0) + 1
    if claim_count:
        mapping_counts["UNMAPPED"] = max(0, claim_count - len(data.mappings))

    return ReadinessAssessment(
        score_version=READINESS_SCORE_VERSION,
        weights=dict(WEIGHTS),
        factors=factors,
        overall_risk=overall,
        label=label,
        blockers=_dedupe(blockers),
        assumptions=list(ASSUMPTIONS),
        limitations=_dedupe(limitations),
        claim_counts=claim_counts,
        mapping_counts=mapping_counts,
        known_weight=known_weight,
        is_probability=False,
    )


# ------------------------------------------------------------------- factors
def _paper_detail(data: ReadinessInput, claims: list[ResearchClaim]) -> ReadinessFactor:
    active = [
        claim for claim in claims if claim.review_status is not ClaimReviewStatus.REJECTED
    ]
    if not active:
        return ReadinessFactor(
            key="paper_detail",
            label=FACTOR_LABELS["paper_detail"],
            weight=WEIGHTS["paper_detail"],
            state="UNKNOWN",
            risk=None,
            explanation="No active claim exists, so paper completeness cannot be assessed.",
        )
    missing = [claim for claim in active if claim.missing_details]
    unsourced = [
        claim
        for claim in active
        if claim.claim_type is ClaimType.EXPLICIT_SOURCE_CLAIM and not claim.source_locations
    ]
    risk = 0.6 * (len(missing) / len(active)) + 0.4 * (len(unsourced) / len(active))
    evidence = [f"claim {claim.id}: {claim.missing_details[0][:160]}" for claim in missing[:5]]
    evidence.extend(
        f"claim {claim.id} is an explicit source claim without a source location"
        for claim in unsourced[:5]
    )
    warnings = sum(len(document.extraction_warnings) for document in data.documents)
    missing_pages = sum(len(document.missing_pages) for document in data.documents)
    if warnings or missing_pages:
        risk = min(1.0, risk + 0.1)
        evidence.append(
            f"{warnings} extraction warning(s) and {missing_pages} page(s) without text."
        )
    return ReadinessFactor(
        key="paper_detail",
        label=FACTOR_LABELS["paper_detail"],
        weight=WEIGHTS["paper_detail"],
        state="KNOWN",
        risk=round(min(1.0, risk), 4),
        explanation=(
            f"{len(missing)} of {len(active)} active claim(s) record missing paper detail and "
            f"{len(unsourced)} explicit claim(s) lack a source location."
        ),
        evidence=evidence,
    )


def _code_mapping(
    data: ReadinessInput,
    claims: list[ResearchClaim],
    mapping_by_claim: dict[str, ClaimMapping],
) -> ReadinessFactor:
    if not data.mappings:
        return ReadinessFactor(
            key="code_mapping",
            label=FACTOR_LABELS["code_mapping"],
            weight=WEIGHTS["code_mapping"],
            state="UNKNOWN",
            risk=None,
            explanation=(
                "No claim-to-code mapping exists for this snapshot; mapping uncertainty cannot be "
                "assessed. Run `maya map`."
            ),
        )
    active = [
        claim for claim in claims if claim.review_status is not ClaimReviewStatus.REJECTED
    ]
    accepted = [
        claim for claim in active if claim.review_status is ClaimReviewStatus.ACCEPTED
    ]
    pool = accepted or active
    if not pool:
        return ReadinessFactor(
            key="code_mapping",
            label=FACTOR_LABELS["code_mapping"],
            weight=WEIGHTS["code_mapping"],
            state="UNKNOWN",
            risk=None,
            explanation="Every claim was rejected, so mapping risk is undefined.",
        )
    values: list[float] = []
    evidence: list[str] = []
    for claim in pool:
        mapping = mapping_by_claim.get(claim.id)
        if mapping is None:
            values.append(0.75)
            evidence.append(f"claim {claim.id} has no mapping")
            continue
        if mapping.status is ClaimMappingStatus.NOT_APPLICABLE:
            continue
        values.append(MAPPING_RISK.get(mapping.status, 0.75))
        if mapping.status is not ClaimMappingStatus.PRESENT:
            evidence.append(
                f"claim {claim.id}: {mapping.status.value} — {mapping.status_reason[:160]}"
            )
    if not values:
        return ReadinessFactor(
            key="code_mapping",
            label=FACTOR_LABELS["code_mapping"],
            weight=WEIGHTS["code_mapping"],
            state="UNKNOWN",
            risk=None,
            explanation="Every mapping is NOT_APPLICABLE, so mapping risk is undefined.",
        )
    return ReadinessFactor(
        key="code_mapping",
        label=FACTOR_LABELS["code_mapping"],
        weight=WEIGHTS["code_mapping"],
        state="KNOWN",
        risk=round(sum(values) / len(values), 4),
        explanation=(
            f"Weighted mean over {len(values)} claim(s) "
            + ("accepted by review" if accepted else "(no claim is accepted yet, so all active claims count)")
            + ". PRESENT=0, PARTIAL=0.45, UNCERTAIN=0.75, ABSENT=0.95, CONFLICTING=1, "
            "unmapped=0.75."
        ),
        evidence=evidence[:8],
    )


def _risk_factor_factor(
    key: str,
    data: ReadinessInput,
    category: RiskCategory,
    *,
    guidance: str,
) -> ReadinessFactor:
    relevant = [factor for factor in data.risk_factors if factor.category is category]
    if not relevant:
        return ReadinessFactor(
            key=key,
            label=FACTOR_LABELS[key],
            weight=WEIGHTS[key],
            state="UNKNOWN",
            risk=None,
            explanation=guidance,
        )
    risk = max(SEVERITY_RISK.get(factor.severity, 0.6) for factor in relevant)
    return ReadinessFactor(
        key=key,
        label=FACTOR_LABELS[key],
        weight=WEIGHTS[key],
        state="KNOWN",
        risk=round(risk, 4),
        explanation=(
            f"{len(relevant)} recorded {category.value} risk factor(s); the worst severity sets "
            "the factor risk."
        ),
        evidence=[
            f"{factor.severity.value}: {factor.detail[:200]}" for factor in relevant[:6]
        ],
    )


def _licensing(data: ReadinessInput) -> ReadinessFactor:
    licenses = [
        document.license for document in data.documents if document.license
    ]
    factors = [
        factor for factor in data.risk_factors if factor.category is RiskCategory.LICENSING
    ]
    if not licenses and not factors:
        return ReadinessFactor(
            key="licensing",
            label=FACTOR_LABELS["licensing"],
            weight=WEIGHTS["licensing"],
            state="UNKNOWN",
            risk=None,
            explanation=(
                "No licence is stated in the supplied document and no licensing risk was "
                "recorded. Reuse terms cannot be assessed."
            ),
        )
    risk = 0.0
    evidence: list[str] = []
    for licence in licenses:
        lowered = licence.casefold()
        if any(marker in lowered for marker in RESTRICTIVE_LICENSE_MARKERS):
            risk = max(risk, 0.6)
            evidence.append(f"restrictive licence: {licence[:160]}")
        elif any(marker in lowered for marker in PERMISSIVE_LICENSE_MARKERS):
            risk = max(risk, 0.1)
            evidence.append(f"permissive licence: {licence[:160]}")
        else:
            risk = max(risk, 0.35)
            evidence.append(f"unclassified licence text: {licence[:160]}")
    for factor in factors:
        risk = max(risk, SEVERITY_RISK.get(factor.severity, 0.6))
        evidence.append(f"{factor.severity.value}: {factor.detail[:160]}")
    return ReadinessFactor(
        key="licensing",
        label=FACTOR_LABELS["licensing"],
        weight=WEIGHTS["licensing"],
        state="KNOWN",
        risk=round(risk, 4),
        explanation="Licence text and recorded licensing risks, worst case wins.",
        evidence=evidence[:6],
    )


def _evaluation(
    data: ReadinessInput,
    claims: list[ResearchClaim],
    mapping_by_claim: dict[str, ClaimMapping],
) -> ReadinessFactor:
    active = [
        claim for claim in claims if claim.review_status is not ClaimReviewStatus.REJECTED
    ]
    factors = [
        factor for factor in data.risk_factors if factor.category is RiskCategory.EVALUATION
    ]
    test_covers = {
        evidence.mapping_id
        for evidence in data.evidence
        if evidence.relation.value == "TEST_COVERS"
    }
    covered = [
        claim
        for claim in active
        if claim.validation_methods
        or (
            claim.id in mapping_by_claim
            and mapping_by_claim[claim.id].id in test_covers
        )
    ]
    if not active:
        return ReadinessFactor(
            key="evaluation",
            label=FACTOR_LABELS["evaluation"],
            weight=WEIGHTS["evaluation"],
            state="UNKNOWN",
            risk=None,
            explanation="No claims exist, so evaluation coverage cannot be assessed.",
        )
    if not covered and not factors:
        return ReadinessFactor(
            key="evaluation",
            label=FACTOR_LABELS["evaluation"],
            weight=WEIGHTS["evaluation"],
            state="UNKNOWN",
            risk=None,
            explanation=(
                "No claim declares a validation method and no evaluation gap is recorded, so "
                "evaluation readiness is unknown rather than zero-risk."
            ),
        )
    risk = 1.0 - (len(covered) / len(active))
    for factor in factors:
        risk = max(risk, SEVERITY_RISK.get(factor.severity, 0.6))
    return ReadinessFactor(
        key="evaluation",
        label=FACTOR_LABELS["evaluation"],
        weight=WEIGHTS["evaluation"],
        state="KNOWN",
        risk=round(min(1.0, risk), 4),
        explanation=(
            f"{len(covered)} of {len(active)} active claim(s) have a declared validation method "
            "or test evidence."
        ),
        evidence=[f"claim {claim.id}: {claim.validation_methods[0][:160]}" for claim in covered[:5]],
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = ["ReadinessInput", "assess_readiness", "WEIGHTS", "MAPPING_RISK"]

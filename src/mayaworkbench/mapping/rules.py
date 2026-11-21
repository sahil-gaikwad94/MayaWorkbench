"""Deterministic mapping rules (rule pack ``mapping/v1``).

The rules exist to make one promise mechanical: **``PRESENT`` is never inferred
from a name match.** The requirements are:

1. the claim declares machine-checkable acceptance signals;
2. every declared signal occurs in *executable code*;
3. at least one of those occurrences is inside a definition-level span with full
   parser coverage;
4. no forbidden signal occurs anywhere in the candidate evidence.

Anything less is reported as ``PARTIAL``, ``ABSENT`` or ``UNCERTAIN`` with the
reason stated. Derived signals (taken from claim text) can never produce
``PRESENT``; they can only produce ``PARTIAL`` or ``UNCERTAIN``.

Automatic rules never emit ``NOT_APPLICABLE`` or ``CONFLICTING`` without code
evidence: ``CONFLICTING`` requires a forbidden signal to actually appear, and
``NOT_APPLICABLE`` is a human decision recorded through a manual override.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mayaworkbench.domain.enums import ClaimMappingStatus, ClaimReviewStatus, EvidenceStrength
from mayaworkbench.domain.models import ResearchClaim
from mayaworkbench.mapping.evidence import SpanAnalysis

DEFAULT_VALIDATION_METHOD = (
    "manual review of the cited spans against the requirement, plus the claim's "
    "validation methods when declared"
)


@dataclass
class MappingDecision:
    status: ClaimMappingStatus
    reason: str
    rationale: str = ""
    gaps: list[str] = field(default_factory=list)
    counter_evidence: list[str] = field(default_factory=list)
    validation_method: str = DEFAULT_VALIDATION_METHOD


def decide_mapping(
    *,
    claim: ResearchClaim,
    declared_signals: list[str],
    derived_signals: list[str],
    analyses: list[SpanAnalysis],
    coverage_limited: bool,
    truncated: bool,
) -> MappingDecision:
    declared = list(dict.fromkeys(signal for signal in declared_signals if signal.strip()))
    derived = list(dict.fromkeys(signal for signal in derived_signals if signal.strip()))
    matched = {
        signal
        for analysis in analyses
        for signal in analysis.matched_signals
    }
    matched_in_code = {
        signal
        for analysis in analyses
        for signal in analysis.code_signals
    }
    declared_matched = [signal for signal in declared if signal in matched]
    declared_in_code = [signal for signal in declared if signal in matched_in_code]
    unmatched = [signal for signal in declared if signal not in matched]
    forbidden_hits = [analysis for analysis in analyses if analysis.forbidden_signals]
    definitional_code = [
        analysis
        for analysis in analyses
        if analysis.definitional and analysis.code_signals
    ]
    searched = sorted({analysis.candidate.path for analysis in analyses})
    validation_method = (
        "; ".join(claim.validation_methods)
        if claim.validation_methods
        else DEFAULT_VALIDATION_METHOD
    )
    gaps: list[str] = []
    counter: list[str] = []

    if forbidden_hits:
        counter = [
            f"{analysis.location}: forbidden signal(s) "
            + ", ".join(f"`{signal}`" for signal in analysis.forbidden_signals)
            for analysis in forbidden_hits[:6]
        ]
        return MappingDecision(
            status=ClaimMappingStatus.CONFLICTING,
            reason=(
                "Candidate code contains signal(s) the claim forbids, so the observed "
                "implementation appears to contradict the requirement."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            gaps=[
                "A human needs to decide whether the forbidden signal applies to this claim's "
                "scope or whether the claim itself is wrong."
            ],
            counter_evidence=counter,
            validation_method=validation_method,
        )

    if claim.review_status is ClaimReviewStatus.REJECTED:
        return MappingDecision(
            status=ClaimMappingStatus.UNCERTAIN,
            reason=(
                "The claim was rejected during review; its mapping is reported for completeness "
                "only and must not be treated as a requirement."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            validation_method=validation_method,
        )

    if declared:
        if len(declared_in_code) == len(declared):
            # Signals scattered across unrelated definitions are a name-match,
            # not evidence of one coherent implementation. PRESENT requires all
            # declared signals to co-occur in the same executable definition.
            coherent = [
                analysis
                for analysis in definitional_code
                if set(declared).issubset(set(analysis.code_signals))
            ]
            strong_coherent = [
                analysis for analysis in coherent if analysis.strength is EvidenceStrength.STRONG
            ]
            if strong_coherent:
                return MappingDecision(
                    status=ClaimMappingStatus.PRESENT,
                    reason=(
                        "All declared acceptance signals occur together in executable code inside "
                        "a definition-level span with full parser coverage."
                    ),
                    rationale=_rationale(
                        analyses, declared, declared_matched, strong=strong_coherent
                    ),
                    validation_method=validation_method,
                )
            if coherent:
                gaps.append(
                    "All declared signals occur in one executable definition, but that definition "
                    "does not have full parser coverage at definition level; the mapping is "
                    "therefore only partial."
                )
                return MappingDecision(
                    status=ClaimMappingStatus.PARTIAL,
                    reason=(
                        "All declared signals occur together in executable code, but not in a "
                        "definition-level span with full parser coverage."
                    ),
                    rationale=_rationale(analyses, declared, declared_matched),
                    gaps=gaps,
                    validation_method=validation_method,
                )
            gaps.append(
                "Declared signals occur in executable code, but only in different spans that do "
                "not demonstrate one coherent implementation. A human should confirm the "
                "relationship before this is treated as present."
            )
            return MappingDecision(
                status=ClaimMappingStatus.PARTIAL,
                reason=(
                    "Declared acceptance signals occur in executable code but are not co-located "
                    "in one definition-level span, so a coherent implementation is not established."
                ),
                rationale=_rationale(analyses, declared, declared_matched),
                gaps=gaps,
                validation_method=validation_method,
            )
        if declared_in_code:
            gaps.extend(
                f"Declared signal `{signal}` was not found in executable code."
                for signal in unmatched
            )
            return MappingDecision(
                status=ClaimMappingStatus.PARTIAL,
                reason=(
                    f"{len(declared_in_code)} of {len(declared)} declared signals occur in "
                    "executable code; the rest were not found."
                ),
                rationale=_rationale(analyses, declared, declared_matched),
                gaps=gaps,
                validation_method=validation_method,
            )
        if declared_matched:
            gaps.append(
                "Declared signals appear only in comments, docstrings or string literals, which "
                "is not implementation evidence."
            )
            return MappingDecision(
                status=ClaimMappingStatus.UNCERTAIN,
                reason=(
                    "Declared signals appear only in prose (comments, docstrings or strings), so "
                    "the implementation behaviour cannot be established."
                ),
                rationale=_rationale(analyses, declared, declared_matched),
                gaps=gaps,
                validation_method=validation_method,
            )
        gaps.extend(
            f"Declared signal `{signal}` was not found anywhere in the inspected snapshot."
            for signal in unmatched
        )
        if coverage_limited:
            gaps.append(
                "Some candidate files have partial or lexical-only parser coverage, so absence "
                "cannot be asserted."
            )
            return MappingDecision(
                status=ClaimMappingStatus.UNCERTAIN,
                reason=(
                    "No declared signal was found, but parser coverage in candidate files is "
                    "incomplete, so absence is not established."
                ),
                rationale=_rationale(analyses, declared, declared_matched),
                gaps=gaps,
                validation_method=validation_method,
            )
        if truncated:
            gaps.append("Candidate discovery hit its span cap before exhausting the search.")
            return MappingDecision(
                status=ClaimMappingStatus.UNCERTAIN,
                reason="No declared signal was found, but candidate discovery was truncated.",
                rationale=_rationale(analyses, declared, declared_matched),
                gaps=gaps,
                validation_method=validation_method,
            )
        return MappingDecision(
            status=ClaimMappingStatus.ABSENT,
            reason=(
                "None of the declared acceptance signals occur anywhere in the inspected "
                "snapshot, across symbol, identifier and exact-signal searches."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            gaps=gaps,
            validation_method=validation_method,
        )

    # No declared acceptance signals: the best deterministic result is PARTIAL.
    gaps.append(
        "The claim declares no acceptance signals, so a deterministic PRESENT verdict is not "
        "allowed; add signals with `maya research claim edit --expect ...` or adjudicate with "
        "--model."
    )
    if not derived:
        gaps.append("No identifier or signal could be derived from the claim text.")
        return MappingDecision(
            status=ClaimMappingStatus.UNCERTAIN,
            reason=(
                "The claim declares no acceptance signals and no code-shaped terms could be "
                "derived from its text, so no meaningful code search was possible."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            gaps=gaps,
            validation_method=validation_method,
        )
    if coverage_limited or truncated:
        gaps.append(
            "Parser coverage is incomplete or discovery was truncated, so absence cannot be "
            "asserted."
        )
        return MappingDecision(
            status=ClaimMappingStatus.UNCERTAIN,
            reason=(
                "No unambiguous implementation evidence was found, and coverage is incomplete."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            gaps=gaps,
            validation_method=validation_method,
        )
    definitional_with_two = [
        analysis
        for analysis in definitional_code
        if len(set(analysis.code_signals)) >= 2
    ]
    if definitional_with_two:
        gaps.append(
            "Only derived signals matched; require a reviewer or model to confirm the semantic "
            "correspondence."
        )
        return MappingDecision(
            status=ClaimMappingStatus.PARTIAL,
            reason=(
                "Derived signals from the claim text appear in definition-level code, but the "
                "claim declares no acceptance signals, so PRESENT is not permitted."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            gaps=gaps,
            validation_method=validation_method,
        )
    if matched:
        gaps.append(
            "Matches are weak or occur only in prose; no definition-level executable evidence "
            "was found."
        )
        return MappingDecision(
            status=ClaimMappingStatus.UNCERTAIN,
            reason=(
                "Only weak or prose-only matches were found for terms derived from the claim."
            ),
            rationale=_rationale(analyses, declared, declared_matched),
            gaps=gaps,
            validation_method=validation_method,
        )
    gaps.append(
        "Derived signals were not found; this is not proof of absence because the claim was "
        "never given machine-checkable acceptance signals."
    )
    return MappingDecision(
        status=ClaimMappingStatus.ABSENT,
        reason=(
            "Neither declared nor derived signals appear in the inspected snapshot; because no "
            "acceptance signals were declared, treat this as a strong hint rather than proof."
        ),
        rationale=_rationale(analyses, declared, declared_matched),
        gaps=gaps,
        validation_method=validation_method,
    )


def apply_absence_policy(
    decision: MappingDecision,
    *,
    coverage_limited: bool,
    truncated: bool,
) -> MappingDecision:
    """Downgrade every ``ABSENT`` verdict when the index coverage is incomplete.

    ``ABSENT`` is only permitted after a complete search (audit §7.1). A model
    may propose it, or deterministic rules may reach it before a late coverage
    signal is noticed; in either case a partial index turns the result into
    ``UNCERTAIN`` with the reason recorded rather than silently kept.
    """
    if decision.status is not ClaimMappingStatus.ABSENT:
        return decision
    if not (coverage_limited or truncated):
        return decision
    cause = (
        "parser coverage in candidate files is incomplete"
        if coverage_limited
        else "candidate discovery was truncated"
    )
    gaps = list(decision.gaps)
    gaps.append(f"ABSENT was downgraded to UNCERTAIN because {cause}.")
    return MappingDecision(
        status=ClaimMappingStatus.UNCERTAIN,
        reason=(
            f"No declared signal was found, but {cause}, so absence is not established."
        ),
        rationale=decision.rationale,
        gaps=gaps,
        counter_evidence=list(decision.counter_evidence),
        validation_method=decision.validation_method,
    )


def _rationale(
    analyses: list[SpanAnalysis],
    declared: list[str],
    declared_matched: list[str],
    *,
    strong: list[SpanAnalysis] | None = None,
) -> str:
    parts: list[str] = [
        f"Inspected {len(analyses)} candidate span(s) across "
        f"{len({analysis.candidate.path for analysis in analyses})} file(s)."
    ]
    if declared:
        parts.append(
            f"Declared signals: {len(declared)}; matched in code: {len(declared_matched)}."
        )
        unmatched = [signal for signal in declared if signal not in declared_matched]
        if unmatched:
            parts.append("Unmatched declared signals: " + ", ".join(unmatched) + ".")
    else:
        parts.append("No declared acceptance signals; only text-derived terms were searched.")
    if strong:
        parts.append(
            "Definition-level evidence: "
            + "; ".join(
                f"{analysis.candidate.qualified_name or analysis.candidate.path}"
                f" ({analysis.location})"
                for analysis in strong[:3]
            )
            + "."
        )
    elif analyses:
        best = analyses[0]
        parts.append(
            f"Strongest candidate: {best.location} ({best.strength.value})."
        )
    return " ".join(parts)


__all__ = [
    "MappingDecision",
    "decide_mapping",
    "apply_absence_policy",
    "DEFAULT_VALIDATION_METHOD",
]

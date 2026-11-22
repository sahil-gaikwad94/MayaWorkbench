"""Backend-computed, four-tier reproducibility report (spec R2I-E1).

This module is a *pure function* of the records a run already persists. It
never calls a model, reads a file, queries a database or invents a timestamp:
``generated_at`` is an explicit parameter (defaulting to :func:`clock.utcnow`).

The four tiers are deliberately independent and each fails closed:

``METHOD_CODE_FOUND``
    Did a verified, in-scope claim mapping point at code implementing the method?
``METHOD_IMPLEMENTED``
    Was a non-empty patch captured and reproduced byte-for-byte?
``METHOD_VALIDATED``
    Did required functional checks pass *and* every required obligation get
    concrete evidence?
``RESULT_REPRODUCED``
    Did a fair, usable experiment comparison reproduce the reported result?

``NOT_CHECKED`` is never success. Integrity gates (test integrity, mutation
probes) and unverified criteria can only lower a tier, never raise it. The
evidence vocabulary matches ``verification.report.evidence_classification``:
checks are deterministically checked evidence, comparisons are experimentally
observed evidence, model summaries are not evidence at all.

Every tier cites the exact records that produced it. Inputs are checked for
run/task/project scope before any tier is computed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.domain.enums import (
    CheckKind,
    CheckOutcome,
    ClaimMappingStatus,
    ClaimReviewStatus,
)
from mayaworkbench.domain.models import (
    ClaimMapping,
    ExperimentComparison,
    ResearchClaim,
    Task,
    TaskDiff,
    VerificationCheck,
    VerificationReport,
    WorkflowRun,
)
from mayaworkbench.research.obligations import (
    MUTATION_SCAN_NODE_BUDGET,
    ValidationObligation,
    as_dict as _as_dict,
    enum_value as _enum_value,
    evaluate_primary_metric as _primary_metric,
    fairness_violations as _fairness_violations,
    imported_evidence as _imported_notes,
    is_deterministic_check as _is_deterministic_check,
    iter_items as _iter_items,
    malformed_notes as _malformed_notes,
    margin_evidence as _margin_evidence,
    obligations_for_claims,
    coverage as obligation_coverage,
    verdict_statistics_support as _verdict_statistics_support,
)

SCHEMA_VERSION = "r2i-reproduction/v1"

TIER_NAMES: tuple[str, ...] = (
    "METHOD_CODE_FOUND",
    "METHOD_IMPLEMENTED",
    "METHOD_VALIDATED",
    "RESULT_REPRODUCED",
)

TierLevel = Literal["YES", "PARTIAL", "NO", "NOT_CHECKED"]

FUNCTIONAL_KINDS = {CheckKind.COMMAND, CheckKind.ML_INVARIANT}

#: Check keys accepted as a reproduced-patch check. This is an explicit
#: allowlist: a key merely *containing* "reproduc" (for example
#: ``patch.not_reproducible``) is never accepted.
PATCH_REPRODUCIBLE_KEYS = frozenset(
    {
        "patch.reproducible",
        "patch.reproduced",
        "patch.reproducibility",
        "patch.integrity",
    }
)

CANONICAL_LIMITATIONS: tuple[str, ...] = (
    "Method invariant probes are property checks over sampled inputs, not proofs "
    "of correctness; a passing probe never establishes semantic equivalence.",
    "Hash-verified bundles prove that bytes were transferred intact, not that the "
    "reported scientific result is true.",
)

_STRICT_STATUSES = {"FAILED", "PARTIAL", "UNVERIFIED"}

#: An integrity check that did not positively pass supplies no integrity
#: evidence. This mirrors ``VerificationCheck.blocking`` and adds the
#: absence-of-evidence outcomes (SKIPPED/NOT_APPLICABLE) which are not success.
_INTEGRITY_DOWNGRADE_OUTCOMES = frozenset(
    {
        "FAILED",
        "PARTIAL",
        "UNVERIFIED",
        "BLOCKED",
        "INCONCLUSIVE",
        "SKIPPED",
        "NOT_APPLICABLE",
    }
)

#: Accepted mutation/bypass code vocabulary. A much wider set than the original
#: three tokens: a harness that survived a mutant or was weakened must be named.
_MUTATION_TOKENS = (
    "MUTATION_SURVIVED",
    "MUTANT_SURVIVED",
    "MUTATION_NOT_KILLED",
    "EVAL_HARNESS_WEAKENED",
    "CRITICAL_TEST_BYPASS",
    "BYPASS_DETECTED",
    "SURVIVED",
)

_SEVERE_TOKENS = (
    "integrity",
    "mutation",
    "mutant",
    "survived",
    "not_killed",
    "harness_weakened",
    "bypass",
)
_EXPLICIT_RULE_STATUSES = frozenset({"PRESENT", "PARTIAL", "CONFLICTING", "ABSENT"})
_NEGATIVE_RULE_STATUSES = frozenset({"UNCERTAIN", "NOT_CHECKED", "ABSENT", "UNVERIFIED"})
_MAPPING_EVIDENCE_KEYS = (
    "evidence",
    "evidence_refs",
    "cited_spans",
    "spans",
    "evidence_ids",
    "code_spans",
    "reviewed_spans",
)

#: Workbench run ids are ``<prefix> + 26-char Crockford ULID`` (see ids.py).
#: Experiment runs cited by a comparison use ``xrun_``; workflow runs use
#: ``run_``. A fabricated id such as ``nonexistent_b`` is not accepted even
#: though the pure function cannot query a run store.
_RUN_ID_PREFIXES = ("xrun_", "run_")
_ULID_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def _valid_run_id(value: Any) -> bool:
    text = str(value or "")
    for prefix in _RUN_ID_PREFIXES:
        if text.startswith(prefix):
            body = text[len(prefix) :]
            return len(body) == 26 and all(char in _ULID_ALPHABET for char in body)
    return False


#: Textual mutation/bypass markers in arbitrary note strings (for example a
#: ProtocolFinding.note saying "mutation survived"). Underscores and hyphens
#: are normalised to spaces before matching.
_MUTATION_TEXT_PHRASES = (
    "mutation survived",
    "mutant survived",
    "mutation not killed",
    "mutant not killed",
    "mutation survives",
    "mutant survives",
    "harness weakened",
    "critical test bypass",
    "bypass detected",
    "mutation bypass",
    "test bypass",
)


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _ref(kind: str, identifier: Any, sha256: str | None = None) -> dict[str, Any] | None:
    # Legitimate ids may be falsy values such as 0; only None and "" are absent.
    if identifier is None or identifier == "":
        return None
    ref: dict[str, Any] = {"kind": kind, "id": str(identifier)}
    if sha256:
        ref["sha256"] = str(sha256)
    return ref


def _sha(record: Any) -> str | None:
    """Hash of a cited record, when the record actually records one."""
    for attr in ("json_sha256", "patch_sha256", "sha256"):
        value = getattr(record, attr, None)
        if value:
            return str(value)
    details = _as_dict(getattr(record, "details", None))
    for key in ("sha256", "json_sha256", "source_sha256"):
        if details.get(key):
            return str(details[key])
    return None


def _record_ref(kind: str, record: Any) -> dict[str, Any] | None:
    return _ref(kind, getattr(record, "id", None), _sha(record))


def _tier(
    name: str,
    level: TierLevel,
    reasons: Iterable[str],
    evidence_refs: Iterable[dict[str, Any]],
    gaps: Iterable[str],
) -> TierResult:
    refs: list[dict[str, Any]] = []
    for ref in evidence_refs:
        if ref and ref not in refs:
            refs.append(ref)
    return TierResult(
        name=name,
        level=level,
        reasons=_dedupe(reasons),
        evidence_refs=refs,
        gaps=_dedupe(gaps),
    )


class TierResult(BaseModel):
    """One independently computed answer about a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    level: TierLevel
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ReproducibilityReport(BaseModel):
    """The complete, hash-addressed reproducibility report for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    run_id: str
    task_id: str
    project_id: str
    snapshot_id: str
    tiers: dict[str, TierResult] = Field(default_factory=dict)
    obligations: list[ValidationObligation] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    canonical_sha256: str
    generated_at: str


# --------------------------------------------------------------------- helpers
def _require_scope(
    *,
    run: WorkflowRun,
    task: Task,
    claims: Sequence[ResearchClaim],
    mappings: Sequence[ClaimMapping],
    report: VerificationReport | None,
    checks: Sequence[VerificationCheck],
    comparison: ExperimentComparison | None,
    diff: TaskDiff | None,
) -> None:
    """Refuse inputs that belong to a different run, task or project."""
    errors: list[str] = []
    if task.id != run.task_id:
        errors.append(f"task {task.id} does not match run {run.id} task {run.task_id}")
    if task.project_id != run.project_id:
        errors.append(
            f"task {task.id} belongs to project {task.project_id}, not {run.project_id}"
        )
    for claim in claims:
        if claim.project_id != run.project_id:
            errors.append(f"claim {claim.id} belongs to project {claim.project_id}")
    for mapping in mappings:
        if mapping.project_id != run.project_id:
            errors.append(f"mapping {mapping.id} belongs to project {mapping.project_id}")
        if mapping.snapshot_id != run.snapshot_id:
            errors.append(
                f"mapping {mapping.id} belongs to snapshot {mapping.snapshot_id}"
            )
    if report is not None and (
        report.project_id != run.project_id
        or report.run_id != run.id
        or report.task_id != task.id
    ):
        errors.append(f"verification report {report.id} belongs to a different run/task/project")
    for check in checks:
        if (
            check.project_id != run.project_id
            or check.run_id != run.id
            or check.task_id != task.id
        ):
            errors.append(f"check {check.id} belongs to a different run/task/project")
        # A check from a different verification attempt is mixed evidence even
        # when it passed all the run/task/project scope guards.
        if report is not None and check.verification_id != report.verification_id:
            errors.append(
                f"check {check.id} belongs to verification {check.verification_id}, "
                f"not report verification {report.verification_id}"
            )
    if comparison is not None:
        if comparison.project_id != run.project_id:
            errors.append(
                f"comparison {comparison.id} belongs to project {comparison.project_id}"
            )
        if comparison.task_id != task.id:
            errors.append(
                f"comparison {comparison.id} belongs to task {comparison.task_id}, not {task.id}"
            )
    if diff is not None and (
        diff.project_id != run.project_id
        or diff.run_id != run.id
        or diff.task_id != task.id
    ):
        errors.append(f"diff {diff.id} belongs to a different run/task/project")
    if errors:
        raise ValueError(
            "reproduction inputs are not scoped to this run/task/project: "
            + "; ".join(errors)
        )


def _criteria_requiring_attention(report: VerificationReport | None) -> list[str]:
    if report is None:
        return []
    result: list[str] = []
    for criterion in report.criteria:
        status = _enum_value(criterion.status)
        if status in _STRICT_STATUSES:
            result.append(f"{criterion.criterion_id}={status}")
    return sorted(result)


def _integrity_failed(checks: Sequence[VerificationCheck]) -> list[str]:
    """Integrity downgrades: exact key, TEST_INTEGRITY kind or an integrity key."""
    result: list[str] = []
    for check in checks:
        out = _enum_value(check.outcome)
        is_integrity = (
            check.key == "test.integrity"
            or check.kind is CheckKind.TEST_INTEGRITY
            or "integrity" in check.key.casefold()
        )
        if is_integrity and out in _INTEGRITY_DOWNGRADE_OUTCOMES:
            result.append(
                f"check {check.key} (id {check.id}) recorded {out}: test integrity is not verified"
            )
    return _dedupe(result)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "imported"}
    return bool(value)


def _mutation_text_hit(value: Any) -> bool:
    """True when a string explicitly states mutation survival or a bypass."""
    if not isinstance(value, str):
        return False
    text = re.sub(r"[\s_\-]+", " ", value).strip().casefold()
    return any(phrase in text for phrase in _MUTATION_TEXT_PHRASES)


def _integrity_failure_value(value: Any) -> bool:
    """True when an integrity field records a non-passing outcome."""
    if isinstance(value, bool):
        return not value
    return _enum_value(value).upper() in _INTEGRITY_DOWNGRADE_OUTCOMES


def _scan_mutation_payload(
    data: Any,
    out: list[str],
    *,
    seen: set[int] | None = None,
    budget: list[int] | None = None,
) -> None:
    """Recursively name mutation-survival/bypass markers in any payload.

    Cycles are broken with a visited-id set and the walk is bounded by a
    documented node budget; exhausting the budget is itself a named downgrade,
    so deeply nested evidence cannot evade detection by hiding below a cutoff.
    """
    if seen is None:
        seen = set()
    if budget is None:
        budget = [MUTATION_SCAN_NODE_BUDGET]
    stack: list[Any] = [data]
    while stack:
        if budget[0] <= 0:
            out.append("mutation-evidence scan node budget exhausted")
            return
        budget[0] -= 1
        current = stack.pop()
        if isinstance(current, Mapping):
            ident = id(current)
            if ident in seen:
                continue
            seen.add(ident)
            node = _as_dict(current)
            if node.get("_malformed"):
                out.append(str(node["_malformed"]))
                continue
            for key, value in node.items():
                lowered = key.casefold() if isinstance(key, str) else str(key).casefold()
                if (
                    lowered in {"mutation_survived", "survived_declared", "bypass_detected"}
                    and _is_truthy(value)
                ):
                    out.append(
                        "mutation probe detected a critical-test bypass"
                        if lowered == "bypass_detected"
                        else "mutation probe survived a declared critical test"
                    )
                if lowered == "counts":
                    for count_key, count_value in _as_dict(value).items():
                        if str(count_key).upper() == "SURVIVED":
                            try:
                                survived_count = int(count_value or 0)
                            except (TypeError, ValueError):
                                survived_count = 0
                            if survived_count > 0:
                                out.append(f"{survived_count} mutation probe(s) survived")
                if lowered == "outcome" and _enum_value(value).upper() == "SURVIVED":
                    probe_id = node.get("mutation_id") or node.get("test") or "unknown"
                    out.append(f"mutation probe {probe_id} survived")
                if "integrity" in lowered and _integrity_failure_value(value):
                    out.append(
                        f"integrity field {key}={_enum_value(value)} is not verified"
                    )
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            ident = id(current)
            if ident in seen:
                continue
            seen.add(ident)
            stack.extend(current)
        elif _mutation_text_hit(current):
            out.append(f"tampering marker in evidence text: {str(current)[:200]}")


def _mutation_survived(
    checks: Sequence[VerificationCheck], *sources: Any
) -> list[str]:
    """Find mutation-survival/bypass evidence in findings AND detail payloads."""
    findings: list[str] = []
    for check in checks:
        for finding in check.findings:
            code = _enum_value(getattr(finding, "code", "")).upper()
            if any(token in code for token in _MUTATION_TOKENS):
                findings.append(f"check {check.id} finding {code}")
            _scan_mutation_payload(getattr(finding, "details", None), findings)
    details: list[str] = []
    for source in sources:
        _scan_mutation_payload(source, details)
    return _dedupe([*findings, *details])


def _comparison_tampering(comparison: ExperimentComparison | None) -> list[str]:
    """Scan the whole comparison record for tampering/integrity evidence.

    A comparison is a first-class evidence record: its ``details``,
    ``findings`` (path/classification/note) and ``limitations`` are as capable
    of declaring a survived mutant or a failed integrity check as a
    verification check is. Ignoring them allowed a tampered harness to reach a
    YES result tier. Any hit is a downgrade that can only lower a tier.
    """
    if comparison is None:
        return []
    payload: dict[str, Any] = {
        "comparison": _as_dict(comparison),
        "details": _as_dict(getattr(comparison, "details", None)),
        "findings": [_as_dict(item) for item in (getattr(comparison, "findings", None) or ())],
        "limitations": list(getattr(comparison, "limitations", None) or ()),
    }
    found: list[str] = []
    _scan_mutation_payload(payload, found)
    return _dedupe(f"comparison {item}" for item in found)


def _is_severe_downgrade(text: str) -> bool:
    lowered = text.casefold()
    return any(token in lowered for token in _SEVERE_TOKENS)


def _cap_at_partial(
    level: TierLevel,
    reasons: list[str],
    gaps: list[str],
    downgrades: Sequence[str],
    *,
    force_no_on_severe: bool = False,
) -> tuple[TierLevel, list[str], list[str]]:
    """Integrity/unverified evidence can lower YES, never raise it.

    Severe tampering (integrity failure or mutation bypass) additionally forces
    METHOD_VALIDATED to NO via ``force_no_on_severe``; for the other tiers it
    caps at PARTIAL.
    """
    if not downgrades:
        return level, reasons, gaps
    severe = any(_is_severe_downgrade(item) for item in downgrades)
    if force_no_on_severe and severe:
        new_level: TierLevel = "NO"
    elif level == "YES":
        new_level = "PARTIAL"
    else:
        new_level = level
    transition = (
        f"downgraded from {level} to {new_level}"
        if new_level != level
        else f"integrity/unverified evidence present (level stays {level})"
    )
    reasons = reasons + [f"{transition}: " + "; ".join(downgrades)]
    gaps = gaps + list(downgrades)
    return new_level, reasons, gaps


def _explicit_mapping_evidence(details: Mapping[str, Any]) -> bool:
    return any(_iter_items(details.get(key)) for key in _MAPPING_EVIDENCE_KEYS)


def _mapping_verified(
    mapping: ClaimMapping, claims: Sequence[ResearchClaim] = ()
) -> bool:
    """A PRESENT mapping counts only with explicit, in-scope, verified evidence."""
    status = mapping.status
    if status in (ClaimMappingStatus.NOT_CHECKED, ClaimMappingStatus.UNCERTAIN):
        return False

    claim_by_id = {claim.id: claim for claim in claims}
    claim = claim_by_id.get(mapping.claim_id)
    if claim is None or claim.review_status is ClaimReviewStatus.REJECTED:
        return False

    details = _as_dict(mapping.details)
    if details.get("_malformed"):
        return False
    if details.get("coverage_limited") or details.get("discovery_truncated"):
        return False
    rule_status = _enum_value(details.get("rule_status"))

    author = _enum_value(mapping.created_by)
    if author == "MODEL" or mapping.model_invocation_id or details.get("model"):
        # A model-adjudicated mapping is never verified on its own. It needs an
        # accepted human review plus explicit evidence/provenance.
        if rule_status in _NEGATIVE_RULE_STATUSES:
            return False
        if mapping.review_status is not ClaimReviewStatus.ACCEPTED:
            return False
        if not (_explicit_mapping_evidence(details) or details.get("reviewer")):
            return False
    elif author == "USER":
        if rule_status in _NEGATIVE_RULE_STATUSES:
            return False
        if mapping.review_status is not ClaimReviewStatus.ACCEPTED:
            return False
        if not (
            _explicit_mapping_evidence(details)
            or mapping.rationale
            or mapping.validation_method
        ):
            return False
    else:
        # Deterministic mappings need an explicit rule_status that agrees with
        # the recorded status. A PRESENT verdict without rule_status=PRESENT is
        # an unattributed assertion, not verified code evidence.
        if rule_status not in _EXPLICIT_RULE_STATUSES or rule_status != status.value:
            return False

    gap_text = " ".join(mapping.gaps).casefold()
    if "coverage" in gap_text or "incomplete index" in gap_text:
        return False
    return True


def _patch_checks(checks: Sequence[VerificationCheck]) -> list[VerificationCheck]:
    """A patch-reproducible key is evidence only with deterministic kind+method."""
    return [
        check
        for check in checks
        if check.key in PATCH_REPRODUCIBLE_KEYS and _is_deterministic_check(check)
    ]


def _non_deterministic_patch_checks(
    checks: Sequence[VerificationCheck],
) -> list[VerificationCheck]:
    """Model-only/human-only patch checks are named gaps, never evidence."""
    return [
        check
        for check in checks
        if check.key in PATCH_REPRODUCIBLE_KEYS and not _is_deterministic_check(check)
    ]


# ------------------------------------------------------------------ tier logic
def _tier_code_found(
    mappings: Sequence[ClaimMapping], claims: Sequence[ResearchClaim] = ()
) -> TierResult:
    verified = [mapping for mapping in mappings if _mapping_verified(mapping, claims)]
    present = [
        mapping for mapping in verified if mapping.status == ClaimMappingStatus.PRESENT
    ]
    partial = [
        mapping
        for mapping in verified
        if mapping.status in (ClaimMappingStatus.PARTIAL, ClaimMappingStatus.CONFLICTING)
    ]
    matched = [
        mapping
        for mapping in verified
        if mapping.status not in {
            ClaimMappingStatus.PRESENT,
            ClaimMappingStatus.PARTIAL,
            ClaimMappingStatus.CONFLICTING,
        }
    ]
    if present:
        # Only the PRESENT mappings produced this YES; PARTIAL/CONFLICTING ones
        # must not be cited as if they supported it.
        return _tier(
            "METHOD_CODE_FOUND",
            "YES",
            [
                f"verified PRESENT mapping {mapping.id} for claim {mapping.claim_id}"
                for mapping in present
            ],
            [_record_ref("mapping", mapping) for mapping in present],
            [],
        )
    if partial:
        return _tier(
            "METHOD_CODE_FOUND",
            "PARTIAL",
            [
                f"mapping {mapping.id} is {mapping.status.value}, not PRESENT"
                for mapping in partial
            ],
            [_record_ref("mapping", mapping) for mapping in partial],
            _dedupe(
                gap
                for mapping in partial
                for gap in [*mapping.gaps, mapping.status_reason]
            ),
        )
    if not verified:
        return _tier(
            "METHOD_CODE_FOUND",
            "NOT_CHECKED",
            [
                "no claim-to-code mapping check was recorded" if not mappings
                else "no verified mapping was obtained (uncertain, NOT_CHECKED, "
                "coverage-limited, unattributed or rejected)"
            ],
            [],
            ["a verified definition-level mapping is required for this tier"],
        )
    return _tier(
        "METHOD_CODE_FOUND",
        "NO",
        [f"verified mapping {mapping.id} is {mapping.status.value}" for mapping in matched],
        [_record_ref("mapping", mapping) for mapping in matched],
        _dedupe(
            gap
            for mapping in matched
            for gap in [*mapping.gaps, mapping.status_reason]
        )
        or ["mappings were attempted but none matched the claimed method"],
    )


def _tier_implemented(
    diff: TaskDiff | None,
    checks: Sequence[VerificationCheck],
    report: VerificationReport | None,
    downgrades: Sequence[str],
) -> TierResult:
    if diff is None or not diff.files:
        return _tier(
            "METHOD_IMPLEMENTED",
            "NO",
            ["no captured diff: there is no candidate implementation to inspect"],
            [_ref("diff", getattr(diff, "id", None))],
            ["a non-empty TaskDiff is required for this tier"],
        )

    patch_checks = _patch_checks(checks)
    patch_passed = [
        check for check in patch_checks if check.outcome == CheckOutcome.PASSED
    ]
    criteria = _criteria_requiring_attention(report)
    report_ref = _ref(
        "report", getattr(report, "id", None), getattr(report, "json_sha256", None)
    )
    refs = [
        ref
        for ref in [
            _ref("diff", diff.id, getattr(diff, "patch_sha256", None)),
            # Only a PASSED patch check can produce this tier; failed patch
            # checks are gaps, not evidence, and must not be cited on a YES.
            *(_record_ref("check", check) for check in patch_passed),
            # The report's criteria can downgrade this tier, so cite it.
            report_ref if criteria or any("criterion" in item for item in downgrades) else None,
        ]
        if ref
    ]
    gaps: list[str] = []
    reasons = [f"captured diff {diff.id} touches {len(diff.files)} file(s)"]
    if patch_passed:
        reasons.extend(
            f"check {check.key} (id {check.id}) PASSED" for check in patch_passed
        )
    else:
        gaps.append("no passing patch.reproducible check for the captured diff")
    for check in _non_deterministic_patch_checks(checks):
        gaps.append(
            f"check {check.key} (id {check.id}) was produced by "
            f"{_enum_value(check.method)}/{_enum_value(check.kind)}, "
            "not deterministic evidence"
        )
    if criteria:
        gaps.extend(f"criterion {criterion} is not VERIFIED" for criterion in criteria)

    if patch_passed and not criteria:
        level: TierLevel = "YES"
    else:
        level = "PARTIAL"
    level, reasons, gaps = _cap_at_partial(level, reasons, gaps, downgrades)
    return _tier("METHOD_IMPLEMENTED", level, reasons, refs, gaps)


def _tier_validated(
    checks: Sequence[VerificationCheck],
    report: VerificationReport | None,
    coverage_result: Mapping[str, Any],
    downgrades: Sequence[str],
    comparison: ExperimentComparison | None = None,
) -> TierResult:
    non_deterministic = [
        check
        for check in checks
        if check.required
        and check.kind in FUNCTIONAL_KINDS
        and not _is_deterministic_check(check)
    ]
    method_gaps = [
        f"required functional check {check.key} (id {check.id}) used "
        f"{_enum_value(check.method)}/{_enum_value(check.kind)}, "
        "not deterministic evidence"
        for check in non_deterministic
    ]
    functional = [
        check
        for check in checks
        if check.required and check.kind in FUNCTIONAL_KINDS and _is_deterministic_check(check)
    ]
    if not functional:
        # No functional check produced this tier; citing unrelated checks would
        # over-state the evidence. Downgrades still appear in the gaps below.
        return _tier(
            "METHOD_VALIDATED",
            "NOT_CHECKED",
            ["no required functional check (COMMAND or ML_INVARIANT) was recorded"],
            [],
            [
                "at least one required functional check must run before this tier can be assigned",
                *method_gaps,
            ],
        )

    failed = [check for check in functional if check.outcome == CheckOutcome.FAILED]
    passed = [check for check in functional if check.outcome == CheckOutcome.PASSED]
    other = [check for check in functional if check not in failed and check not in passed]
    missing = list(coverage_result.get("missing_required") or [])
    key_conflicts = list(coverage_result.get("key_conflicts") or [])
    criteria = _criteria_requiring_attention(report)
    comparison_id = str(getattr(comparison, "id", "")) if comparison is not None else ""
    comparison_cited = any(
        comparison_id and comparison_id in (item.get("evidence") or [])
        for item in (coverage_result.get("obligations") or [])
    )

    if failed:
        producing_checks = failed
    elif other:
        producing_checks = other
    else:
        producing_checks = passed

    refs = [
        ref
        for ref in [
            *(_record_ref("check", check) for check in producing_checks),
            _ref("report", getattr(report, "id", None), getattr(report, "json_sha256", None))
            if criteria or any("criterion" in item for item in downgrades)
            else None,
            _ref(
                "comparison",
                comparison_id or None,
                getattr(comparison, "json_sha256", None),
            )
            if comparison_cited
            else None,
        ]
        if ref
    ]
    reasons = [
        f"functional check {check.key} (id {check.id}) {check.outcome.value}"
        for check in functional
    ]
    gaps: list[str] = []
    for check in failed:
        gaps.append(f"required functional check {check.key} FAILED")
    for check in other:
        gaps.append(f"required functional check {check.key} is {check.outcome.value}")
    gaps.extend(
        f"obligation {obligation_id} has no passing evidence" for obligation_id in missing
    )
    gaps.extend(f"criterion {criterion} is not VERIFIED" for criterion in criteria)
    gaps.extend(key_conflicts)

    if failed:
        level: TierLevel = "NO"
    elif len(passed) == len(functional) and not missing and not other and not key_conflicts:
        level = "YES"
    else:
        level = "PARTIAL"
    level, reasons, gaps = _cap_at_partial(
        level, reasons, gaps, downgrades, force_no_on_severe=True
    )
    return _tier("METHOD_VALIDATED", level, reasons, refs, gaps)


def _tier_result_reproduced(
    comparison: ExperimentComparison | None,
    report: VerificationReport | None,
    run_details: Mapping[str, Any],
    coverage_result: Mapping[str, Any],
    *,
    checks: Sequence[VerificationCheck] = (),
    validated_level: TierLevel = "NOT_CHECKED",
    downgrades: Sequence[str] = (),
) -> TierResult:
    check_list = list(checks)
    functional = [
        check for check in check_list if check.required and check.kind in FUNCTIONAL_KINDS
    ]
    passing_functional = [
        check for check in functional if check.outcome == CheckOutcome.PASSED
    ]

    if comparison is None:
        return _tier(
            "RESULT_REPRODUCED",
            "NOT_CHECKED",
            ["no experiment comparison is attached to this run"],
            [],
            ["a pre-registered fair comparison is required for this tier", *downgrades],
        )

    verdict = _enum_value(comparison.verdict)
    # A comparison is fetched by task, not by run. Only a run-level binding can
    # stop run A citing run B's comparison; without one this tier is capped.
    bound_id = run_details.get("experiment_comparison_id")
    run_bound = bound_id is not None and str(bound_id) == comparison.id
    report_ref = (
        _ref("report", getattr(report, "id", None), getattr(report, "json_sha256", None))
        if any("criterion" in item for item in downgrades)
        else None
    )
    # Functional checks support a candidate-favourable claim; for a baseline
    # advantage or an inconclusive verdict the comparison itself decides.
    cite_functional = verdict in {
        "CANDIDATE_ADVANTAGE",
        "CANDIDATE_NON_INFERIOR",
        "NO_DETECTABLE_DIFFERENCE",
    }
    refs = [
        ref
        for ref in [
            _record_ref("comparison", comparison),
            _ref("artifact", getattr(comparison, "report_artifact_id", None)),
            *(
                _ref("run", run_id)
                for run_id in [*comparison.baseline_run_ids, *comparison.candidate_run_ids]
            ),
            report_ref,
            *(
                _record_ref("check", check)
                for check in passing_functional
                if cite_functional
            ),
        ]
        if ref
    ]
    gaps: list[str] = []
    reasons = [f"comparison {comparison.id} verdict {verdict}"]

    fairness = _fairness_violations(comparison)
    imported = _imported_notes(
        comparison.details,
        comparison,
        run_details,
        *(check.details for check in check_list),
    )
    primary, usable, metric_note = _primary_metric(comparison)
    missing = list(coverage_result.get("missing_required") or [])
    key_conflicts = list(coverage_result.get("key_conflicts") or [])
    stats_supported, stats_note = _verdict_statistics_support(comparison)
    parity = verdict in {"CANDIDATE_NON_INFERIOR", "NO_DETECTABLE_DIFFERENCE"}
    margin_ok, margin_note = (
        _margin_evidence(comparison) if parity else (True, "")
    )

    if not passing_functional:
        gaps.append("no passing required functional check supports this result")
    if validated_level != "YES":
        gaps.append(
            f"METHOD_VALIDATED is {validated_level}; a reproduction cannot exceed its validation"
        )
    gaps.extend(fairness)
    if imported:
        gaps.append(
            "comparison contains runs produced outside this workbench: "
            + "; ".join(imported)
        )
    if not comparison.comparable:
        gaps.append("comparison is marked NOT comparable")
    if not comparison.baseline_run_ids or not comparison.candidate_run_ids:
        gaps.append("comparison has no baseline and candidate runs")
    invalid_run_ids = [
        run_id
        for run_id in [*comparison.baseline_run_ids, *comparison.candidate_run_ids]
        if not _valid_run_id(run_id)
    ]
    if invalid_run_ids:
        gaps.append(
            "comparison cites run ids that are not workbench run ids: "
            + ", ".join(str(item) for item in invalid_run_ids)
        )
    if not usable:
        gaps.append(metric_note)
    gaps.extend(
        f"obligation {obligation_id} has no passing evidence" for obligation_id in missing
    )
    gaps.extend(key_conflicts)
    gaps.extend(list(comparison.limitations or []))
    reasons.append(metric_note)
    reasons.append(stats_note)
    if not stats_supported:
        gaps.append(stats_note)
    if parity:
        reasons.append(margin_note)
        if not margin_ok:
            gaps.append(margin_note)
    if bound_id is None:
        gaps.append(
            "no run-level comparison binding (run.details.experiment_comparison_id): "
            "the comparison is task-scoped and may belong to another run; "
            "RESULT_REPRODUCED is capped at PARTIAL"
        )
    elif not run_bound:
        gaps.append(
            f"run declares experiment_comparison_id={bound_id}, which does not match "
            f"comparison {comparison.id}"
        )

    if verdict in {"INCONCLUSIVE", "NOT_COMPARABLE"}:
        reasons.append(f"verdict {verdict} does not answer the reproduction question")
        gaps.append("a decisive comparison is required for this tier")
        level: TierLevel = "NOT_CHECKED"
    elif verdict == "BASELINE_ADVANTAGE":
        reasons.append("the baseline outperformed the candidate on the primary metric")
        level = "NO"
    else:
        clean = (
            bool(comparison.baseline_run_ids)
            and bool(comparison.candidate_run_ids)
            and not invalid_run_ids
            and comparison.comparable
            and not fairness
            and not imported
            and usable
            and stats_supported
            and not missing
            and not key_conflicts
            and bool(passing_functional)
            and validated_level == "YES"
        )
        if verdict == "CANDIDATE_ADVANTAGE":
            if clean:
                reasons.append(
                    "fair, run-bound comparison with statistics supporting the verdict "
                    "and full obligation coverage"
                )
                level = "YES"
            else:
                reasons.append("the comparison does not yet establish a reproduced result")
                level = "PARTIAL"
        elif parity:
            if clean and margin_ok:
                reasons.append(
                    f"{verdict} with {margin_note}, usable statistics and full obligation coverage"
                )
                level = "YES"
            else:
                reasons.append(
                    f"{verdict} is weaker than a reproduced advantage unless an exact "
                    "declared margin is satisfied with usable statistics"
                )
                level = "PARTIAL"
        else:
            reasons.append("the comparison verdict does not establish a reproduced result")
            level = "PARTIAL"

    if not run_bound and level == "YES":
        reasons.append("capped at PARTIAL: the comparison is not bound to this run")
        level = "PARTIAL"

    level, reasons, gaps = _cap_at_partial(level, reasons, gaps, downgrades)
    return _tier("RESULT_REPRODUCED", level, reasons, refs, gaps)


# ------------------------------------------------------------------ public API
def build_reproduction_report(
    *,
    run: WorkflowRun,
    task: Task,
    run_details: Mapping[str, Any] | None,
    claims: Sequence[ResearchClaim],
    mappings: Sequence[ClaimMapping],
    report: VerificationReport | None,
    checks: Sequence[VerificationCheck],
    comparison: ExperimentComparison | None,
    diff: TaskDiff | None = None,
    generated_at: datetime | str | None = None,
) -> ReproducibilityReport:
    """Compute the four tiers from persisted records. Pure and deterministic.

    ``generated_at`` is the only source of "now": pass it explicitly to make the
    output reproducible in tests; otherwise it defaults to ``clock.utcnow()``
    evaluated once, at call time.
    """
    claim_list = list(claims)
    mapping_list = list(mappings)
    check_list = list(checks)
    _require_scope(
        run=run,
        task=task,
        claims=claim_list,
        mappings=mapping_list,
        report=report,
        checks=check_list,
        comparison=comparison,
        diff=diff,
    )

    details = _as_dict(run_details)
    report_details = _as_dict(getattr(report, "details", None)) if report else {}

    obligations = obligations_for_claims(claim_list, checks=check_list)
    coverage_result = obligation_coverage(obligations, check_list, comparison)

    downgrades = [
        *_integrity_failed(check_list),
        *_mutation_survived(
            check_list,
            details,
            report_details,
            *(check.details for check in check_list),
        ),
        *_malformed_notes(run_details, getattr(report, "details", None) if report else None),
        *(
            f"criterion {criterion} is not VERIFIED"
            for criterion in _criteria_requiring_attention(report)
        ),
    ]
    downgrades = _dedupe(downgrades)

    code_found = _tier_code_found(mapping_list, claim_list)
    implemented = _tier_implemented(diff, check_list, report, downgrades)
    validated = _tier_validated(
        check_list, report, coverage_result, downgrades, comparison
    )
    result = _tier_result_reproduced(
        comparison,
        report,
        details,
        coverage_result,
        checks=check_list,
        validated_level=validated.level,
        downgrades=downgrades,
    )
    tiers = {
        "METHOD_CODE_FOUND": code_found,
        "METHOD_IMPLEMENTED": implemented,
        "METHOD_VALIDATED": validated,
        "RESULT_REPRODUCED": result,
    }

    # With no claims/obligations there is no question to answer. Absence of
    # evidence is NOT_CHECKED, never a vacuous YES.
    if not claim_list or not obligations:
        gap = "no research claims/obligations were declared for this run"
        for name, reason in (
            (
                "METHOD_VALIDATED",
                "validation cannot be assessed without declared obligations",
            ),
            (
                "RESULT_REPRODUCED",
                "reproduction cannot be assessed without declared obligations",
            ),
        ):
            # Keep the reasons, evidence refs, gaps and tampering downgrades
            # already computed: swapping in fresh empty NOT_CHECKED tiers would
            # erase the very findings that must lower these tiers.
            existing = tiers[name]
            tiers[name] = _tier(
                name,
                "NOT_CHECKED",
                [*existing.reasons, reason, *downgrades],
                existing.evidence_refs,
                [*existing.gaps, gap, *downgrades],
            )

    limitations = list(CANONICAL_LIMITATIONS)
    if comparison is not None:
        bound_id = details.get("experiment_comparison_id")
        if bound_id is None or str(bound_id) != comparison.id:
            limitations.append(
                "The experiment comparison is task-scoped, not run-scoped; this run "
                "does not declare a matching experiment_comparison_id, so the "
                "comparison may belong to another run of the same task. "
                "RESULT_REPRODUCED cannot exceed PARTIAL on this basis."
            )
    if _imported_notes(
        getattr(comparison, "details", None),
        details,
        comparison,
        *(check.details for check in check_list),
    ):
        limitations.append(
            "The comparison includes imported runs executed outside this workbench; "
            "their environment and execution were not independently verified here."
        )
    if report is not None:
        limitations.extend(
            item
            for item in report.limitations
            if "outside this workbench" in str(item).casefold()
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.id,
        "task_id": task.id,
        "project_id": run.project_id,
        "snapshot_id": run.snapshot_id,
        "tiers": {
            name: {
                "name": tier.name,
                "level": tier.level,
                "reasons": tier.reasons,
                "evidence_refs": tier.evidence_refs,
                "gaps": tier.gaps,
            }
            for name, tier in tiers.items()
        },
        "obligations": [item.model_dump(mode="json") for item in obligations],
        "coverage": coverage_result,
        "limitations": _dedupe(limitations),
    }
    canonical = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()

    timestamp = generated_at if isinstance(generated_at, str) else to_iso(
        generated_at or utcnow()
    )
    return ReproducibilityReport(
        schema_version=SCHEMA_VERSION,
        run_id=run.id,
        task_id=task.id,
        project_id=run.project_id,
        snapshot_id=run.snapshot_id,
        tiers=tiers,
        obligations=obligations,
        coverage=coverage_result,
        limitations=payload["limitations"],
        canonical_sha256=canonical,
        generated_at=timestamp,
    )


def render_reproduction_markdown(report: ReproducibilityReport) -> str:
    """Render a human-inspectable report; every tier section is always present."""
    lines: list[str] = [
        "# Reproducibility report",
        "",
        f"- Schema: `{report.schema_version}`",
        f"- Run: `{report.run_id}`",
        f"- Task: `{report.task_id}`",
        f"- Project: `{report.project_id}`",
        f"- Snapshot: `{report.snapshot_id}`",
        f"- Canonical sha256: `{report.canonical_sha256}`",
        f"- Generated at: `{report.generated_at}`",
        "",
        "A tier is a separate question. `NOT_CHECKED` is never success.",
        "",
    ]
    for name in TIER_NAMES:
        tier = report.tiers.get(name)
        lines.extend([f"## {name}", "", ""])
        if tier is None:
            lines[-1] = "**Level: NOT_CHECKED — missing tier**"
            lines.extend(["", "- Gap: this tier was absent from the persisted report"])
            lines.append("")
            continue
        lines[-1] = f"**Level: {tier.level}**"
        lines.append("")
        lines.extend(f"- Reason: {reason}" for reason in tier.reasons)
        if tier.evidence_refs:
            for ref in tier.evidence_refs:
                suffix = f" sha256 `{ref['sha256']}`" if ref.get("sha256") else ""
                lines.append(f"- Evidence: `{ref['kind']}` `{ref['id']}`{suffix}")
        if tier.gaps:
            lines.extend(f"- Gap: {gap}" for gap in tier.gaps)
        lines.append("")

    lines.extend(
        [
            "## Obligations",
            "",
            "| id | kind | claim | source | required | covered | evidence |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    covered = {
        item["id"]: item for item in (report.coverage.get("obligations") or [])
    }
    for obligation in sorted(report.obligations, key=lambda item: item.id):
        status = covered.get(obligation.id, {})
        lines.append(
            f"| `{obligation.id}` | `{obligation.kind}` | `{obligation.claim_id}` | "
            f"`{obligation.source}` | {'yes' if obligation.required else 'no'} | "
            f"{'yes' if status.get('covered') else 'no'} | "
            f"{', '.join('`' + item + '`' for item in status.get('evidence') or []) or '—'} |"
        )

    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report.limitations)
    return "\n".join(lines) + "\n"


__all__ = [
    "SCHEMA_VERSION",
    "TIER_NAMES",
    "TierLevel",
    "TierResult",
    "ReproducibilityReport",
    "build_reproduction_report",
    "render_reproduction_markdown",
]

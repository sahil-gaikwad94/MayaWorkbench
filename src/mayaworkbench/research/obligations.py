"""Deterministic validation obligations derived from typed research claims.

An :class:`~mayaworkbench.domain.models.ResearchClaim` states *what* must hold;
a :class:`~mayaworkbench.domain.models.VerificationCheck` records *how* something
was checked. This module bridges the two without a model call: it turns the
claim's typed fields (``kind``, ``validation_methods``, ``validation_ids`` and
the claim text) plus the declared ``invariant.<name>`` check keys into a sorted,
stable list of :class:`ValidationObligation` records.

The taxonomies below are deliberately closed and documented. A keyword that is
not in the table produces no obligation; a check key that is not in the
invariant table produces no obligation. That is intentional: an unknown string
must fail closed as "no evidence" rather than be guessed into a pass.

The coverage report uses the same evidence vocabulary as
``verification.report.evidence_classification``: a PASSED deterministically
checked check is evidence, a fair and usable experiment is experimentally
observed evidence, and anything else (a model summary, a human note, a
malformed payload) is unverified. The helpers in this module are shared with
``research.reproduction`` so the two agree on what counts as evidence.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.domain.enums import (
    CheckKind,
    CheckOutcome,
    ClaimKind,
    ExperimentVerdict,
    VerificationMethod,
)
from mayaworkbench.domain.models import ResearchClaim, VerificationCheck

ObligationKind = Literal[
    "SHAPE",
    "MASKING",
    "NUMERICAL_REFERENCE",
    "GRADIENT",
    "INVARIANT",
    "SPLIT_PARITY",
    "METRIC",
    "BASELINE_PARITY",
    "SEED",
    "DATASET_PARITY",
]

ALL_OBLIGATION_KINDS: tuple[ObligationKind, ...] = (
    "SHAPE",
    "MASKING",
    "NUMERICAL_REFERENCE",
    "GRADIENT",
    "INVARIANT",
    "SPLIT_PARITY",
    "METRIC",
    "BASELINE_PARITY",
    "SEED",
    "DATASET_PARITY",
)

#: Keyword taxonomy for ``claim.validation_methods``. EVERY matching row is
#: returned (with stable de-duplication) so a string that declares several
#: obligations does not silently lose all but the first. Matching is
#: case-insensitive and treats ``-`` and ``_`` as spaces.
METHOD_KEYWORD_TAXONOMY: tuple[tuple[ObligationKind, tuple[str, ...]], ...] = (
    ("GRADIENT", ("gradient", "derivative", "finite difference", "autograd", "backprop", "jacobian", "chain rule")),
    ("MASKING", ("causal", "mask", "attention bias", "look ahead", "autoregressive", "padding")),
    ("SHAPE", ("shape", "dtype", "device", "dimension", "rank", "layout", "tensor size")),
    ("SPLIT_PARITY", ("split", "train test", "holdout", "stratif", "shuffle", "fold")),
    ("DATASET_PARITY", ("dataset", "data version", "corpus", "preprocess", "tokeniz", "fingerprint", "input distribution")),
    ("SEED", ("seed", "determin", "random state", "rng")),
    ("BASELINE_PARITY", ("baseline", "parity", "reference implementation", "paper result", "against the paper")),
    ("NUMERICAL_REFERENCE", ("numerical", "reference", "tolerance", "softmax", "normalization", "moment", "conservation", "closed form", "analytic", "float", "precision", "allclose", "isclose")),
    ("METRIC", ("metric", "accuracy", "loss", "perplexity", "f1", "bleu", "score", "evaluate", "measurement")),
    ("INVARIANT", ("invariant", "equivariance", "permutation", "symmetry", "property check", "commut")),
)

#: Documented mapping from the ``<name>`` part of an ``invariant.<name>`` check
#: key to an obligation kind. Exact names are checked first; the remaining rows
#: are matched as case-insensitive prefixes (so ``causal_mask`` and
#: ``causal_lookahead`` both map to MASKING).
INVARIANT_KEY_EXACT: Mapping[str, ObligationKind] = {
    "finite_difference_gradient": "GRADIENT",
    "softmax_row_sum": "NUMERICAL_REFERENCE",
    "normalization_moments": "NUMERICAL_REFERENCE",
    "conservation_of_sum": "NUMERICAL_REFERENCE",
    "permutation_equivariance": "INVARIANT",
    "shape_preserved": "INVARIANT",
}

INVARIANT_KEY_PREFIXES: tuple[tuple[str, ObligationKind], ...] = (
    ("causal", "MASKING"),
    ("mask", "MASKING"),
    ("finite_difference_gradient", "GRADIENT"),
    ("gradient", "GRADIENT"),
    ("softmax_row_sum", "NUMERICAL_REFERENCE"),
    ("normalization_moments", "NUMERICAL_REFERENCE"),
    ("conservation_of_sum", "NUMERICAL_REFERENCE"),
    ("permutation_equivariance", "INVARIANT"),
    ("shape_preserved", "INVARIANT"),
)

_INVARIANT_KEY_RE = re.compile(r"^invariant\.([a-z][a-z0-9_]*)$")

#: Only these kinds are deterministic evidence. A check produced by a model or
#: by a human assertion is never allowed to cover an obligation.
DETERMINISTIC_EVIDENCE_KINDS: frozenset[CheckKind] = frozenset(
    {
        CheckKind.COMMAND,
        CheckKind.ML_INVARIANT,
        CheckKind.DETERMINISTIC,
        CheckKind.TEST_INTEGRITY,
    }
)

#: ``check.kind`` alone is not enough: a check can carry a deterministic kind
#: while its ``method`` records an independent model review or a human note.
#: Only these methods produce deterministic evidence. INDEPENDENT_MODEL_REVIEW,
#: HUMAN_REVIEW and NOT_CHECKED are deliberately absent.
DETERMINISTIC_EVIDENCE_METHODS: frozenset[VerificationMethod] = frozenset(
    {
        VerificationMethod.DETERMINISTIC_TEST,
        VerificationMethod.NUMERICAL_INVARIANT,
        VerificationMethod.RUNTIME_OBSERVATION,
        VerificationMethod.STATIC_ANALYSIS,
        VerificationMethod.SOURCE_INSPECTION,
        VerificationMethod.EXPERIMENT,
    }
)


def is_deterministic_check(check: Any) -> bool:
    """True only when BOTH the kind and the method are deterministic.

    Every place that treats a check as deterministic evidence (obligation
    coverage, functional validation and patch reproduction) must use this
    helper so a model-only or human-only verdict can never raise a tier.
    """
    return (
        getattr(check, "kind", None) in DETERMINISTIC_EVIDENCE_KINDS
        and getattr(check, "method", None) in DETERMINISTIC_EVIDENCE_METHODS
    )

#: A comparison covers a METRIC/BASELINE_PARITY obligation only when it actually
#: answers the reproduction question. Inconclusive/not-comparable verdicts do
#: not.
DECISIVE_VERDICTS: frozenset[str] = frozenset(
    {
        ExperimentVerdict.CANDIDATE_ADVANTAGE.value,
        ExperimentVerdict.BASELINE_ADVANTAGE.value,
        ExperimentVerdict.CANDIDATE_NON_INFERIOR.value,
        ExperimentVerdict.NO_DETECTABLE_DIFFERENCE.value,
    }
)

_IMPORTED_FLAG_KEYS = (
    "experiment_imported_runs",
    "imported_runs",
    "has_imported_runs",
    "runs_imported",
    "imported",
)
_PROVENANCE_KEYS = (
    "metrics_source",
    "metrics_source_kind",
    "source_kind",
    "source",
    "provenance",
    "execution_source",
    "origin",
)
_IMPORTED_SOURCE_VALUES = frozenset(
    {"IMPORTED", "IMPORTED_RUN", "EXTERNAL", "OUTSIDE_WORKBENCH", "OUTSIDE_THIS_WORKBENCH"}
)

#: Documented, broad provenance markers. An evidence record that names any of
#: these in a nested string was produced somewhere other than this workbench.
#: The list is intentionally explicit: benign words such as "local" are NOT
#: markers. Provider names are matched as whole words so "modal" cannot be
#: confused with an unrelated adjective in prose.
_EXTERNAL_PROVENANCE_RE = re.compile(
    r"\b(?:external|imported|remote|hosted|cluster|kaggle|colab|runpod|skypilot|modal)\b"
    r"|outside\s+this\s+workbench"
    r"|third[\s_-]?party"
    r"|\b(?:external|hosted|remote|cloud|third[\s_-]?party)\s+service\b",
    re.IGNORECASE,
)


def has_external_provenance(text: Any) -> bool:
    """True when a string explicitly names non-local execution provenance."""
    return isinstance(text, str) and bool(_EXTERNAL_PROVENANCE_RE.search(text))
_RECOGNIZED_FAVORS = frozenset(
    {"", "CANDIDATE", "BASELINE", "INDETERMINATE", "UNSPECIFIED_DIRECTION"}
)
_EXPLICIT_RULE_STATUSES = frozenset({"PRESENT", "PARTIAL", "CONFLICTING", "ABSENT"})

#: Significance level used when a comparison does not record one. Matches
#: ``experiments.stats.DEFAULT_ALPHA``; declared locally so this pure module has
#: no dependency on an experiment service. The chosen alpha is always reported
#: by :func:`verdict_statistics_support` so a default is never mistaken for a
#: pre-registered threshold.
DEFAULT_ALPHA = 0.05

#: ``favors`` values that do not answer a directional question. A directional
#: verdict needs a recorded, verdict-consistent direction.
_INDETERMINATE_FAVORS = frozenset({"", "INDETERMINATE", "UNSPECIFIED_DIRECTION"})
_ADVANTAGE_FAVORS = {
    "CANDIDATE_ADVANTAGE": "CANDIDATE",
    "BASELINE_ADVANTAGE": "BASELINE",
}

#: Read a pre-registered margin from comparison details. A bare
#: ``within_margin`` boolean is never sufficient for a parity YES.
_MARGIN_VALUE_KEYS = (
    "non_inferiority_margin",
    "equivalence_margin",
    "margin",
    "margin_value",
)
_MARGIN_SOURCE_KEYS = ("margin_source", "margin_basis", "margin_rationale")

#: Upper bound on nodes inspected while looking for imported-run provenance.
#: A budget (rather than a depth cutoff) prevents both deep nesting and cycles
#: from hiding evidence; exhausting it is itself reported as a gap.
IMPORTED_SCAN_NODE_BUDGET = 2000

#: Bounds the recursive mutation-evidence scanner in ``research.reproduction``.
#: Re-exported here so both scanners document the same kind of safety limit.
MUTATION_SCAN_NODE_BUDGET = 2000


class ValidationObligation(BaseModel):
    """One typed thing a claim says must be demonstrated before it is trusted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: ObligationKind
    claim_id: str
    description: str
    source: Literal["claim", "check", "experiment"] = "claim"
    satisfying_check_keys: tuple[str, ...] = ()
    required: bool = True


# ------------------------------------------------------------------ shared helpers
def as_dict(value: Any) -> dict[str, Any]:
    """Best-effort mapping view that FAILS CLOSED with an explicit note.

    A malformed payload never silently becomes ``{}``: callers surface the
    ``_malformed`` marker as a gap, so bad evidence cannot contribute to a YES.
    ``None`` stays an ordinary empty mapping (absence of evidence, not malformed).
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception as exc:  # pragma: no cover - defensive
            return {"_malformed": f"unreadable mapping ({type(value).__name__}): {exc}"}
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
        except Exception as exc:  # pragma: no cover - defensive
            return {"_malformed": f"model_dump failed ({type(value).__name__}): {exc}"}
        if isinstance(dumped, Mapping):
            return dict(dumped)
        return {
            "_malformed": (
                f"model_dump returned {type(dumped).__name__}, expected a mapping"
            )
        }
    return {"_malformed": f"expected a mapping, got {type(value).__name__}"}


def malformed_notes(*values: Any) -> list[str]:
    """Explicit notes for any payload that could not be read as a mapping."""
    return [
        str(data["_malformed"])
        for data in (as_dict(value) for value in values)
        if isinstance(data, Mapping) and data.get("_malformed")
    ]


def enum_value(value: Any) -> str:
    """Stable string for an enum or literal, without assuming a type."""
    return str(getattr(value, "value", value))


def iter_items(value: Any) -> list[Any]:
    """Normalise a loosely typed sequence without inventing extra entries.

    A bare string is ONE item (not one item per character) and a mapping is
    rendered as key/value pairs rather than iterated as bare keys.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [f"{key}={item}" for key, item in value.items()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def finite_number(value: Any) -> bool:
    """True only for a real, finite number (bools and non-numerics are rejected)."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def fairness_violations(comparison: Any) -> list[str]:
    """Fairness violations from typed findings and explicit detail fields."""
    if comparison is None:
        return []
    violations: list[str] = []
    for finding in getattr(comparison, "findings", ()) or ():
        classification = enum_value(getattr(finding, "classification", None))
        if classification == "FAIRNESS_VIOLATION":
            path = getattr(finding, "path", "") or "unknown"
            note = getattr(finding, "note", "") or "fairness violation"
            violations.append(f"finding {path}: {note}")
        for code in iter_items(getattr(finding, "details", {}).get("codes")):
            if enum_value(code).upper() == "FAIRNESS_VIOLATION":
                violations.append(f"finding {getattr(finding, 'path', 'unknown')}: fairness violation code")
    details = as_dict(getattr(comparison, "details", None))
    for note in malformed_notes(getattr(comparison, "details", None)):
        violations.append(note)
    for key in ("fairness_violations", "experiment_fairness_violations"):
        for item in iter_items(details.get(key)):
            violations.append(str(item))
    return list(dict.fromkeys(violations))


def _is_imported_value(value: Any) -> bool:
    text = enum_value(value).upper()
    return text in _IMPORTED_SOURCE_VALUES


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "imported"}
    return bool(value)


def _scan_imported(
    data: Any,
    found: list[str],
    *,
    seen: set[int] | None = None,
    budget: list[int] | None = None,
) -> None:
    """Walk every nested mapping/list, not just the first five levels.

    An explicit visited-id set breaks cycles and shared sub-graphs; a documented
    node budget bounds pathological inputs. Exhausting the budget is reported as
    an explicit note so it can never silently hide imported provenance.
    """
    if seen is None:
        seen = set()
    if budget is None:
        budget = [IMPORTED_SCAN_NODE_BUDGET]
    stack: list[Any] = [data]
    while stack:
        if budget[0] <= 0:
            found.append("imported-evidence scan node budget exhausted")
            return
        budget[0] -= 1
        current = stack.pop()
        if isinstance(current, Mapping):
            ident = id(current)
            if ident in seen:
                continue
            seen.add(ident)
            for key in _IMPORTED_FLAG_KEYS:
                if key in current and _truthy(current[key]):
                    found.append(f"{key}=True")
            for key in _PROVENANCE_KEYS:
                if key in current and _is_imported_value(current[key]):
                    found.append(f"{key}={enum_value(current[key])}")
            for key in ("limitations", "notes", "note"):
                for item in iter_items(current.get(key)):
                    text = str(item).casefold()
                    if (
                        "outside this workbench" in text
                        or "imported run" in text
                        or "imported metric" in text
                    ):
                        found.append(str(item)[:200])
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            ident = id(current)
            if ident in seen:
                continue
            seen.add(ident)
            stack.extend(current)
        elif has_external_provenance(current):
            # Any nested string, not only a small closed vocabulary of keys,
            # can declare non-local execution (for example
            # execution_environment="EXTERNAL CLUSTER" or
            # runner="an external service"). Numeration of these strings is
            # bounded by the same node budget and visited-id set as mappings.
            found.append(str(current)[:200])


def imported_evidence(*sources: Any) -> list[str]:
    """Explicit notes for evidence produced outside this workbench.

    All supplied explicit provenance fields are inspected (comparison details,
    check details, run details and metric source enums). A malformed payload is
    itself a reason to refuse a YES.
    """
    found: list[str] = []
    for source in sources:
        data = as_dict(source)
        if data.get("_malformed"):
            found.append(str(data["_malformed"]))
            continue
        _scan_imported(data, found)
    return list(dict.fromkeys(found))


def evaluate_primary_metric(comparison: Any) -> tuple[Any | None, bool, str]:
    """Validate the single declared primary metric; never invent one.

    Returns ``(metric, usable, note)``. ``usable`` is True only when exactly one
    primary metric is declared, p-values are finite in [0, 1], the CI is finite
    and ordered, and a stated ``favors`` agrees with the declared direction.
    """
    metrics = list(getattr(comparison, "metrics", ()) or ())
    primaries = [metric for metric in metrics if getattr(metric, "primary", False)]
    if not primaries:
        return None, False, "the comparison declares no primary metric"
    if len(primaries) > 1:
        return None, False, (
            f"the comparison declares {len(primaries)} primary metrics; exactly one is required"
        )

    primary = primaries[0]
    key = str(getattr(primary, "key", "primary"))
    p_value = getattr(primary, "p_value", None)
    adjusted = getattr(primary, "p_value_adjusted", None)
    chosen = p_value if p_value is not None else adjusted
    if chosen is None:
        return primary, False, f"primary metric {key} has no p-value"
    if not finite_number(chosen) or not 0.0 <= float(chosen) <= 1.0:
        return primary, False, f"primary metric {key} has an invalid p-value"
    if adjusted is not None and (
        not finite_number(adjusted) or not 0.0 <= float(adjusted) <= 1.0
    ):
        return primary, False, f"primary metric {key} has an invalid adjusted p-value"

    low = getattr(primary, "ci_low", None)
    high = getattr(primary, "ci_high", None)
    if low is None or high is None:
        return primary, False, f"primary metric {key} has no usable confidence interval"
    if (
        not finite_number(low)
        or not finite_number(high)
        or float(low) > float(high)
    ):
        return primary, False, f"primary metric {key} has a malformed confidence interval"

    favors = str(getattr(primary, "favors", "") or "").upper()
    if favors not in _RECOGNIZED_FAVORS:
        return primary, False, f"primary metric {key} has an unrecognised favors value"
    direction = enum_value(getattr(primary, "direction", None))
    if favors in {"CANDIDATE", "BASELINE"}:
        if direction not in {"HIGHER_IS_BETTER", "LOWER_IS_BETTER"}:
            return primary, False, (
                f"primary metric {key} favors {favors} but declares no usable direction"
            )
        baseline = getattr(primary, "baseline_mean", None)
        candidate = getattr(primary, "candidate_mean", None)
        difference = getattr(primary, "difference", None)
        if baseline is not None and candidate is not None:
            if not finite_number(baseline) or not finite_number(candidate):
                return primary, False, f"primary metric {key} has malformed means"
            candidate_better = (
                float(candidate) > float(baseline)
                if direction == "HIGHER_IS_BETTER"
                else float(candidate) < float(baseline)
            )
        elif difference is not None:
            if not finite_number(difference):
                return primary, False, f"primary metric {key} has a malformed difference"
            candidate_better = (
                float(difference) > 0
                if direction == "HIGHER_IS_BETTER"
                else float(difference) < 0
            )
        else:
            candidate_better = None
        if candidate_better is not None:
            expected = "CANDIDATE" if candidate_better else "BASELINE"
            if favors != expected:
                return primary, False, (
                    f"primary metric {key} favors {favors} but its statistics favor {expected}"
                )

    return (
        primary,
        True,
        f"primary metric {key}: p={chosen}, CI=[{low}, {high}]",
    )


def _declared_alpha(comparison: Any, primary: Any) -> tuple[float, str]:
    """Return the declared significance level, or a documented default."""
    details = as_dict(getattr(comparison, "details", None))
    raw = details.get("alpha", getattr(primary, "alpha", None))
    if finite_number(raw) and 0.0 < float(raw) < 1.0:
        return float(raw), "declared on the comparison"
    return DEFAULT_ALPHA, f"default alpha={DEFAULT_ALPHA}; no alpha was declared on the comparison"


def _chosen_p_value(primary: Any) -> float | None:
    """Prefer an adjusted p-value, fall back to the raw one; never invent one."""
    for value in (getattr(primary, "p_value_adjusted", None), getattr(primary, "p_value", None)):
        if finite_number(value) and 0.0 <= float(value) <= 1.0:
            return float(value)
    return None


def verdict_statistics_support(comparison: Any) -> tuple[bool, str]:
    """Do the primary-metric statistics actually support the declared verdict?

    Structural validity (see :func:`evaluate_primary_metric`) is necessary but
    not sufficient: a non-significant p-value, an interval spanning zero, or an
    absent/indeterminate direction cannot answer a directional question. A
    comparison that does not declare ``alpha`` is judged against the documented
    :data:`DEFAULT_ALPHA`, and that fact is always included in the returned
    note. Parity verdicts additionally require a significant p-value, a
    verdict-consistent direction and an exact satisfied margin (see
    :func:`margin_evidence`); they are never accepted vacuously.
    """
    primary, usable, note = evaluate_primary_metric(comparison)
    if primary is None or not usable:
        return False, note
    verdict = enum_value(getattr(comparison, "verdict", None))

    alpha, alpha_source = _declared_alpha(comparison, primary)
    p_value = _chosen_p_value(primary)
    key = str(getattr(primary, "key", "primary"))
    favors = str(getattr(primary, "favors", "") or "").upper()
    low = getattr(primary, "ci_low", None)
    high = getattr(primary, "ci_high", None)
    direction = enum_value(getattr(primary, "direction", None))

    if verdict in {"CANDIDATE_NON_INFERIOR", "NO_DETECTABLE_DIFFERENCE"}:
        # Parity is not the absence of a refutation: it needs a significant
        # result, a non-contradictory direction and an exact satisfied margin.
        problems: list[str] = []
        if p_value is None:
            problems.append(f"primary metric {key} has no usable p-value")
        elif p_value >= alpha:
            problems.append(
                f"primary metric {key} p={p_value} is not below alpha={alpha} "
                f"({alpha_source}); the result is null or unsupported"
            )
        if favors == "BASELINE":
            problems.append(
                f"primary metric {key} favors BASELINE, which contradicts {verdict}"
            )
        elif verdict == "NO_DETECTABLE_DIFFERENCE" and favors == "CANDIDATE":
            problems.append(
                f"primary metric {key} favors CANDIDATE, which contradicts a "
                "no-detectable-difference verdict"
            )
        elif (
            verdict == "CANDIDATE_NON_INFERIOR"
            and favors not in _INDETERMINATE_FAVORS
            and favors != "CANDIDATE"
        ):
            problems.append(
                f"primary metric {key} favors {favors}, which does not support {verdict}"
            )
        margin_ok, margin_note = margin_evidence(comparison)
        if not margin_ok:
            problems.append(margin_note)
        summary = (
            f"primary metric {key}: p={p_value}, alpha={alpha} ({alpha_source}), "
            f"favors={favors or 'UNDECLARED'}, CI=[{low}, {high}]; {margin_note}"
        )
        return (not problems), summary + ("; " + "; ".join(problems) if problems else "")

    if verdict not in _ADVANTAGE_FAVORS:
        return False, f"verdict {verdict} does not support a reproduced result"

    expected = _ADVANTAGE_FAVORS[verdict]
    problems = []
    if p_value is None:
        problems.append(f"primary metric {key} has no usable p-value")
    elif p_value >= alpha:
        problems.append(
            f"primary metric {key} p={p_value} is not below alpha={alpha} "
            f"({alpha_source}); the result is null or unsupported"
        )
    if favors in _INDETERMINATE_FAVORS:
        problems.append(f"primary metric {key} has indeterminate favors for {verdict}")
    elif favors != expected:
        problems.append(
            f"primary metric {key} favors {favors}, not {expected} as {verdict} requires"
        )

    if direction not in {"HIGHER_IS_BETTER", "LOWER_IS_BETTER"}:
        problems.append(f"primary metric {key} has no directional confidence interval")
        favorable = False
    else:
        low_ok = finite_number(low) and float(low) > 0
        high_ok = finite_number(high) and float(high) < 0
        favorable = (
            (low_ok if direction == "HIGHER_IS_BETTER" else high_ok)
            if expected == "CANDIDATE"
            else (high_ok if direction == "HIGHER_IS_BETTER" else low_ok)
        )
        if not favorable:
            problems.append(
                f"primary metric {key} CI=[{low}, {high}] includes zero or "
                f"points away from {verdict}"
            )

    summary = (
        f"primary metric {key}: p={p_value}, alpha={alpha} ({alpha_source}), "
        f"favors={favors or 'UNDECLARED'}, CI=[{low}, {high}]"
    )
    return (not problems), summary + ("; " + "; ".join(problems) if problems else "")


def margin_evidence(comparison: Any) -> tuple[bool, str]:
    """Require an exact, usable margin before a parity verdict can be a YES.

    ``within_margin=True`` alone is a boolean assertion, never evidence. A
    recorded, positive and finite margin is required, and the primary metric's
    confidence interval must lie entirely inside that margin: ``[-m, +m]`` for
    a two-sided equivalence verdict and ``[0, m]`` (or ``[-m, 0]`` for a
    lower-is-better metric) for a one-sided non-inferiority verdict. A
    negative, zero or non-finite margin is invalid, not a licence to claim
    parity.
    """
    primary, usable, note = evaluate_primary_metric(comparison)
    if primary is None or not usable:
        return False, note
    if getattr(primary, "within_margin", None) is not True:
        return False, "the primary metric has no satisfied within_margin result"

    details = as_dict(getattr(comparison, "details", None))
    value: float | None = None
    invalid: str | None = None
    for key in _MARGIN_VALUE_KEYS:
        if key not in details:
            continue
        raw = details[key]
        if finite_number(raw) and float(raw) > 0:
            value = float(raw)
            break
        invalid = f"declared margin {raw} via {key} is not a positive finite number"
    source = ""
    for key in _MARGIN_SOURCE_KEYS:
        text = str(details.get(key) or "").strip()
        if text:
            source = text
            break
    if value is None:
        pattern = re.compile(r"declared margin:\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)
        texts = [
            *iter_items(details.get("limitations")),
            *iter_items(getattr(comparison, "limitations", ())),
            *iter_items(getattr(primary, "notes", ())),
        ]
        for text in texts:
            match = pattern.search(str(text))
            if match:
                raw = float(match.group(1))
                if not (math.isfinite(raw) and raw > 0):
                    return False, f"declared margin {raw} in the comparison record is not positive"
                value = raw
                source = source or f"comparison record: {str(text)[:160]}"
                break
    if value is None:
        if invalid:
            return False, invalid
        return False, "no exact margin value is recorded for this parity verdict"

    low = getattr(primary, "ci_low", None)
    high = getattr(primary, "ci_high", None)
    verdict = enum_value(getattr(comparison, "verdict", None))
    direction = enum_value(getattr(primary, "direction", None))
    one_sided = verdict == "CANDIDATE_NON_INFERIOR" and direction in {
        "HIGHER_IS_BETTER",
        "LOWER_IS_BETTER",
    }
    if one_sided and direction == "HIGHER_IS_BETTER":
        interval_ok = finite_number(low) and finite_number(high) and float(low) >= 0 and float(high) <= value
        expected = f"[0, {value}]"
    elif one_sided:
        interval_ok = (
            finite_number(low) and finite_number(high) and float(high) <= 0 and float(low) >= -value
        )
        expected = f"[{-value}, 0]"
    else:
        interval_ok = (
            finite_number(low)
            and finite_number(high)
            and float(low) >= -value
            and float(high) <= value
        )
        expected = f"[-{value}, {value}]"
    if not interval_ok:
        return False, (
            f"primary metric CI=[{low}, {high}] is not entirely within declared "
            f"margin {expected}"
        )
    return True, (
        f"declared margin {value} from {source or 'the comparison record'}; "
        f"CI=[{low}, {high}] within {expected}"
    )


def comparison_supports_metric_evidence(
    comparison: Any, extra_sources: Sequence[Any] = ()
) -> bool:
    """A comparison is METRIC/BASELINE_PARITY evidence only when it is usable."""
    if comparison is None:
        return False
    if not bool(getattr(comparison, "comparable", False)):
        return False
    if enum_value(getattr(comparison, "verdict", None)) not in DECISIVE_VERDICTS:
        return False
    if fairness_violations(comparison):
        return False
    if imported_evidence(getattr(comparison, "details", None), comparison, *extra_sources):
        return False
    _, usable, _ = evaluate_primary_metric(comparison)
    if not usable:
        return False
    supported, _ = verdict_statistics_support(comparison)
    return supported


def _normalise_text(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value).strip().casefold()


def kinds_for_validation_method(method: str) -> tuple[ObligationKind, ...]:
    """Return ALL obligation kinds named by one validation-method string."""
    haystack = _normalise_text(method)
    matched: list[ObligationKind] = []
    for kind, keywords in METHOD_KEYWORD_TAXONOMY:
        if any(_normalise_text(keyword) in haystack for keyword in keywords):
            matched.append(kind)
    return tuple(dict.fromkeys(matched))


def kind_for_check_key(key: str) -> ObligationKind | None:
    """Map a declared ``invariant.<name>`` check key to an obligation kind."""
    match = _INVARIANT_KEY_RE.match(key.strip())
    if match is None:
        return None
    name = match.group(1)
    if name in INVARIANT_KEY_EXACT:
        return INVARIANT_KEY_EXACT[name]
    for prefix, kind in INVARIANT_KEY_PREFIXES:
        if name.startswith(prefix):
            return kind
    return None


def _check_keys_for_claim(
    claim: ResearchClaim, checks_by_id: Mapping[str, VerificationCheck]
) -> tuple[str, ...]:
    """Check keys a claim explicitly declared by id or by name."""
    keys: list[str] = []
    for validation_id in claim.validation_ids:
        check = checks_by_id.get(validation_id)
        if check is not None:
            keys.append(check.key)
    for method in claim.validation_methods:
        keys.extend(re.findall(r"invariant\.[a-z][a-z0-9_]*", method))
    return tuple(sorted(set(keys)))


def _claim_text(claim: ResearchClaim) -> str:
    return _normalise_text(
        " ".join(
            (
                claim.title,
                claim.statement,
                claim.implementation_requirement,
                " ".join(claim.validation_methods),
                " ".join(claim.expected_code_signals),
            )
        )
    )


def _merge(
    obligations: dict[tuple[str, ObligationKind], ValidationObligation],
    obligation: ValidationObligation,
) -> None:
    """Merge duplicates by (claim_id, kind); a required obligation stays required."""
    key = (obligation.claim_id, obligation.kind)
    existing = obligations.get(key)
    if existing is None:
        obligations[key] = obligation
        return
    obligations[key] = obligation.model_copy(
        update={
            "satisfying_check_keys": tuple(
                sorted(set(existing.satisfying_check_keys) | set(obligation.satisfying_check_keys))
            ),
            "required": existing.required or obligation.required,
            "source": "claim" if "claim" in (existing.source, obligation.source) else existing.source,
        }
    )


def obligations_for_claims(
    claims: Iterable[ResearchClaim], *, checks: Sequence[VerificationCheck] = ()
) -> list[ValidationObligation]:
    """Derive a deterministic, sorted obligation list from claims and checks.

    Sources, in order:

    1. ``claim.kind`` — PAPER_METHOD yields INVARIANT (and MASKING when the
       statement mentions a mask/causal mechanism); PAPER_RESULT yields METRIC
       and BASELINE_PARITY; REPRODUCTION_REQUIREMENT yields DATASET_PARITY,
       SEED and SPLIT_PARITY.
    2. ``claim.validation_methods`` — matched against :data:`METHOD_KEYWORD_TAXONOMY`.
    3. declared check keys — ``claim.validation_ids`` resolved through ``checks``
       plus any ``invariant.<name>`` tokens in the validation methods.
    4. un-attributed ``invariant.<name>`` check keys — :data:`INVARIANT_KEY_EXACT`
       and :data:`INVARIANT_KEY_PREFIXES`; these are attributed to the claim that
       declared them when there is exactly one such claim, otherwise to a
       synthetic ``check:<key>`` id with ``source="check"``.

    No fuzzy or model-assisted matching is performed, and the result is
    independent of input ordering. An obligation whose declared keys do not
    match its kind keeps an empty ``satisfying_check_keys`` tuple: it stays
    unsatisfied rather than falling back to unrelated declared keys.
    """
    claim_list = sorted(claims, key=lambda item: item.id)
    check_list = sorted(checks, key=lambda item: item.key)
    checks_by_id = {check.id: check for check in check_list}
    check_key_kinds = {
        check.key: kind
        for check in check_list
        if (kind := kind_for_check_key(check.key)) is not None
    }
    merged: dict[tuple[str, ObligationKind], ValidationObligation] = {}

    for claim in claim_list:
        text = _claim_text(claim)
        declared_keys = _check_keys_for_claim(claim, checks_by_id)
        required: list[tuple[ObligationKind, str]] = []

        if claim.kind is ClaimKind.PAPER_METHOD:
            if any(token in text for token in ("mask", "causal", "look ahead", "autoregressive")):
                required.append(("MASKING", "the paper method states a masking/causal mechanism"))
            required.append(("INVARIANT", "a paper method must satisfy its defining invariant"))
        elif claim.kind is ClaimKind.PAPER_RESULT:
            required.append(("METRIC", "a paper result asserts a measured metric"))
            required.append(("BASELINE_PARITY", "a paper result is a comparison with a baseline"))
        elif claim.kind is ClaimKind.REPRODUCTION_REQUIREMENT:
            required.append(("DATASET_PARITY", "a reproduction requirement pins the dataset"))
            required.append(("SEED", "a reproduction requirement pins random seeds"))
            required.append(("SPLIT_PARITY", "a reproduction requirement pins the split"))

        for method in claim.validation_methods:
            for kind in kinds_for_validation_method(method):
                required.append((kind, f"declared validation method: {method}"))

        for kind, description in required:
            satisfying = tuple(
                key for key in declared_keys if check_key_kinds.get(key) == kind
            )
            # No fallback to unrelated declared keys: an obligation with no
            # matching declared evidence stays unsatisfied (a named gap).
            _merge(
                merged,
                ValidationObligation(
                    id=f"obl:{claim.id}:{kind}",
                    kind=kind,
                    claim_id=claim.id,
                    description=description[:500],
                    source="claim",
                    satisfying_check_keys=satisfying,
                    required=True,
                ),
            )

    # Checks that were declared but not attributed to any claim still produce
    # obligations: a declared invariant check is a promise to demonstrate it.
    for check in check_list:
        kind = check_key_kinds.get(check.key)
        if kind is None:
            continue
        attributed = [
            claim
            for claim in claim_list
            if check.id in claim.validation_ids or check.key in _check_keys_for_claim(claim, checks_by_id)
        ]
        claim_id = attributed[0].id if len(attributed) == 1 else f"check:{check.key}"
        _merge(
            merged,
            ValidationObligation(
                id=f"obl:{claim_id}:{kind}",
                kind=kind,
                claim_id=claim_id,
                description=f"declared invariant check {check.key} must pass",
                source="claim" if len(attributed) == 1 else "check",
                satisfying_check_keys=(check.key,),
                required=check.required,
            ),
        )

    return sorted(merged.values(), key=lambda item: item.id)


def coverage(
    obligations: Iterable[ValidationObligation],
    checks: Sequence[VerificationCheck],
    comparison: Any | None = None,
) -> dict[str, Any]:
    """Report, per obligation, the passing checks or comparison that cover it.

    ``evidence`` entries are record ids: a :class:`VerificationCheck.key` for a
    deterministically checked obligation, or an
    :class:`~mayaworkbench.domain.models.ExperimentComparison.id` for an
    experimentally observed METRIC/BASELINE_PARITY obligation. A comparison is
    counted only with a decisive verdict, usable primary statistics, no fairness
    violations and no imported runs. Nothing is inferred from a model summary,
    a human note, an unexecuted check or a malformed payload.
    """
    # A check key is an identity, not a last-write-wins dictionary slot. Group
    # every record first so a PASSED duplicate can never mask an earlier FAILED,
    # PARTIAL, UNVERIFIED or non-deterministic record for the same key.
    checks_by_key: dict[str, list[VerificationCheck]] = {}
    for check in checks:
        checks_by_key.setdefault(check.key, []).append(check)
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    conflicts: list[str] = []
    for key, group in sorted(checks_by_key.items()):
        if len(group) < 2:
            continue
        outcomes = sorted({enum_value(item.outcome) for item in group})
        conflicts.append(
            f"duplicate check key {key} has {len(group)} records with outcomes "
            f"{outcomes}; a passing duplicate cannot mask a failing one"
        )

    comparison_support = comparison_supports_metric_evidence(
        comparison, extra_sources=[check.details for check in checks]
    )

    for obligation in sorted(obligations, key=lambda item: item.id):
        candidate_keys = set(obligation.satisfying_check_keys)
        # A declared invariant check of the same kind is evidence even when the
        # claim did not explicitly list its id -- but only from a deterministic
        # kind. Model/human checks never cover an obligation.
        for check in checks:
            if (
                kind_for_check_key(check.key) == obligation.kind
                and is_deterministic_check(check)
            ):
                candidate_keys.add(check.key)

        evidence: list[str] = []
        for key in sorted(candidate_keys):
            group = checks_by_key.get(key, [])
            if group and all(
                item.outcome is CheckOutcome.PASSED
                and is_deterministic_check(item)
                and not imported_evidence(getattr(item, "details", None))
                for item in group
            ):
                evidence.append(key)

        if obligation.kind in {"METRIC", "BASELINE_PARITY"} and comparison_support:
            comparison_id = str(getattr(comparison, "id", ""))
            if comparison_id:
                evidence.append(comparison_id)

        evidence = list(dict.fromkeys(evidence))
        items.append(
            {
                "id": obligation.id,
                "covered": bool(evidence),
                "evidence": evidence,
            }
        )
        if obligation.required and not evidence:
            missing.append(obligation.id)

    return {
        "obligations": items,
        "missing_required": sorted(missing),
        "key_conflicts": conflicts,
    }


__all__ = [
    "ObligationKind",
    "ALL_OBLIGATION_KINDS",
    "METHOD_KEYWORD_TAXONOMY",
    "INVARIANT_KEY_EXACT",
    "INVARIANT_KEY_PREFIXES",
    "DETERMINISTIC_EVIDENCE_KINDS",
    "DETERMINISTIC_EVIDENCE_METHODS",
    "is_deterministic_check",
    "has_external_provenance",
    "DECISIVE_VERDICTS",
    "ValidationObligation",
    "as_dict",
    "malformed_notes",
    "enum_value",
    "iter_items",
    "finite_number",
    "fairness_violations",
    "imported_evidence",
    "evaluate_primary_metric",
    "verdict_statistics_support",
    "margin_evidence",
    "DEFAULT_ALPHA",
    "comparison_supports_metric_evidence",
    "kinds_for_validation_method",
    "kind_for_check_key",
    "obligations_for_claims",
    "coverage",
]
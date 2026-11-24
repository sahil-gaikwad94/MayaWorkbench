"""Held-out R2I corpus contract, agreement statistics and grading guide.

An internal seed corpus can be overfit by implementation rules. A corpus may be
reported as *held-out* only when it carries two independent annotator labels per
case and a recorded adjudication. This module enforces that contract and
computes inter-rater agreement; it never invents expert labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from mayaworkbench.errors import UsageError

SCHEMA_VERSION = "r2i-grading/v1"
MIN_ANNOTATORS = 2
MIN_HELD_OUT_CASES = 50


@dataclass(frozen=True)
class AnnotatorLabel:
    annotator_id: str
    claim_id: str
    expected_status: str
    source_span: str = ""

    def __post_init__(self) -> None:
        if not self.annotator_id or not self.claim_id or not self.expected_status:
            raise UsageError("an annotator label requires id, claim_id and expected_status")


@dataclass(frozen=True)
class HoldoutCase:
    case_id: str
    labels: tuple[AnnotatorLabel, ...]
    adjudicated_status: str | None = None
    adjudicator: str | None = None
    adjudication_note: str = ""

    def is_adjudicated(self) -> bool:
        return bool(self.adjudicated_status and self.adjudicator)


def validate_holdout_corpus(
    cases: Iterable[HoldoutCase], *, minimum: int = MIN_HELD_OUT_CASES
) -> dict[str, Any]:
    """Fail closed unless a corpus genuinely qualifies as held-out."""
    rows = list(cases)
    problems: list[str] = []
    if len(rows) < minimum:
        problems.append(
            f"held-out corpus needs at least {minimum} cases, found {len(rows)}"
        )
    for case in rows:
        annotators = {label.annotator_id for label in case.labels}
        if len(annotators) < MIN_ANNOTATORS:
            problems.append(
                f"{case.case_id}: needs at least {MIN_ANNOTATORS} independent annotators, "
                f"found {sorted(annotators)}"
            )
        if not case.is_adjudicated():
            problems.append(f"{case.case_id}: no adjudicated label")
    return {
        "schema_version": SCHEMA_VERSION,
        "qualifies_as_held_out": not problems,
        "cases": len(rows),
        "problems": problems[:100],
        "note": (
            "A corpus without two annotators and adjudication stays an internal "
            "seed/development set; it must not be reported as an external benchmark."
        ),
    }


def cohens_kappa(
    first: Iterable[tuple[str, str]], second: Iterable[tuple[str, str]]
) -> float:
    """Inter-rater agreement on (item, category) pairs.

    Returns 0.0 with no items rather than claiming perfect agreement from no
    data. Categories are closed strings; both raters must label the same items.
    """
    a = dict(first)
    b = dict(second)
    items = sorted(set(a) & set(b))
    if not items:
        return 0.0
    categories = sorted({a[item] for item in items} | {b[item] for item in items})
    observed = sum(1 for item in items if a[item] == b[item]) / len(items)
    chance = sum(
        (sum(1 for item in items if a[item] == category) / len(items))
        * (sum(1 for item in items if b[item] == category) / len(items))
        for category in categories
    )
    if chance >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return round((observed - chance) / (1 - chance), 4)


def adjudicate(
    case: HoldoutCase, *, adjudicator: str, status: str, note: str
) -> HoldoutCase:
    """Record an independent adjudication without altering raw labels."""
    if not adjudicator or not status:
        raise UsageError("adjudication requires an adjudicator and a final status")
    return HoldoutCase(
        case_id=case.case_id,
        labels=case.labels,
        adjudicated_status=status,
        adjudicator=adjudicator,
        adjudication_note=note,
    )


def grading_guide_markdown() -> str:
    """The published metric/protocol guide independent graders must follow."""
    return """# R2I held-out grading guide (v1)

## What is graded
1. **Claim-span grounding** -- precision/recall/F1 of extracted claims against
   exact source spans, with the quoted fragment recorded.
2. **Claim type accuracy** -- method / result / requirement / inference /
   validation claims are distinct; a blended accuracy is not reported.
3. **Claim-to-code mapping** -- precision/recall of returned code locations;
   `ABSENT` counts as a false absence when a relevant definition exists.
4. **Uncertainty calibration** -- `UNCERTAIN`/`NOT_CHECKED` must track real
   ambiguity; confidence is not a substitute for a source span.
5. **Obligation coverage** -- required validations identified from the claim.
6. **Reproduction tier** -- code-found / implemented / validated / reproduced,
   assigned by the backend from evidence, never by the model.

## Protocol
- Each task is labelled by two independent annotators before the run.
- Disagreements are adjudicated by a third reviewer with a written rationale.
- Inter-rater agreement (Cohen's kappa) is published per metric family.
- Corpus hashes, annotator counts and adjudication records are versioned.
- No benchmark rule may be derived from held-out gold labels.
- Until the two-annotator/adjudication contract is met, a corpus is an internal
  seed/development set and must be described that way in every report.

## Scoring
Report per-family rates with Wilson 95% confidence intervals, and the false
absence rate separately. Do not publish one unqualified accuracy figure.
"""

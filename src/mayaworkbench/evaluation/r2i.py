"""Offline R2I (research-to-implementation) evaluation harness.

This module measures two independent inputs to a reproducibility report:

* **claim extraction** — did the system recover the claims stated in a paper,
  with a verbatim source span for each one?
* **claim-to-code mapping** — did it map each claim to the right code, and did
  it keep `UNCERTAIN` / `NOT_CHECKED` where the evidence is genuinely absent?

Everything here is deterministic and offline: there is no model call, no network
access and no hidden ground truth. Gold labels are explicit input data. Metrics
are computed from the predictions actually supplied; a vacuous gold corpus is
rejected rather than scored as 100%.

Matching rules (documented because they define the metric):

* A predicted claim is matched to a gold claim when the token Jaccard similarity
  of their normalized statements is at least ``threshold`` (default 0.6).
  Normalization uses :func:`normalize_quote`; tokens are ``\\w+`` matches in
  lower case; comparison uses sets of tokens. Candidate pairs are sorted by
  descending similarity, then prediction index, then gold index, and accepted
  greedily so each prediction and each gold label is used at most once.
* A matched prediction is citation-valid only when its source location's
  ``quote_hash`` (SHA-256 of the normalized quote, per
  :func:`mayaworkbench.research.passages.quote_hash`) equals the hash of the
  gold claim's verbatim ``source_quote``. A prediction with no citation is
  invalid. A gold claim with no ``source_quote`` imposes no citation requirement.
* A mapping is a true positive only when the predicted status is *compatible*
  with the gold status **and** every relevant path appears in its verified code
  evidence. Compatibility: ``PRESENT`` satisfies only ``PRESENT``; ``PARTIAL``
  satisfies ``PARTIAL`` or ``PRESENT``; uncertain gold labels (and gold
  ``UNCERTAIN`` / ``NOT_CHECKED``) require ``UNCERTAIN`` or ``NOT_CHECKED``;
  all other statuses must match exactly. A wrong path therefore prevents a TP
  even when the status string is correct.
"""

from __future__ import annotations

import importlib.resources as resources
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mayaworkbench.domain.enums import ClaimKind, ClaimMappingStatus
from mayaworkbench.research.passages import normalize_quote, quote_hash

DEFAULT_STATEMENT_JACCARD_THRESHOLD = 0.6

_KINDS = frozenset(item.value for item in ClaimKind)
_MAPPING_STATUSES = frozenset(item.value for item in ClaimMappingStatus)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_UNCERTAIN_STATUSES = frozenset({"UNCERTAIN", "NOT_CHECKED"})

_SEED_PACKAGE = "mayaworkbench.evaluation"
_SEED_RESOURCE = "gold/causal_attention_seed.json"


# --------------------------------------------------------------- gold schema
class _StrictModel(BaseModel):
    """Gold data is authored by hand: reject silently misspelled fields."""

    model_config = ConfigDict(extra="forbid")


class GoldClaimLabel(_StrictModel):
    """One expected claim, described by exact fragments of the paper."""

    id: str = Field(min_length=1)
    statement_fragments: list[str] = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_quote: str | None = None
    required: bool = True

    @field_validator("statement_fragments")
    @classmethod
    def _fragments_non_empty(cls, value: list[str]) -> list[str]:
        if not all(fragment.strip() for fragment in value):
            raise ValueError("statement_fragments must not contain blank strings")
        return value

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _KINDS:
            raise ValueError(f"unknown gold claim kind {value!r}; expected one of {sorted(_KINDS)}")
        return normalized

    @property
    def statement(self) -> str:
        """The normalized statement used for matching predictions."""
        return normalize_quote(" ".join(self.statement_fragments))


class GoldMappingLabel(_StrictModel):
    """The expected claim-to-code verdict for one claim."""

    claim_id: str = Field(min_length=1)
    expected_status: str = Field(min_length=1)
    relevant_paths: list[str] = Field(default_factory=list)
    uncertain: bool = False

    @field_validator("expected_status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _MAPPING_STATUSES:
            raise ValueError(
                f"unknown expected mapping status {value!r}; expected one of {sorted(_MAPPING_STATUSES)}"
            )
        return normalized


class GoldCase(_StrictModel):
    """One explicitly labelled evaluation case."""

    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    paper_excerpt: str = ""
    claims: list[GoldClaimLabel] = Field(default_factory=list)
    mappings: list[GoldMappingLabel] = Field(default_factory=list)
    provenance: str = Field(min_length=1)
    is_seed_corpus: bool = True

    @field_validator("case_id")
    @classmethod
    def _case_id_shape(cls, value: str) -> str:
        return value.strip() or value


def load_gold_case(path: str | Path) -> GoldCase:
    """Load and validate one gold case from a JSON file.

    Raises ``ValueError`` with the offending path for missing files, malformed
    JSON and schema violations (including unknown fields).
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read gold case {target}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"gold case {target} is not valid JSON: {exc}") from exc
    try:
        return GoldCase.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"gold case {target} has an invalid schema: {exc}") from exc


def bundled_seed_case() -> GoldCase:
    """Load the checked-in seed corpus through importlib.resources.

    The seed corpus is built from this repository's own paper/code fixtures. It
    is a harness self-test, never an external benchmark.
    """
    try:
        text = (
            resources.files(_SEED_PACKAGE)
            .joinpath("gold")
            .joinpath("causal_attention_seed.json")
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        raise ValueError(
            f"bundled seed corpus {_SEED_RESOURCE} is unavailable; reinstall mayaworkbench"
        ) from exc
    try:
        return GoldCase.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(f"bundled seed corpus {_SEED_RESOURCE} is malformed: {exc}") from exc


def load_gold_corpus(path: str | Path) -> list[GoldCase]:
    """Load one case, a list of cases, a ``{"cases": [...]}`` file or a directory."""
    target = Path(path).expanduser()
    if target.is_dir():
        files = sorted(target.glob("*.json"))
        if not files:
            raise ValueError(f"gold corpus directory {target} contains no .json files")
        return [load_gold_case(item) for item in files]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read gold corpus {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"gold corpus {target} is not valid JSON: {exc}") from exc
    if isinstance(payload, Mapping) and "cases" in payload:
        payload = payload["cases"]
    if isinstance(payload, Mapping):
        return [load_gold_case(target)]
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"gold corpus {target} must contain a case, a non-empty list of cases or 'cases'")
    cases: list[GoldCase] = []
    for index, item in enumerate(payload):
        try:
            cases.append(GoldCase.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"gold case #{index} in {target} has an invalid schema: {exc}") from exc
    return cases


# -------------------------------------------------------- prediction adapters
@dataclass(frozen=True)
class PredictedClaim:
    """A normalized claim from an exported CLAIM_SET artifact."""

    id: str
    statement: str
    kind: str = ""
    quote: str | None = None
    quote_hash: str | None = None

    @property
    def effective_quote_hash(self) -> str | None:
        """The authoritative hash of this prediction's cited span, if any."""
        if self.quote_hash:
            return self.quote_hash
        return quote_hash(self.quote) if self.quote else None


@dataclass(frozen=True)
class PredictedMapping:
    """A normalized mapping row from an exported CLAIM_MATRIX artifact."""

    claim_id: str
    status: str
    paths: tuple[str, ...] = ()
    verified: bool = True


def _enum_value(value: Any) -> str:
    """Read an enum serialized as a string, a ``{"value": ...}`` dict or a name."""
    if isinstance(value, str):
        return value.strip().upper()
    if isinstance(value, Mapping):
        for key in ("value", "name"):
            if key in value:
                return _enum_value(value[key])
    if value is None:
        return ""
    return str(value).strip().upper()


def _records(payload: Any, list_keys: Sequence[str], *, label: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in list_keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    return value
                raise ValueError(f"{label} payload field {key!r} must be a list")
    raise ValueError(f"{label} payload must be a list or an object with one of {list(list_keys)}")


def claims_from_artifact(payload: Any) -> list[PredictedClaim]:
    """Adapt a CLAIM_SET JSON export (or a bare list of claims).

    Handles ``model_dump(mode="json")`` enums-as-strings, both ``kind`` and the
    legacy ``claim_type``, and quotes from ``source_locations`` (the shape
    persisted by the research service) or top-level ``quote`` fields.
    """
    records = _records(payload, ("claims", "predictions"), label="claim")
    claims: list[PredictedClaim] = []
    for index, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise ValueError(f"claim #{index} is not a JSON object")
        locations = item.get("source_locations") or item.get("citations") or []
        if isinstance(locations, Mapping):
            locations = [locations]
        if not isinstance(locations, list):
            raise ValueError(f"claim #{index} source_locations must be a list")
        quote: str | None = None
        explicit_hash: str | None = None
        for location in locations:
            if not isinstance(location, Mapping):
                continue
            candidate_quote = location.get("quote") or location.get("text") or ""
            candidate_hash = location.get("quote_hash") or ""
            if not quote and candidate_quote:
                quote = str(candidate_quote)
            if not explicit_hash and candidate_hash:
                explicit_hash = str(candidate_hash)
            if quote and explicit_hash:
                break
        if not quote:
            top_quote = item.get("source_quote") or item.get("quote")
            quote = str(top_quote) if top_quote else None
        if not explicit_hash:
            top_hash = item.get("quote_hash")
            explicit_hash = str(top_hash) if top_hash else None
        claim_id = item.get("id") or item.get("claim_id")
        statement = item.get("statement") or item.get("title") or ""
        claims.append(
            PredictedClaim(
                id=str(claim_id) if claim_id else f"pred_claim_{index}",
                statement=str(statement),
                kind=_enum_value(item.get("kind") or item.get("claim_type") or ""),
                quote=quote,
                quote_hash=explicit_hash,
            )
        )
    return claims


def mappings_from_artifact(payload: Any) -> list[PredictedMapping]:
    """Adapt a CLAIM_MATRIX JSON export (or a bare list of rows).

    Matrix rows use ``mapping_status`` and nested
    ``evidence: [{"evidence": {"path": ...}, "verified": bool}]``. Bare
    ``status`` and flat ``path`` rows are accepted for offline callers. When a
    row carries explicit ``verified`` flags, only verified evidence contributes
    paths; when no flag is present the paths are taken at face value.
    """
    records = _records(payload, ("rows", "claims", "mappings"), label="mapping")
    mappings: list[PredictedMapping] = []
    for index, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise ValueError(f"mapping row #{index} is not a JSON object")
        raw_status = item.get("status", item.get("mapping_status"))
        status = _enum_value(raw_status) if raw_status not in (None, "") else "NOT_CHECKED"
        evidence = item.get("evidence") or []
        if isinstance(evidence, Mapping):
            evidence = [evidence]
        if not isinstance(evidence, list):
            raise ValueError(f"mapping row #{index} evidence must be a list")
        has_flags = any(
            isinstance(entry, Mapping) and "verified" in entry for entry in evidence
        )
        paths: list[str] = []
        for entry in evidence:
            if not isinstance(entry, Mapping):
                continue
            if has_flags and entry.get("verified") is not True:
                continue
            location = entry.get("evidence")
            if isinstance(location, Mapping):
                raw_path = location.get("path")
            else:
                raw_path = entry.get("path") or entry.get("relevant_path")
            if raw_path:
                paths.append(str(raw_path))
        if not paths and not has_flags:
            # Flat offline rows: {"claim_id": ..., "status": ..., "path": ...}
            flat_path = item.get("path") or item.get("relevant_path")
            if flat_path:
                paths.append(str(flat_path))
        claim_id = item.get("claim_id") or item.get("id")
        mappings.append(
            PredictedMapping(
                claim_id=str(claim_id) if claim_id else f"pred_mapping_{index}",
                status=status,
                paths=tuple(paths),
                verified=bool(item.get("verified", True)),
            )
        )
    return mappings


# ------------------------------------------------------------------- metrics
@dataclass(frozen=True)
class Counts:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class CitationValidity:
    valid: int
    total: int
    rate: float
    invalid_claim_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractionMetrics:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    by_kind: dict[str, Counts]
    span_citation_validity: CitationValidity
    fabricated_claims: list[str]
    threshold: float = DEFAULT_STATEMENT_JACCARD_THRESHOLD


@dataclass(frozen=True)
class MappingMetrics:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    uncertainty_correct: int
    uncertainty_total: int
    uncertainty_rate: float
    path_hits: dict[str, list[str]]
    path_hits_valid: int
    path_hits_total: int
    path_hit_rate: float


@dataclass(frozen=True)
class CaseEvaluation:
    case_id: str
    title: str
    provenance: str
    is_seed_corpus: bool
    gold_claim_count: int
    gold_mapping_count: int
    predicted_claim_count: int
    predicted_mapping_count: int
    extraction: ExtractionMetrics
    mapping: MappingMetrics


@dataclass(frozen=True)
class CorpusEvaluation:
    case_count: int
    corpus_is_seed: bool
    cases: list[CaseEvaluation]
    micro_extraction: ExtractionMetrics
    macro_extraction: Counts
    micro_mapping: MappingMetrics
    macro_mapping: Counts
    micro_by_kind: dict[str, Counts]


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Precision, recall and F1; a zero denominator is 0.0, never 1.0."""
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _counts(tp: int, fp: int, fn: int) -> Counts:
    precision, recall, f1 = _prf(tp, fp, fn)
    return Counts(tp=tp, fp=fp, fn=fn, precision=precision, recall=recall, f1=f1)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(normalize_quote(text).lower()))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _greedy_matches(
    predictions: Sequence[PredictedClaim], case: GoldCase, threshold: float
) -> dict[int, int]:
    """Map prediction index to gold index using deterministic best-first matching."""
    gold_tokens = [_tokens(label.statement) for label in case.claims]
    candidate_pairs: list[tuple[float, int, int]] = []
    for pred_index, prediction in enumerate(predictions):
        pred_tokens = _tokens(prediction.statement)
        for gold_index, tokens in enumerate(gold_tokens):
            similarity = _jaccard(pred_tokens, tokens)
            if similarity >= threshold:
                candidate_pairs.append((similarity, pred_index, gold_index))
    candidate_pairs.sort(key=lambda pair: (-pair[0], pair[1], pair[2]))
    matches: dict[int, int] = {}
    used_gold: set[int] = set()
    for _similarity, pred_index, gold_index in candidate_pairs:
        if pred_index in matches or gold_index in used_gold:
            continue
        matches[pred_index] = gold_index
        used_gold.add(gold_index)
    return matches


def evaluate_extraction(
    case: GoldCase,
    predicted_claims: Sequence[PredictedClaim],
    *,
    threshold: float = DEFAULT_STATEMENT_JACCARD_THRESHOLD,
) -> ExtractionMetrics:
    """Score claim extraction against the gold labels of ``case``.

    ``fp`` counts predictions matching no gold label, ``fn`` counts gold labels
    with no matching prediction. Citation validity is measured over *all*
    predictions: a matched prediction is valid only if it cites the exact
    normalized source span required by its gold label; an unmatched prediction
    is always invalid.
    """
    if not case.claims:
        raise ValueError(
            f"gold case {case.case_id!r} has no claim labels; refusing to report vacuous extraction metrics"
        )
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"statement Jaccard threshold must be in (0, 1], got {threshold!r}")

    predictions = list(predicted_claims)
    matches = _greedy_matches(predictions, case, threshold)
    tp = len(matches)
    fp = len(predictions) - tp
    fn = len(case.claims) - tp
    precision, recall, f1 = _prf(tp, fp, fn)

    kinds = sorted({label.kind for label in case.claims} | {item.kind for item in predictions if item.kind})
    by_kind: dict[str, Counts] = {}
    matched_gold = set(matches.values())
    for kind in kinds:
        kind_tp = sum(1 for gold_index in matches.values() if case.claims[gold_index].kind == kind)
        kind_fp = sum(
            1
            for index, item in enumerate(predictions)
            if index not in matches and item.kind == kind
        )
        kind_fn = sum(
            1
            for gold_index, label in enumerate(case.claims)
            if gold_index not in matched_gold and label.kind == kind
        )
        by_kind[kind] = _counts(kind_tp, kind_fp, kind_fn)

    valid = 0
    invalid_ids: list[str] = []
    fabricated: list[str] = []
    for index, prediction in enumerate(predictions):
        gold_index = matches.get(index)
        if gold_index is None:
            invalid_ids.append(prediction.id)
            fabricated.append(prediction.id)
            continue
        label = case.claims[gold_index]
        if label.source_quote is None:
            valid += 1
            continue
        if prediction.effective_quote_hash == quote_hash(label.source_quote):
            valid += 1
        else:
            invalid_ids.append(prediction.id)

    total = len(predictions)
    return ExtractionMetrics(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        by_kind=by_kind,
        span_citation_validity=CitationValidity(
            valid=valid,
            total=total,
            rate=valid / total if total else 0.0,
            invalid_claim_ids=invalid_ids,
        ),
        fabricated_claims=fabricated,
        threshold=threshold,
    )


def _status_satisfies(predicted: str, expected: str, *, uncertain: bool = False) -> bool:
    """Status compatibility rule documented in the module docstring."""
    predicted = predicted.strip().upper()
    expected = expected.strip().upper()
    if uncertain or expected in _UNCERTAIN_STATUSES:
        return predicted in _UNCERTAIN_STATUSES
    if expected == "PRESENT":
        return predicted == "PRESENT"
    if expected == "PARTIAL":
        return predicted in {"PARTIAL", "PRESENT"}
    return predicted == expected


def _normalized_path(path: str) -> str:
    cleaned = str(path).replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _hit_paths(relevant: Sequence[str], predicted: Sequence[str]) -> list[str]:
    available = {_normalized_path(item) for item in predicted}
    return [item for item in relevant if _normalized_path(item) in available]


def evaluate_mapping(
    case: GoldCase, predicted_mappings: Sequence[PredictedMapping]
) -> MappingMetrics:
    """Score claim-to-code mappings, including path correctness and uncertainty."""
    if not case.mappings:
        raise ValueError(
            f"gold case {case.case_id!r} has no mapping labels; refusing to report vacuous mapping metrics"
        )

    predictions = list(predicted_mappings)
    by_claim: dict[str, list[tuple[int, PredictedMapping]]] = {}
    for index, prediction in enumerate(predictions):
        by_claim.setdefault(prediction.claim_id, []).append((index, prediction))

    tp = 0
    used_predictions: set[int] = set()
    uncertainty_correct = 0
    uncertainty_total = 0
    path_hits: dict[str, list[str]] = {}
    path_hits_valid = 0
    path_hits_total = 0

    for label in case.mappings:
        relevant = list(label.relevant_paths)
        path_hits_total += len(relevant)
        candidates = by_claim.get(label.claim_id, [])
        best_index: int | None = None
        best_status_ok = False
        best_hits: list[str] = []
        # Prefer the candidate with a compatible status and the most relevant
        # paths; ties break on the lower (earlier) artifact index.
        for index, prediction in candidates:
            hits = _hit_paths(relevant, prediction.paths)
            status_ok = _status_satisfies(
                prediction.status, label.expected_status, uncertain=label.uncertain
            )
            if best_index is None or (status_ok, len(hits)) > (best_status_ok, len(best_hits)):
                best_index, best_status_ok, best_hits = index, status_ok, hits
        if relevant:
            path_hits[label.claim_id] = best_hits
        path_hits_valid += len(best_hits)

        if label.uncertain or label.expected_status in _UNCERTAIN_STATUSES:
            uncertainty_total += 1
            if best_status_ok and best_index is not None:
                uncertainty_correct += 1

        if best_index is not None and best_status_ok and len(best_hits) == len(relevant):
            tp += 1
            used_predictions.add(best_index)

    fp = len(predictions) - tp
    fn = len(case.mappings) - tp
    precision, recall, f1 = _prf(tp, fp, fn)
    return MappingMetrics(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        uncertainty_correct=uncertainty_correct,
        uncertainty_total=uncertainty_total,
        uncertainty_rate=uncertainty_correct / uncertainty_total if uncertainty_total else 0.0,
        path_hits=path_hits,
        path_hits_valid=path_hits_valid,
        path_hits_total=path_hits_total,
        path_hit_rate=path_hits_valid / path_hits_total if path_hits_total else 0.0,
    )


def evaluate_case(
    case: GoldCase,
    *,
    claims: Sequence[PredictedClaim],
    mappings: Sequence[PredictedMapping],
    threshold: float = DEFAULT_STATEMENT_JACCARD_THRESHOLD,
) -> CaseEvaluation:
    """Evaluate both dimensions for one gold case."""
    extraction = evaluate_extraction(case, claims, threshold=threshold)
    mapping = evaluate_mapping(case, mappings)
    return CaseEvaluation(
        case_id=case.case_id,
        title=case.title,
        provenance=case.provenance,
        is_seed_corpus=case.is_seed_corpus,
        gold_claim_count=len(case.claims),
        gold_mapping_count=len(case.mappings),
        predicted_claim_count=len(claims),
        predicted_mapping_count=len(mappings),
        extraction=extraction,
        mapping=mapping,
    )


def evaluate_corpus(cases: Sequence[tuple[GoldCase, CaseEvaluation]]) -> CorpusEvaluation:
    """Aggregate case evaluations with micro and macro averages.

    ``corpus_is_seed`` is true only for a non-empty corpus whose every gold case
    declares itself a seed; an empty corpus is never reported as a seed corpus.
    """
    pairs = list(cases)
    if not pairs:
        empty_counts = _counts(0, 0, 0)
        empty_citation = CitationValidity(valid=0, total=0, rate=0.0)
        return CorpusEvaluation(
            case_count=0,
            corpus_is_seed=False,
            cases=[],
            micro_extraction=ExtractionMetrics(
                tp=0, fp=0, fn=0, precision=0.0, recall=0.0, f1=0.0,
                by_kind={}, span_citation_validity=empty_citation, fabricated_claims=[],
            ),
            macro_extraction=empty_counts,
            micro_mapping=MappingMetrics(
                tp=0, fp=0, fn=0, precision=0.0, recall=0.0, f1=0.0,
                uncertainty_correct=0, uncertainty_total=0, uncertainty_rate=0.0,
                path_hits={}, path_hits_valid=0, path_hits_total=0, path_hit_rate=0.0,
            ),
            macro_mapping=empty_counts,
            micro_by_kind={},
        )

    case_evaluations = [evaluation for _case, evaluation in pairs]
    extraction_tp = sum(item.extraction.tp for item in case_evaluations)
    extraction_fp = sum(item.extraction.fp for item in case_evaluations)
    extraction_fn = sum(item.extraction.fn for item in case_evaluations)
    mapping_tp = sum(item.mapping.tp for item in case_evaluations)
    mapping_fp = sum(item.mapping.fp for item in case_evaluations)
    mapping_fn = sum(item.mapping.fn for item in case_evaluations)

    kind_totals: dict[str, list[int]] = {}
    for evaluation in case_evaluations:
        for kind, counts in evaluation.extraction.by_kind.items():
            totals = kind_totals.setdefault(kind, [0, 0, 0])
            totals[0] += counts.tp
            totals[1] += counts.fp
            totals[2] += counts.fn
    micro_by_kind = {
        kind: _counts(totals[0], totals[1], totals[2])
        for kind, totals in sorted(kind_totals.items())
    }

    citation_valid = sum(item.extraction.span_citation_validity.valid for item in case_evaluations)
    citation_total = sum(item.extraction.span_citation_validity.total for item in case_evaluations)
    invalid_ids = [
        claim_id
        for item in case_evaluations
        for claim_id in item.extraction.span_citation_validity.invalid_claim_ids
    ]
    fabricated = [
        claim_id
        for item in case_evaluations
        for claim_id in item.extraction.fabricated_claims
    ]
    path_hits: dict[str, list[str]] = {}
    for evaluation in case_evaluations:
        path_hits.update(evaluation.mapping.path_hits)

    def _macro(field_name: str) -> Counts:
        n = len(case_evaluations)
        precision = sum(getattr(item, field_name).precision for item in case_evaluations) / n
        recall = sum(getattr(item, field_name).recall for item in case_evaluations) / n
        f1 = sum(getattr(item, field_name).f1 for item in case_evaluations) / n
        return Counts(
            tp=sum(getattr(item, field_name).tp for item in case_evaluations),
            fp=sum(getattr(item, field_name).fp for item in case_evaluations),
            fn=sum(getattr(item, field_name).fn for item in case_evaluations),
            precision=precision,
            recall=recall,
            f1=f1,
        )

    uncertainty_correct = sum(item.mapping.uncertainty_correct for item in case_evaluations)
    uncertainty_total = sum(item.mapping.uncertainty_total for item in case_evaluations)
    path_hits_valid = sum(item.mapping.path_hits_valid for item in case_evaluations)
    path_hits_total = sum(item.mapping.path_hits_total for item in case_evaluations)
    extraction_precision, extraction_recall, extraction_f1 = _prf(
        extraction_tp, extraction_fp, extraction_fn
    )
    mapping_precision, mapping_recall, mapping_f1 = _prf(mapping_tp, mapping_fp, mapping_fn)

    return CorpusEvaluation(
        case_count=len(case_evaluations),
        corpus_is_seed=all(case.is_seed_corpus for case, _evaluation in pairs),
        cases=case_evaluations,
        micro_extraction=ExtractionMetrics(
            tp=extraction_tp,
            fp=extraction_fp,
            fn=extraction_fn,
            precision=extraction_precision,
            recall=extraction_recall,
            f1=extraction_f1,
            by_kind=micro_by_kind,
            span_citation_validity=CitationValidity(
                valid=citation_valid,
                total=citation_total,
                rate=citation_valid / citation_total if citation_total else 0.0,
                invalid_claim_ids=invalid_ids,
            ),
            fabricated_claims=fabricated,
        ),
        macro_extraction=_macro("extraction"),
        micro_mapping=MappingMetrics(
            tp=mapping_tp,
            fp=mapping_fp,
            fn=mapping_fn,
            precision=mapping_precision,
            recall=mapping_recall,
            f1=mapping_f1,
            uncertainty_correct=uncertainty_correct,
            uncertainty_total=uncertainty_total,
            uncertainty_rate=uncertainty_correct / uncertainty_total if uncertainty_total else 0.0,
            path_hits=path_hits,
            path_hits_valid=path_hits_valid,
            path_hits_total=path_hits_total,
            path_hit_rate=path_hits_valid / path_hits_total if path_hits_total else 0.0,
        ),
        macro_mapping=_macro("mapping"),
        micro_by_kind=micro_by_kind,
    )


# ------------------------------------------------------------------ rendering
def _fmt(value: float) -> str:
    return f"{value:.3f}"


def evaluation_payload(evaluation: CaseEvaluation | CorpusEvaluation) -> dict[str, Any]:
    """Return a JSON-serializable payload for ``maya eval r2i --json``."""
    payload = asdict(evaluation)
    if isinstance(evaluation, CaseEvaluation):
        payload["kind"] = "case"
        payload["corpus_is_seed"] = evaluation.is_seed_corpus
    else:
        payload["kind"] = "corpus"
    return payload


def render_markdown(evaluation: CaseEvaluation | CorpusEvaluation) -> str:
    """Render a compact, human-readable evaluation report."""
    lines: list[str] = []

    def counts_table(title: str, counts: Counts) -> None:
        lines.extend(
            [
                f"### {title}",
                "",
                "| metric | value |",
                "| --- | --- |",
                f"| true positives | {counts.tp} |",
                f"| false positives | {counts.fp} |",
                f"| false negatives | {counts.fn} |",
                f"| precision | {_fmt(counts.precision)} |",
                f"| recall | {_fmt(counts.recall)} |",
                f"| F1 | {_fmt(counts.f1)} |",
                "",
            ]
        )

    def extraction_details(metrics: ExtractionMetrics) -> None:
        counts_table("Claim extraction", metrics)
        citation = metrics.span_citation_validity
        lines.extend(
            [
                f"- citation validity: {citation.valid}/{citation.total} "
                f"({_fmt(citation.rate)})",
                f"- invalid citation claim ids: {', '.join(citation.invalid_claim_ids) or 'none'}",
                f"- fabricated claim ids: {', '.join(metrics.fabricated_claims) or 'none'}",
                f"- match threshold (statement Jaccard): {metrics.threshold}",
                "",
                "### Extraction by claim kind",
                "",
                "| kind | TP | FP | FN | precision | recall | F1 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for kind, counts in sorted(metrics.by_kind.items()):
            lines.append(
                f"| {kind} | {counts.tp} | {counts.fp} | {counts.fn} | "
                f"{_fmt(counts.precision)} | {_fmt(counts.recall)} | {_fmt(counts.f1)} |"
            )
        lines.append("")

    def mapping_details(metrics: MappingMetrics) -> None:
        counts_table("Claim-to-code mapping", metrics)
        lines.extend(
            [
                f"- uncertainty correct: {metrics.uncertainty_correct}/{metrics.uncertainty_total} "
                f"({_fmt(metrics.uncertainty_rate)})",
                f"- relevant paths hit: {metrics.path_hits_valid}/{metrics.path_hits_total} "
                f"({_fmt(metrics.path_hit_rate)})",
                "",
            ]
        )

    if isinstance(evaluation, CaseEvaluation):
        lines.extend(
            [
                f"# R2I evaluation — {evaluation.title}",
                "",
                f"- case: `{evaluation.case_id}`",
                f"- seed corpus: {'yes' if evaluation.is_seed_corpus else 'no'}",
                f"- provenance: {evaluation.provenance}",
                f"- gold claims / predictions: {evaluation.gold_claim_count} / "
                f"{evaluation.predicted_claim_count}",
                f"- gold mappings / predictions: {evaluation.gold_mapping_count} / "
                f"{evaluation.predicted_mapping_count}",
                "",
            ]
        )
        extraction_details(evaluation.extraction)
        mapping_details(evaluation.mapping)
        return "\n".join(lines).rstrip() + "\n"

    lines.extend(
        [
            "# R2I corpus evaluation",
            "",
            f"- cases: {evaluation.case_count}",
            f"- corpus is seed: {'yes' if evaluation.corpus_is_seed else 'no'}",
            "",
        ]
    )
    lines.append("## Micro averages (pooled counts)")
    lines.append("")
    extraction_details(evaluation.micro_extraction)
    lines.append("## Macro averages (mean of per-case scores)")
    lines.append("")
    counts_table("Extraction (macro)", evaluation.macro_extraction)
    counts_table("Mapping (macro)", evaluation.macro_mapping)
    mapping_details(evaluation.micro_mapping)
    lines.append("## Cases")
    lines.append("")
    for item in evaluation.cases:
        lines.append(
            f"- `{item.case_id}` — {item.title}: extraction F1 {_fmt(item.extraction.f1)}, "
            f"mapping F1 {_fmt(item.mapping.f1)}"
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "DEFAULT_STATEMENT_JACCARD_THRESHOLD",
    "GoldClaimLabel",
    "GoldMappingLabel",
    "GoldCase",
    "load_gold_case",
    "load_gold_corpus",
    "bundled_seed_case",
    "PredictedClaim",
    "PredictedMapping",
    "claims_from_artifact",
    "mappings_from_artifact",
    "Counts",
    "CitationValidity",
    "ExtractionMetrics",
    "MappingMetrics",
    "CaseEvaluation",
    "CorpusEvaluation",
    "evaluate_extraction",
    "evaluate_mapping",
    "evaluate_case",
    "evaluate_corpus",
    "render_markdown",
    "evaluation_payload",
]

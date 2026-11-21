"""Claim extraction, authoring and review.

Three ways a claim can exist, all persisted identically and all inspectable:

* **deterministic extraction** — conservative sentence heuristics that only ever
  produce ``EXPLICIT_SOURCE_CLAIM`` nodes with a verified verbatim quote and
  ``NEEDS_REVIEW`` status;
* **model-assisted extraction** — schema-validated output in which every passage
  reference and quote is verified locally; invalid claims are dropped and counted,
  never repaired by guessing;
* **manual authoring** — user requirements, inferred requirements, implementation
  decisions, corrections and review decisions, each with append-only revision
  history.

Categories are never merged: inferred requirements and implementation decisions
cannot carry paper quotes, and explicit source claims must carry one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    ClaimAuthor,
    ClaimConfidence,
    ClaimExplicitness,
    ClaimKind,
    ClaimReviewStatus,
    ClaimType,
    KIND_FOR_LEGACY_CLAIM_TYPE,
    LEGACY_CLAIM_TYPE_FOR_KIND,
    EventType,
    PassageKind,
    RiskCategory,
    RiskSeverity,
)
from mayaworkbench.domain.models import (
    ClaimDependency,
    ClaimRevision,
    ClaimRiskFactor,
    ClaimTarget,
    Project,
    ResearchClaim,
    ResearchDocument,
    ResearchExtraction,
    SourceLocation,
    SourcePassage,
    WorkflowEvent,
)
from mayaworkbench.errors import (
    CitationError,
    ConflictError,
    ModelError,
    NotFoundError,
    PolicyError,
    UsageError,
)
from mayaworkbench.ids import new_ulid, short_hash
from mayaworkbench.models.base import ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.research.dag import validate_claim_dag
from mayaworkbench.research.models import (
    CLAIM_EXTRACTION_SCHEMA,
    ClaimExtractionPayload,
    ClaimExtractionSummary,
    ClaimDagValidation,
    ModelClaim,
)
from mayaworkbench.research.passages import (
    is_reference_section,
    normalize_quote,
    quote_hash,
)
from mayaworkbench.research.prompts import build_claim_extraction_messages
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    EventStore,
    ProjectStore,
    ResearchClaimStore,
    ResearchDocumentStore,
    ResearchExtractionStore,
    SourcePassageStore,
)
from mayaworkbench.version import (
    CLAIM_EXTRACTION_VERSION,
    RESEARCH_PROTOCOL_VERSION,
)

MAX_SIGNALS_PER_CLAIM = 12
MAX_SIGNAL_CHARS = 120
MIN_SENTENCE_CHARS = 40
MAX_SENTENCE_CHARS = 700

_STRONG_MARKERS = [
    r"\bmust not\b",
    r"\bmust\b",
    r"\bwe propose\b",
    r"\bwe introduce\b",
    r"\bwe present\b",
    r"\bwe define\b",
    r"\bis defined as\b",
    r"\bwe prove\b",
    r"\bguarantee(s|d)?\b",
    r"\brequires?\b",
    r"\bconstraint(s)?\b",
    r"\bonly if\b",
    r"\bwe hypothesi[sz]e\b",
]
_MODERATE_MARKERS = [
    r"\bwe use\b",
    r"\bwe apply\b",
    r"\bwe train\b",
    r"\bwe minimi[sz]e\b",
    r"\bwe maximi[sz]e\b",
    r"\bwe compute\b",
    r"\bwe sample\b",
    r"\bwe choose\b",
    r"\bwe set\b",
    r"\bwe assume\b",
    r"\bassumption(s)?\b",
    r"\bobjective\b",
    r"\bloss\b",
    r"\bhyperparameter(s)?\b",
    r"\bis trained\b",
]
_ABBREVIATIONS = {
    "e.g.",
    "i.e.",
    "et al.",
    "fig.",
    "figs.",
    "eq.",
    "eqs.",
    "cf.",
    "vs.",
    "sec.",
    "no.",
    "approx.",
    "ref.",
    "refs.",
    "tab.",
}
_LEADING_PHRASE_RE = re.compile(
    r"(?i)^(?:in this (?:paper|section|work),?\s+)?"
    r"(?:we|our (?:method|model|approach|framework))\s+"
    r"(?:propose|introduce|present|define|show|use|apply|assume|describe|find|argue)\s+"
    r"(?:that\s+)?"
)
_CITATION_ONLY_RE = re.compile(r"^\s*(?:\[\d+\]|\d+\.\s*$)")


# ------------------------------------------------------------------- sentences
def split_sentences(text: str) -> list[str]:
    """Split a passage into sentences without breaking common abbreviations."""
    flattened = re.sub(r"\s+", " ", text).strip()
    if not flattened:
        return []
    parts = re.split(r"(?<=[.!?])\s+", flattened)
    merged: list[str] = []
    for part in parts:
        candidate = part.strip()
        if not candidate:
            continue
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {candidate}"
            continue
        merged.append(candidate)
    return merged


def _ends_with_abbreviation(text: str) -> bool:
    tail = text.rstrip().split(" ")[-1].lower()
    if tail in _ABBREVIATIONS:
        return True
    return bool(re.fullmatch(r"[a-z]\.", tail))


# --------------------------------------------------------------- heuristics
@dataclass
class _Candidate:
    passage: SourcePassage
    statement: str
    title: str
    confidence: ClaimConfidence


def _heuristic_candidates(
    passages: list[SourcePassage], *, min_confidence: ClaimConfidence
) -> tuple[list[_Candidate], int, list[str]]:
    rank = {ClaimConfidence.LOW: 0, ClaimConfidence.MEDIUM: 1, ClaimConfidence.HIGH: 2}
    candidates: list[_Candidate] = []
    eligible = 0
    warnings: list[str] = []
    seen: set[str] = set()
    for passage in passages:
        if is_reference_section(passage.section_path or passage.section_title):
            continue
        if passage.kind not in (PassageKind.PARAGRAPH, PassageKind.LIST, PassageKind.CAPTION):
            continue
        for sentence in split_sentences(passage.text):
            if not (MIN_SENTENCE_CHARS <= len(sentence) <= MAX_SENTENCE_CHARS):
                continue
            if not re.search(r"[A-Za-z]{3}", sentence):
                continue
            if _CITATION_ONLY_RE.match(sentence):
                continue
            eligible += 1
            confidence = _confidence_for(sentence)
            if confidence is None or rank[confidence] < rank[min_confidence]:
                continue
            key = normalize_quote(sentence).casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _Candidate(
                    passage=passage,
                    statement=normalize_quote(sentence),
                    title=_title_from_statement(sentence),
                    confidence=confidence,
                )
            )
    if not candidates:
        warnings.append(
            "No sentence carried a normative marker strong enough for deterministic "
            "extraction; nothing was stored. Review the document or use --model."
        )
    return candidates, eligible, warnings


def _confidence_for(sentence: str) -> ClaimConfidence | None:
    strong = sum(1 for marker in _STRONG_MARKERS if re.search(marker, sentence, re.I))
    moderate = sum(1 for marker in _MODERATE_MARKERS if re.search(marker, sentence, re.I))
    emphatic = bool(
        re.search(r"\bmust not\b|\bguarantee(s|d)?\b|\bwe prove\b", sentence, re.I)
    )
    if emphatic or strong >= 2:
        return ClaimConfidence.HIGH
    if strong >= 1:
        return ClaimConfidence.MEDIUM
    if moderate >= 1:
        return ClaimConfidence.LOW
    return None


def _title_from_statement(statement: str) -> str:
    stripped = _LEADING_PHRASE_RE.sub("", statement).strip()
    stripped = stripped.split(";")[0].strip()
    words = stripped.split()
    truncated = len(words) > 12
    title = " ".join(words[:12]).rstrip(",.;:")
    if not title:
        title = statement[:80]
    title = title[:120]
    if title:
        title = title[0].upper() + title[1:]
    return title + ("…" if truncated else "")


def _heuristic_claims(
    document: ResearchDocument,
    candidates: list[_Candidate],
    *,
    limit: int,
) -> list[ResearchClaim]:
    claims: list[ResearchClaim] = []
    for candidate in candidates[:limit]:
        statement = candidate.statement
        claims.append(
            ResearchClaim(
                id=_claim_id(document.id, candidate.passage.id, ClaimType.EXPLICIT_SOURCE_CLAIM, statement),
                project_id=document.project_id,
                document_id=document.id,
                claim_type=ClaimType.EXPLICIT_SOURCE_CLAIM,
                title=candidate.title,
                statement=statement,
                source_locations=[
                    _location_from_passage(candidate.passage, statement)
                ],
                created_by=ClaimAuthor.HEURISTIC,
                created_by_detail=f"deterministic-extractor/{CLAIM_EXTRACTION_VERSION}",
                confidence=candidate.confidence,
                review_status=ClaimReviewStatus.NEEDS_REVIEW,
            )
        )
    return claims


def _claim_id(
    document_id: str, passage_id: str, claim_type: ClaimType, statement: str
) -> str:
    key = "\x1f".join(
        [document_id, passage_id, claim_type.value, normalize_quote(statement)]
    )
    return "clm_" + short_hash(key, 32)


def _location_from_passage(passage: SourcePassage, quote: str) -> SourceLocation:
    stored = normalize_quote(quote)[:4000]
    return SourceLocation(
        passage_id=passage.id,
        quote_hash=quote_hash(stored),
        page=passage.page,
        page_end=passage.page_end,
        section=passage.section_path,
        equation=passage.equation_label,
        quote=stored,
    )


def verify_source_location(
    passage: SourcePassage | None, location: SourceLocation
) -> tuple[bool, str]:
    """Re-verify one source location against the stored passage text."""
    if passage is None:
        return False, "the referenced passage does not exist"
    if not location.quote:
        return False, "the source location has no quote"
    normalized_passage = normalize_quote(passage.text)
    normalized_quote = normalize_quote(location.quote)
    if normalized_quote not in normalized_passage:
        return False, "the quote does not appear in the referenced passage"
    if quote_hash(normalized_quote) != location.quote_hash:
        return False, "the quote hash does not match the quote"
    return True, "quote verified against the stored passage"


# ----------------------------------------------------------------- service
@dataclass
class _ModelResult:
    claims: list[ResearchClaim] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    risks: dict[str, list[ClaimRiskFactor]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    invocation_id: str | None = None
    dropped: int = 0


class ClaimService:
    def __init__(
        self,
        db: Database,
        *,
        projects: ProjectStore,
        documents: ResearchDocumentStore,
        passages: SourcePassageStore,
        claims: ResearchClaimStore,
        extractions: ResearchExtractionStore,
        artifacts: ArtifactStore,
        events: EventStore,
        gateway: ModelGateway,
        settings: Settings,
    ):
        self.db = db
        self.projects = projects
        self.documents = documents
        self.passages = passages
        self.claims = claims
        self.extractions = extractions
        self.artifacts = artifacts
        self.events = events
        self.gateway = gateway
        self.settings = settings

    # ------------------------------------------------------------------ queries
    def get(self, claim_id: str) -> ResearchClaim:
        return self.claims.get(claim_id)

    def list_claims(self, **kwargs: Any) -> list[ResearchClaim]:
        return self.claims.list(**kwargs)

    def dependencies(self, claim_ids: list[str]) -> list[ClaimDependency]:
        return self.claims.list_dependencies(claim_ids=claim_ids)

    def risk_factors(self, claim_ids: list[str]) -> list[ClaimRiskFactor]:
        return self.claims.list_risk_factors(claim_ids)

    def dag(
        self, *, project_id: str, document_id: str | None = None
    ) -> ClaimDagValidation:
        claims = self.claims.list(project_id=project_id, document_id=document_id, limit=1_000_000)
        edges = self.claims.list_dependencies(
            claim_ids=[claim.id for claim in claims]
        )
        return validate_claim_dag(
            claims, edges, project_id=project_id, document_id=document_id
        )

    def verify_sources(self, claim: ResearchClaim) -> list[tuple[SourceLocation, bool, str]]:
        located = self.passages.find_many([loc.passage_id for loc in claim.source_locations])
        results: list[tuple[SourceLocation, bool, str]] = []
        for location in claim.source_locations:
            passage = located.get(location.passage_id)
            if passage is not None and claim.document_id and passage.document_id != claim.document_id:
                results.append((location, False, "the passage belongs to another document"))
                continue
            ok, reason = verify_source_location(passage, location)
            results.append((location, ok, reason))
        return results

    # --------------------------------------------------------------- extraction
    def extract(
        self,
        document_id: str,
        *,
        use_model: bool = False,
        force: bool = False,
        section: str | None = None,
        min_confidence: ClaimConfidence = ClaimConfidence.LOW,
        max_claims: int | None = None,
    ) -> ClaimExtractionSummary:
        document = self.documents.get(document_id)
        if document.extraction_status.value != "EXTRACTED":
            raise ConflictError(
                f"Document {document_id} has not been extracted successfully",
                hint="Re-register the file or fix the extraction failure first.",
            )
        project = self.projects.get(document.project_id)
        passages = self.passages.list_by_document(document.id)
        if not passages:
            raise ConflictError("This document has no stored passages")
        if section:
            needle = section.casefold()
            passages = [
                passage
                for passage in passages
                if needle in passage.section_path.casefold()
                or (passage.section_title and needle in passage.section_title.casefold())
            ]
            if not passages:
                raise NotFoundError(f"No passage matches section {section!r}")
        limit = max_claims or self.settings.research.max_claims_per_extraction
        section_key = section.strip().lower() if section else ""

        if use_model:
            status = self.gateway.status()
            if not status.callable:
                raise ModelError(
                    status.reason or "no model is available",
                    error_class="unavailable",
                    hint="Run `maya model status`, or extract deterministically without --model.",
                )
            result = self._model_extraction(document, passages, limit=limit)
            claims = result.claims
            dependencies = result.dependencies
            risks = result.risks
            warnings = result.warnings
            method = "MODEL"
            algorithm = f"{CLAIM_EXTRACTION_VERSION}+{status.provider}/{status.model}"
            invocation_id = result.invocation_id
            dropped = result.dropped
            eligible = 0
        else:
            candidates, eligible, warnings = _heuristic_candidates(
                passages, min_confidence=min_confidence
            )
            claims = _heuristic_claims(document, candidates, limit=limit)
            if len(candidates) > limit:
                warnings.append(
                    f"CLAIM_LIMIT_REACHED: {len(candidates) - limit} candidate sentence(s) were "
                    "not stored. Raise research.max_claims_per_extraction or extract a section."
                )
            dependencies = {}
            risks = {}
            method = "DETERMINISTIC"
            algorithm = CLAIM_EXTRACTION_VERSION
            invocation_id = None
            dropped = 0

        extraction_key = short_hash(
            "\x1f".join(
                [
                    method,
                    algorithm,
                    section_key,
                    min_confidence.value,
                    document.content_sha256,
                ]
            ),
            40,
        )
        return self._persist_extraction(
            document=document,
            project=project,
            claims=claims,
            dependencies=dependencies,
            risks=risks,
            method=method,
            algorithm_version=algorithm,
            extraction_key=extraction_key,
            model_invocation_id=invocation_id,
            warnings=warnings,
            eligible_sentences=eligible,
            dropped=dropped,
            force=force,
            section=section,
            min_confidence=min_confidence,
        )

    # ---------------------------------------------------------- model extraction
    def _model_extraction(
        self,
        document: ResearchDocument,
        passages: list[SourcePassage],
        *,
        limit: int,
    ) -> _ModelResult:
        result = _ModelResult()
        windows = _windows(
            [
                passage
                for passage in passages
                if not is_reference_section(passage.section_path or passage.section_title)
            ],
            max_chars=int(
                self.settings.research.extraction_token_budget
                * self.settings.retrieval.chars_per_token
            ),
        )
        if not windows:
            result.warnings.append("No extractable passages remained after filtering.")
            return result

        payload_notes: list[str] = []
        seen_statements: set[str] = set()
        seen_ids: set[str] = set()
        for window_index, window in enumerate(windows, start=1):
            refs = {passage.id: f"P{index}" for index, passage in enumerate(window, start=1)}
            messages = build_claim_extraction_messages(
                document_title=document.title, passages=window, refs=refs
            )
            request = ModelRequest(
                purpose="claim_extraction",
                messages=messages,
                json_schema=CLAIM_EXTRACTION_SCHEMA,
                schema_name="claim_extraction",
                max_output_tokens=min(8000, self.settings.model.max_output_tokens * 2),
                temperature=0.0,
            )
            try:
                response, invocation = self.gateway.structured(
                    request,
                    project_id=document.project_id,
                    snapshot_id=None,
                    purpose="claim_extraction",
                )
            except (PolicyError, ModelError) as exc:
                if window_index == 1:
                    raise
                result.warnings.append(
                    f"Model extraction stopped after window {window_index - 1}: {exc}"
                )
                break
            result.invocation_id = invocation.id
            try:
                payload = ClaimExtractionPayload.model_validate(response.structured or {})
            except ValidationError as exc:
                result.warnings.append(
                    f"Window {window_index} output failed schema validation "
                    f"({exc.error_count()} error(s)); it was discarded."
                )
                continue
            payload_notes.extend(payload.notes)
            window_claims: list[tuple[ResearchClaim, ModelClaim]] = []
            for item in payload.claims:
                if len(result.claims) >= limit:
                    result.warnings.append(
                        f"CLAIM_LIMIT_REACHED: model returned more than {limit} claims; "
                        "the remainder was dropped."
                    )
                    break
                built = self._build_model_claim(
                    document, item, refs=refs, window=window, warnings=result.warnings
                )
                if built is None:
                    result.dropped += 1
                    continue
                claim, risk_rows = built
                key = normalize_quote(claim.statement).casefold()
                if key in seen_statements or claim.id in seen_ids:
                    result.dropped += 1
                    continue
                seen_statements.add(key)
                seen_ids.add(claim.id)
                result.claims.append(claim)
                result.risks[claim.id] = risk_rows
                window_claims.append((claim, item))

            # Resolve model-local refs ("c1") inside this window to stable claim
            # ids. Unresolvable refs are dropped with a warning: a dangling edge
            # is worse than no edge. Cross-window edges are not supported.
            ref_to_claim = {item.ref: claim.id for claim, item in window_claims}
            for claim, item in window_claims:
                parent_ref = (item.parent_ref or "").strip()
                if parent_ref:
                    parent_id = ref_to_claim.get(parent_ref)
                    if parent_id and parent_id != claim.id:
                        claim.parent_claim_id = parent_id
                    else:
                        result.warnings.append(
                            f"Claim {claim.id} named an unknown parent ref {parent_ref!r}; "
                            "the parent link was dropped."
                        )
                resolved: list[str] = []
                for reference in item.depends_on_refs:
                    target = ref_to_claim.get(reference)
                    if target and target != claim.id and target not in resolved:
                        resolved.append(target)
                    elif target == claim.id:
                        result.warnings.append(
                            f"Claim {claim.id} listed itself as a dependency; the edge was dropped."
                        )
                    else:
                        result.warnings.append(
                            f"Claim {claim.id} named an unknown dependency ref {reference!r}; "
                            "the edge was dropped."
                        )
                if resolved:
                    result.dependencies[claim.id] = resolved
        result.warnings.extend(payload_notes)
        if result.dropped:
            result.warnings.append(
                f"{result.dropped} model claim(s) were dropped because their passages or quotes "
                "could not be verified."
            )
        return result

    def _build_model_claim(
        self,
        document: ResearchDocument,
        item: ModelClaim,
        *,
        refs: dict[str, str],
        window: list[SourcePassage],
        warnings: list[str],
    ) -> tuple[ResearchClaim, list[ClaimRiskFactor]] | None:
        if item.claim_type not in (
            ClaimType.EXPLICIT_SOURCE_CLAIM.value,
            ClaimType.INFERRED_REQUIREMENT.value,
        ):
            warnings.append(f"Dropped claim with unsupported type {item.claim_type!r}.")
            return None
        claim_type = ClaimType(item.claim_type)
        statement = normalize_quote(item.statement)
        if not statement:
            warnings.append("Dropped claim with an empty statement.")
            return None
        resolved_ids: list[str] = []
        for reference in item.passage_ids:
            passage_id = refs.get(reference)
            if passage_id is None:
                warnings.append(
                    f"Dropped claim {item.title!r}: passage reference {reference!r} is not in "
                    "the supplied window."
                )
                return None
            resolved_ids.append(passage_id)
        by_id = {passage.id: passage for passage in window}
        risk_rows: list[ClaimRiskFactor] = []

        if claim_type == ClaimType.EXPLICIT_SOURCE_CLAIM:
            quote = normalize_quote(item.quote)
            if not resolved_ids:
                warnings.append(
                    f"Dropped explicit claim {item.title!r}: no passage reference was supplied."
                )
                return None
            if not quote:
                warnings.append(
                    f"Dropped explicit claim {item.title!r}: the paper quote was empty."
                )
                return None
            locations: list[SourceLocation] = []
            for passage_id in resolved_ids:
                passage = by_id[passage_id]
                if quote in normalize_quote(passage.text):
                    locations.append(_location_from_passage(passage, quote))
                    break
            if not locations:
                warnings.append(
                    f"Dropped explicit claim {item.title!r}: the quote does not appear verbatim "
                    "in the cited passage."
                )
                return None
            claim = ResearchClaim(
                id=_claim_id(document.id, locations[0].passage_id, claim_type, statement),
                project_id=document.project_id,
                document_id=document.id,
                claim_type=claim_type,
                title=item.title.strip()[:300],
                statement=statement,
                implementation_requirement=normalize_quote(item.implementation_requirement)[:4000],
                source_locations=locations,
                expected_code_signals=_normalise_signals(item.expected_code_signals, warnings),
                forbidden_code_signals=_normalise_signals(
                    item.forbidden_code_signals, warnings
                ),
                missing_details=_normalise_lines(item.missing_details),
                validation_methods=_normalise_lines(item.validation_methods),
                created_by=ClaimAuthor.MODEL,
                created_by_detail=self._model_label(),
                confidence=item.confidence,
                review_status=ClaimReviewStatus.NEEDS_REVIEW,
            )
        else:
            note = normalize_quote(item.derivation_note)
            if not note:
                warnings.append(
                    f"Dropped inferred claim {item.title!r}: an inference needs a derivation note."
                )
                return None
            if not resolved_ids:
                warnings.append(
                    f"Dropped inferred claim {item.title!r}: it must reference its basis passage."
                )
                return None
            claim = ResearchClaim(
                id=_claim_id(
                    document.id,
                    sorted(resolved_ids)[0],
                    claim_type,
                    statement,
                ),
                project_id=document.project_id,
                document_id=document.id,
                claim_type=claim_type,
                title=item.title.strip()[:300],
                statement=statement,
                implementation_requirement=normalize_quote(item.implementation_requirement)[:4000],
                basis_passage_ids=sorted(set(resolved_ids)),
                derivation_note=note[:4000],
                expected_code_signals=_normalise_signals(item.expected_code_signals, warnings),
                forbidden_code_signals=_normalise_signals(
                    item.forbidden_code_signals, warnings
                ),
                missing_details=_normalise_lines(item.missing_details),
                validation_methods=_normalise_lines(item.validation_methods),
                created_by=ClaimAuthor.MODEL,
                created_by_detail=self._model_label(),
                confidence=item.confidence,
                review_status=ClaimReviewStatus.NEEDS_REVIEW,
            )
        _guard_signal_overlap(claim)
        for risk in item.risk_factors:
            risk_rows.append(
                ClaimRiskFactor(
                    id="rsk_" + new_ulid(),
                    claim_id=claim.id,
                    category=risk.category,
                    detail=risk.detail.strip()[:2000],
                    severity=risk.severity,
                )
            )
        return claim, risk_rows

    def _model_label(self) -> str:
        settings = self.settings.model
        return f"{settings.provider}/{settings.model or '<unset>'}"

    # ------------------------------------------------------------- persistence
    def _persist_extraction(
        self,
        *,
        document: ResearchDocument,
        project: Project,
        claims: list[ResearchClaim],
        dependencies: dict[str, list[str]],
        risks: dict[str, list[ClaimRiskFactor]],
        method: str,
        algorithm_version: str,
        extraction_key: str,
        model_invocation_id: str | None,
        warnings: list[str],
        eligible_sentences: int,
        dropped: int,
        force: bool,
        section: str | None,
        min_confidence: ClaimConfidence,
    ) -> ClaimExtractionSummary:
        previous = self.extractions.find_by_key(document.id, extraction_key)
        if previous is not None and not force:
            raise ConflictError(
                "This extraction has already been run for this document version",
                hint=f"Pass --force to replace unreviewed machine claims (run {previous.id}).",
            )
        if previous is not None and force:
            # A forced re-run is a new audit record: keep the earlier run's key stable
            # and suffix repeats instead of overwriting history.
            run = 2
            while self.extractions.find_by_key(
                document.id, f"{extraction_key}:r{run}"
            ) is not None:
                run += 1
            extraction_key = f"{extraction_key}:r{run}"
        existing = self.claims.list(
            project_id=project.id, document_id=document.id, limit=1_000_000
        )
        preserved = [claim for claim in existing if claim.is_reviewed()]
        preserved_ids = {claim.id for claim in preserved}
        new_claims = [claim for claim in claims if claim.id not in preserved_ids]
        next_ordinal = max((claim.ordinal for claim in preserved), default=-1) + 1
        for claim in new_claims:
            claim.ordinal = next_ordinal
            next_ordinal += 1
            _guard_signal_overlap(claim)

        payload = {
            "protocol_version": RESEARCH_PROTOCOL_VERSION,
            "document": {
                "id": document.id,
                "document_key": document.document_key,
                "version": document.version,
                "content_sha256": document.content_sha256,
                "title": document.title,
            },
            "method": method,
            "algorithm_version": algorithm_version,
            "parameters": {
                "section": section,
                "min_confidence": min_confidence.value,
            },
            "claims": [
                claim.model_dump(mode="json") for claim in new_claims
            ],
            "preserved_claim_ids": sorted(preserved_ids),
            "warnings": warnings,
        }

        with self.db.transaction():
            dropped_ids = self.claims.delete_machine_claims(
                document.id, keep=sorted(preserved_ids)
            )
            for claim in new_claims:
                self.claims.create(claim)
                for target in dependencies.get(claim.id, []):
                    self.claims.add_dependency(
                        ClaimDependency(
                            id="dep_" + short_hash(f"{claim.id}:{target}", 32),
                            claim_id=claim.id,
                            depends_on_claim_id=target,
                            note="model-extracted dependency",
                        )
                    )
                if risks.get(claim.id):
                    self.claims.insert_risk_factors(risks[claim.id])
            artifact = self.artifacts.put_text(
                json.dumps(payload, indent=2, sort_keys=True),
                kind=ArtifactKind.CLAIM_SET,
                media_type="application/json",
                created_by="claims",
            )
            extraction = ResearchExtraction(
                id="rex_" + new_ulid(),
                document_id=document.id,
                method=method,
                algorithm_version=algorithm_version,
                extraction_key=extraction_key,
                model_invocation_id=model_invocation_id,
                claims_created=len(new_claims),
                claims_preserved=len(preserved),
                claims_dropped=len(dropped_ids) + dropped,
                artifact_id=artifact.id,
                warnings=warnings[:200],
                parameters={
                    "section": section,
                    "min_confidence": min_confidence.value,
                    "dropped_invalid": dropped,
                },
            )
            self.extractions.create(extraction)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.CLAIMS_EXTRACTED,
                    project_id=project.id,
                    entity_type="research_document",
                    entity_id=document.id,
                    actor_kind=ActorKind.SYSTEM
                    if method == "DETERMINISTIC"
                    else ActorKind.MODEL,
                    actor_name="SYSTEM:claims" if method == "DETERMINISTIC" else self._model_label(),
                    payload={
                        "extraction_id": extraction.id,
                        "method": method,
                        "algorithm_version": algorithm_version,
                        "claims_created": len(new_claims),
                        "claims_preserved": len(preserved),
                        "claims_dropped": extraction.claims_dropped,
                        "warnings": len(warnings),
                        "artifact_id": artifact.id,
                    },
                )
            )

        return ClaimExtractionSummary(
            document_id=document.id,
            method=method,
            algorithm_version=algorithm_version,
            claims=new_claims,
            claims_created=len(new_claims),
            claims_preserved=len(preserved),
            claims_dropped=extraction.claims_dropped,
            warnings=warnings,
            model_used=method == "MODEL",
            model_invocation_id=model_invocation_id,
            extraction_id=extraction.id,
            artifact_id=artifact.id,
            eligible_sentences=eligible_sentences,
        )

    # --------------------------------------------------------------- manual add
    def add_claim(
        self,
        project_id: str,
        *,
        claim_type: ClaimType | None = None,
        kind: ClaimKind | None = None,
        explicitness: ClaimExplicitness | None = None,
        target_resources: list[ClaimTarget | dict[str, Any]] | None = None,
        validation_ids: list[str] | None = None,
        uncertainty: list[str] | None = None,
        title: str,
        statement: str,
        implementation_requirement: str = "",
        document_id: str | None = None,
        passage_id: str | None = None,
        quote: str = "",
        basis_passage_ids: list[str] | None = None,
        parent_claim_id: str | None = None,
        depends_on: list[str] | None = None,
        expected_code_signals: list[str] | None = None,
        forbidden_code_signals: list[str] | None = None,
        target_paths: list[str] | None = None,
        target_symbols: list[str] | None = None,
        missing_details: list[str] | None = None,
        validation_methods: list[str] | None = None,
        risk_factors: list[tuple[RiskCategory, RiskSeverity, str]] | None = None,
        derivation_note: str = "",
        confidence: ClaimConfidence = ClaimConfidence.MEDIUM,
        accept: bool = False,
        actor: str = "local-user",
    ) -> ResearchClaim:
        if claim_type is None and kind is None:
            raise UsageError(
                "Provide --kind or --claim-type",
                hint="Typed kinds: " + ", ".join(item.value for item in ClaimKind),
            )
        if claim_type is not None and kind is not None:
            if LEGACY_CLAIM_TYPE_FOR_KIND[kind] is not claim_type:
                raise UsageError(
                    f"kind {kind.value} is incompatible with claim_type {claim_type.value}"
                )
        elif claim_type is None and kind is not None:
            claim_type = LEGACY_CLAIM_TYPE_FOR_KIND[kind]
        elif kind is None and claim_type is not None:
            kind = KIND_FOR_LEGACY_CLAIM_TYPE[claim_type]
        project = self.projects.get(project_id)
        document = self.documents.get(document_id) if document_id else None
        if document is not None and document.project_id != project.id:
            raise UsageError("The document belongs to another project")

        warnings: list[str] = []
        source_locations: list[SourceLocation] = []
        basis: list[str] = list(basis_passage_ids or [])
        if claim_type == ClaimType.EXPLICIT_SOURCE_CLAIM:
            if document is None or not passage_id:
                raise UsageError(
                    "An explicit source claim needs --document and --passage",
                    hint="Quote the paper from the passage that states it.",
                )
            if not normalize_quote(quote):
                raise UsageError(
                    "An explicit source claim needs a verbatim --quote",
                    hint="`maya research show <document> --passages` lists passage ids.",
                )
            source_locations.append(self._verified_location(document, passage_id, quote))
        else:
            if passage_id:
                basis.append(passage_id)
            if claim_type in (
                ClaimType.INFERRED_REQUIREMENT,
                ClaimType.IMPLEMENTATION_DECISION,
            ) and not normalize_quote(derivation_note):
                raise UsageError(
                    f"{claim_type.value} needs --derivation-note",
                    hint="State why this requirement or decision follows from the source.",
                )
        for basis_id in basis:
            if document is None:
                raise UsageError("Basis passages require --document")
            self.passages.get(basis_id)
        parent = self._check_parent(project_id, parent_claim_id, document_id)
        dependency_ids = self._check_dependencies(project_id, depends_on or [], claim_id=None)

        targets: list[ClaimTarget] = []
        seen_targets: set[str] = set()
        for raw in target_resources or []:
            target = ClaimTarget.model_validate(raw)
            if target.label not in seen_targets:
                seen_targets.add(target.label)
                targets.append(target)

        claim = ResearchClaim(
            id="clm_" + new_ulid(),
            project_id=project.id,
            document_id=document.id if document else None,
            parent_claim_id=parent.id if parent else None,
            ordinal=self.claims.next_ordinal(
                project_id=project.id, document_id=document.id if document else None
            ),
            claim_type=claim_type,
            kind=kind,
            explicitness=explicitness,
            title=title.strip()[:300],
            statement=normalize_quote(statement)[:8000],
            implementation_requirement=normalize_quote(implementation_requirement)[:4000],
            source_locations=source_locations,
            basis_passage_ids=sorted(set(basis)),
            target_resources=targets,
            validation_ids=_normalise_lines(validation_ids or []),
            uncertainty=_normalise_lines(uncertainty or []),
            expected_code_signals=_normalise_signals(expected_code_signals or [], warnings),
            forbidden_code_signals=_normalise_signals(forbidden_code_signals or [], warnings),
            target_paths=_normalise_lines(target_paths or []),
            target_symbols=_normalise_lines(target_symbols or []),
            missing_details=_normalise_lines(missing_details or []),
            validation_methods=_normalise_lines(validation_methods or []),
            derivation_note=normalize_quote(derivation_note)[:4000],
            created_by=ClaimAuthor.USER,
            created_by_detail=actor,
            confidence=confidence,
            review_status=ClaimReviewStatus.ACCEPTED
            if accept
            else ClaimReviewStatus.NEEDS_REVIEW,
            reviewed_by=actor if accept else None,
            reviewed_at=utcnow() if accept else None,
        )
        self._validate_claim(claim)
        rows = _risk_rows(claim.id, risk_factors or [])
        with self.db.transaction():
            self.claims.create(claim)
            for target in dependency_ids:
                self.claims.add_dependency(
                    ClaimDependency(
                        id="dep_" + short_hash(f"{claim.id}:{target}", 32),
                        claim_id=claim.id,
                        depends_on_claim_id=target,
                    )
                )
            if rows:
                self.claims.insert_risk_factors(rows)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.CLAIM_CREATED,
                    project_id=project.id,
                    entity_type="research_claim",
                    entity_id=claim.id,
                    actor_kind=ActorKind.USER,
                    actor_name=actor,
                    payload={
                        "claim_type": claim.claim_type.value,
                        "kind": claim.kind.value,
                        "explicitness": claim.explicitness.value,
                        "graph_version": claim.graph_version,
                        "title": claim.title,
                        "document_id": claim.document_id,
                        "review_status": claim.review_status.value,
                        "dependencies": dependency_ids,
                        "source_locations": len(claim.source_locations),
                    },
                )
            )
        return claim

    def _verified_location(
        self, document: ResearchDocument, passage_id: str, quote: str
    ) -> SourceLocation:
        passage = self.passages.get(passage_id)
        if passage.document_id != document.id:
            raise UsageError(
                f"Passage {passage_id} belongs to another document",
                hint=f"Passages for this document can be listed with "
                f"`maya research show {document.id} --passages`.",
            )
        normalized = normalize_quote(quote)
        if normalized not in normalize_quote(passage.text):
            raise CitationError(
                "The quote does not appear verbatim in the referenced passage",
                hint="Copy the sentence exactly from the passage text shown by `maya research show`.",
            )
        return _location_from_passage(passage, normalized)

    # ------------------------------------------------------------------- edits
    def edit_claim(
        self,
        claim_id: str,
        *,
        changes: dict[str, Any],
        dependencies: list[str] | None = None,
        risk_factors: list[tuple[RiskCategory, RiskSeverity, str]] | None = None,
        source_location: tuple[str, str] | None = None,
        reason: str = "",
        keep_review: bool = False,
        actor: str = "local-user",
    ) -> ResearchClaim:
        claim = self.claims.get(claim_id)
        if source_location is not None:
            passage_id, quote = source_location
            if claim.claim_type is not ClaimType.EXPLICIT_SOURCE_CLAIM:
                raise UsageError(
                    "Only an explicit source claim carries a paper quote",
                    hint="Inferred requirements cite basis passages instead.",
                )
            document = (
                self.documents.get(claim.document_id) if claim.document_id else None
            )
            if document is None:
                raise UsageError("This claim has no document to verify the quote against")
            changes = {
                **changes,
                "source_locations": [
                    self._verified_location(document, passage_id, quote)
                ],
            }
        if not changes and dependencies is None and risk_factors is None:
            raise UsageError("No changes were requested")
        before = claim.model_dump(mode="json")
        updated = claim.model_copy(
            update={
                **changes,
                "updated_at": utcnow(),
                "review_status": claim.review_status if keep_review else ClaimReviewStatus.NEEDS_REVIEW,
                "reviewed_by": claim.reviewed_by if keep_review else None,
                "reviewed_at": claim.reviewed_at if keep_review else None,
            }
        )
        if dependencies is not None:
            resolved = self._check_dependencies(
                claim.project_id, dependencies, claim_id=claim.id
            )
        else:
            resolved = None
        self._validate_claim(updated)
        after = updated.model_dump(mode="json")
        if before == after and resolved is None and risk_factors is None:
            raise ConflictError("The edit did not change anything")
        revision_number = self.claims.next_revision(claim.id)
        with self.db.transaction():
            self.claims.update(updated)
            if resolved is not None:
                self.claims.clear_dependencies(claim.id)
                for target in resolved:
                    self.claims.add_dependency(
                        ClaimDependency(
                            id="dep_" + short_hash(f"{claim.id}:{target}", 32),
                            claim_id=claim.id,
                            depends_on_claim_id=target,
                        )
                    )
            if risk_factors is not None:
                self.claims.clear_risk_factors(claim.id)
                rows = _risk_rows(claim.id, risk_factors)
                if rows:
                    self.claims.insert_risk_factors(rows)
            self.claims.append_revision(
                ClaimRevision(
                    id="rev_" + new_ulid(),
                    claim_id=claim.id,
                    revision=revision_number,
                    changed_at=utcnow(),
                    changed_by=actor,
                    reason=reason,
                    before=before,
                    after=after,
                )
            )
            changed_keys = sorted(
                key for key in changes if before.get(key) != after.get(key)
            )
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.CLAIM_UPDATED,
                    project_id=claim.project_id,
                    entity_type="research_claim",
                    entity_id=claim.id,
                    actor_kind=ActorKind.USER,
                    actor_name=actor,
                    payload={
                        "revision": revision_number,
                        "changed_fields": changed_keys,
                        "dependencies_replaced": resolved is not None,
                        "risk_factors_replaced": risk_factors is not None,
                        "reason": reason[:300],
                    },
                )
            )
        return updated

    def review_claim(
        self,
        claim_id: str,
        *,
        accepted: bool,
        note: str | None = None,
        actor: str = "local-user",
    ) -> ResearchClaim:
        claim = self.claims.get(claim_id)
        if not accepted and not (note or "").strip():
            raise UsageError(
                "Rejecting a claim needs --note",
                hint="Record why the claim is not supported; the reason is evidence.",
            )
        status = ClaimReviewStatus.ACCEPTED if accepted else ClaimReviewStatus.REJECTED
        if claim.review_status == status and claim.review_note == note:
            raise ConflictError("The claim already has this review status and note")
        before = claim.model_dump(mode="json")
        updated = claim.model_copy(
            update={
                "review_status": status,
                "review_note": note,
                "reviewed_by": actor,
                "reviewed_at": utcnow(),
                "updated_at": utcnow(),
            }
        )
        after = updated.model_dump(mode="json")
        with self.db.transaction():
            self.claims.update(updated)
            self.claims.append_revision(
                ClaimRevision(
                    id="rev_" + new_ulid(),
                    claim_id=claim.id,
                    revision=self.claims.next_revision(claim.id),
                    changed_at=utcnow(),
                    changed_by=actor,
                    reason=f"review: {status.value}",
                    before=before,
                    after=after,
                )
            )
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.CLAIM_REVIEWED,
                    project_id=claim.project_id,
                    entity_type="research_claim",
                    entity_id=claim.id,
                    actor_kind=ActorKind.USER,
                    actor_name=actor,
                    payload={
                        "review_status": status.value,
                        "note": (note or "")[:500],
                    },
                )
            )
        return updated

    # --------------------------------------------------------------- validation
    def _check_parent(
        self, project_id: str, parent_claim_id: str | None, document_id: str | None
    ) -> ResearchClaim | None:
        if not parent_claim_id:
            return None
        parent = self.claims.get(parent_claim_id)
        if parent.project_id != project_id:
            raise UsageError("The parent claim belongs to another project")
        if document_id and parent.document_id and parent.document_id != document_id:
            raise UsageError(
                "The parent claim belongs to another document",
                hint="Cross-document hierarchy is recorded with dependencies, not parents.",
            )
        return parent

    def _check_dependencies(
        self, project_id: str, depends_on: list[str], *, claim_id: str | None
    ) -> list[str]:
        resolved: list[str] = []
        for identifier in depends_on:
            target = self.claims.get(identifier)
            if target.project_id != project_id:
                raise UsageError(f"Dependency {identifier} belongs to another project")
            if claim_id and target.id == claim_id:
                raise UsageError("A claim cannot depend on itself")
            if target.id not in resolved:
                resolved.append(target.id)
        return resolved

    def _validate_claim(self, claim: ResearchClaim) -> None:
        if not claim.statement.strip():
            raise UsageError("The claim statement is empty")
        if claim.claim_type == ClaimType.EXPLICIT_SOURCE_CLAIM:
            if not claim.source_locations:
                raise UsageError(
                    "An explicit source claim needs at least one verified source location"
                )
            if claim.basis_passage_ids:
                raise UsageError(
                    "An explicit source claim quotes its source; basis passages are for "
                    "inferred requirements"
                )
        else:
            if claim.source_locations:
                raise UsageError(
                    f"{claim.claim_type.value} must not present a paper quote as its own statement",
                    hint="Use --basis-passage for the passage the inference derives from.",
                )
            if claim.claim_type in (
                ClaimType.INFERRED_REQUIREMENT,
                ClaimType.IMPLEMENTATION_DECISION,
            ) and not claim.derivation_note.strip():
                raise UsageError(
                    f"{claim.claim_type.value} needs a derivation note"
                )
        if claim.kind in (ClaimKind.PAPER_METHOD, ClaimKind.PAPER_RESULT):
            if not claim.source_locations:
                raise UsageError(
                    f"{claim.kind.value} must carry at least one verified source quote",
                    hint="Cite --document/--passage/--quote, or use INFERRED_REQUIREMENT.",
                )
            if claim.explicitness is not ClaimExplicitness.EXPLICIT:
                raise UsageError(
                    f"{claim.kind.value} states what the paper says and must be EXPLICIT"
                )
        if claim.kind is ClaimKind.INFERRED_REQUIREMENT:
            if claim.explicitness is not ClaimExplicitness.INFERRED:
                raise UsageError(
                    "INFERRED_REQUIREMENT must be labelled --explicitness INFERRED"
                )
            if not claim.derivation_note.strip():
                raise UsageError("INFERRED_REQUIREMENT needs a derivation note")
        if claim.kind is ClaimKind.VALIDATION_CLAIM and not (
            claim.validation_ids or claim.validation_methods
        ):
            raise UsageError(
                "VALIDATION_CLAIM needs --validation-id or --validation-method",
                hint="Record what the test or experiment is intended to establish.",
            )
        _guard_signal_overlap(claim)


# ------------------------------------------------------------------- helpers
def _normalise_signals(values: list[str], warnings: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        value = str(raw).strip().strip("`")
        value = re.sub(r"\s+", " ", value)
        if not value:
            continue
        if len(value) > MAX_SIGNAL_CHARS:
            warnings.append(
                f"Signal {value[:40]!r} is longer than {MAX_SIGNAL_CHARS} characters and was dropped."
            )
            continue
        if any(char in value for char in "\n\r\t"):
            continue
        if value.casefold() in {existing.casefold() for existing in cleaned}:
            continue
        cleaned.append(value)
    if len(cleaned) > MAX_SIGNALS_PER_CLAIM:
        warnings.append(
            f"Only the first {MAX_SIGNALS_PER_CLAIM} acceptance signals were kept."
        )
        cleaned = cleaned[:MAX_SIGNALS_PER_CLAIM]
    return cleaned


def _normalise_lines(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw)).strip()
        if value and value not in cleaned:
            cleaned.append(value[:2000])
    return cleaned


def _guard_signal_overlap(claim: ResearchClaim) -> None:
    expected = {signal.casefold() for signal in claim.expected_code_signals}
    forbidden = {signal.casefold() for signal in claim.forbidden_code_signals}
    overlap = expected & forbidden
    if overlap:
        raise ConflictError(
            "A signal cannot be both expected and forbidden: " + ", ".join(sorted(overlap))
        )


def _risk_rows(
    claim_id: str, factors: list[tuple[RiskCategory, RiskSeverity, str]]
) -> list[ClaimRiskFactor]:
    rows: list[ClaimRiskFactor] = []
    for category, severity, detail in factors:
        detail_text = re.sub(r"\s+", " ", detail).strip()
        if not detail_text:
            raise UsageError("A risk factor needs a detail")
        rows.append(
            ClaimRiskFactor(
                id="rsk_" + new_ulid(),
                claim_id=claim_id,
                category=category,
                detail=detail_text[:2000],
                severity=severity,
            )
        )
    return rows


def _windows(passages: list[SourcePassage], *, max_chars: int) -> list[list[SourcePassage]]:
    windows: list[list[SourcePassage]] = []
    current: list[SourcePassage] = []
    size = 0
    for passage in passages:
        cost = len(passage.text) + 80
        if current and size + cost > max_chars:
            windows.append(current)
            current = []
            size = 0
        current.append(passage)
        size += cost
    if current:
        windows.append(current)
    return windows


__all__ = [
    "ClaimService",
    "split_sentences",
    "verify_source_location",
]

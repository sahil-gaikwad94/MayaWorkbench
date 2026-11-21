"""Claim-to-code mapping service.

For every claim the service:

1. collects candidate spans from the pinned snapshot;
2. applies the deterministic rule pack;
3. optionally asks a model to adjudicate over exactly those spans;
4. verifies every piece of evidence against the indexed lines;
5. persists the mapping, its evidence and a workflow event;
6. can rebuild the full claim-to-code matrix, re-verifying all evidence.

A model can refine a deterministic verdict but never fabricate evidence: it may
only cite spans it was shown, and a ``PRESENT`` verdict is rejected unless the
claim's declared signals actually appear in those spans' executable code.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    ClaimMappingStatus,
    ClaimReviewStatus,
    EventType,
    EvidenceStrength,
    MappingAuthor,
    MappingEvidenceRelation,
)
from mayaworkbench.domain.models import (
    ClaimMapping,
    MappingEvidence,
    Project,
    Repository,
    RepositorySnapshot,
    ResearchClaim,
    WorkflowEvent,
)
from mayaworkbench.errors import (
    ConflictError,
    ModelError,
    NotFoundError,
    PolicyError,
    UsageError,
)
from mayaworkbench.ids import new_ulid
from mayaworkbench.mapping.evidence import CollectionResult, EvidenceCollector, SpanAnalysis
from mayaworkbench.mapping.models import (
    MAPPING_ADJUDICATION_SCHEMA,
    MappingAdjudicationPayload,
)
from mayaworkbench.mapping.prompts import build_mapping_messages
from mayaworkbench.mapping.rules import (
    MappingDecision,
    apply_absence_policy,
    decide_mapping,
)
from mayaworkbench.mapping.signals import derive_identifiers, matched_signals
from mayaworkbench.models.base import ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.research.models import (
    ClaimMatrixRow,
    ClaimToCodeMatrix,
    VerifiedEvidence,
)
from mayaworkbench.retrieval.search import SearchService
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    ClaimMappingStore,
    EventStore,
    FileStore,
    ProjectStore,
    RepositoryStore,
    ResearchClaimStore,
    SnapshotStore,
    SymbolStore,
)
from mayaworkbench.version import MAPPING_RULE_PACK_VERSION, RESEARCH_PROTOCOL_VERSION

MAX_MODEL_SPANS = 6


@dataclass
class MappingOutcome:
    mapping: ClaimMapping
    evidence: list[MappingEvidence]
    decision: MappingDecision
    model_used: bool = False
    model_rejected: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class MappingRun:
    matrix: ClaimToCodeMatrix
    outcomes: list[MappingOutcome] = field(default_factory=list)
    artifact_id: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class _Adjudication:
    payload: MappingAdjudicationPayload | None = None
    invocation_id: str | None = None
    refusal: str | None = None
    cited: list[MappingEvidence] = field(default_factory=list)


class ClaimMappingService:
    def __init__(
        self,
        db: Database,
        *,
        projects: ProjectStore,
        repositories: RepositoryStore,
        snapshots: SnapshotStore,
        files: FileStore,
        symbols: SymbolStore,
        claims: ResearchClaimStore,
        mappings: ClaimMappingStore,
        search: SearchService,
        gateway: ModelGateway,
        artifacts: ArtifactStore,
        events: EventStore,
        settings: Settings,
    ):
        self.db = db
        self.projects = projects
        self.repositories = repositories
        self.snapshots = snapshots
        self.files = files
        self.symbols = symbols
        self.claims = claims
        self.mappings = mappings
        self.search = search
        self.gateway = gateway
        self.artifacts = artifacts
        self.events = events
        self.settings = settings
        self.collector = EvidenceCollector(
            files=files, symbols=symbols, search=search, settings=settings
        )

    # ------------------------------------------------------------------- mapping
    def map_project(
        self,
        project: Project,
        snapshot: RepositorySnapshot,
        *,
        use_model: bool = False,
        claims: list[ResearchClaim] | None = None,
    ) -> MappingRun:
        repository = self._repository_for(project, snapshot)
        selected = claims or self.claims.list(project_id=project.id, limit=1_000_000)
        notes: list[str] = []
        status = self.gateway.status()
        if use_model and not status.callable:
            notes.append(
                "Model adjudication was requested but no model is callable "
                f"({status.reason}); deterministic mappings were produced instead."
            )
        outcomes: list[MappingOutcome] = []
        for claim in selected:
            if claim.review_status is ClaimReviewStatus.REJECTED:
                continue
            outcomes.append(self.map_claim(claim, snapshot, use_model=use_model))
        matrix = self.build_matrix(
            project, snapshot, claims=selected, repository=repository
        )
        artifact = self.artifacts.put_text(
            json.dumps(matrix.model_dump(mode="json"), indent=2, sort_keys=True),
            kind=ArtifactKind.CLAIM_MATRIX,
            media_type="application/json",
            created_by="mapping",
        )
        if selected and not matrix.mapped_rows:
            notes.append(
                "No claim could be mapped; every claim was either rejected or produced no "
                "candidate evidence."
            )
        return MappingRun(
            matrix=matrix,
            outcomes=outcomes,
            artifact_id=artifact.id,
            notes=notes,
        )

    def map_claim(
        self,
        claim: ResearchClaim,
        snapshot: RepositorySnapshot,
        *,
        use_model: bool = False,
    ) -> MappingOutcome:
        repository = self.repositories.get(snapshot.repository_id)
        if claim.project_id != repository.project_id:
            raise UsageError(
                "The claim and snapshot belong to different projects",
                hint="Map claims against a snapshot of their own project.",
            )
        declared = list(claim.expected_code_signals)
        derived = self._derived_signals(claim)
        collection = self.collector.collect(
            snapshot.id, claim, declared_signals=declared, derived_signals=derived
        )
        decision = decide_mapping(
            claim=claim,
            declared_signals=declared,
            derived_signals=derived,
            analyses=collection.analyses,
            coverage_limited=collection.coverage_limited,
            truncated=collection.truncated,
        )
        selected = collection.analyses[: self.settings.research.max_evidence_spans]
        author = MappingAuthor.DETERMINISTIC
        model_invocation_id: str | None = None
        rejected_model = False
        notes: list[str] = []
        details: dict = {
            "searched_terms": collection.searched_terms[:50],
            "candidate_spans": len(collection.analyses),
            "coverage_limited": collection.coverage_limited,
            "discovery_truncated": collection.truncated,
            "declared_signals": declared,
            "derived_signals": derived,
            "rule_status": decision.status.value,
            "rule_reason": decision.reason,
        }
        evidence_rows: list[MappingEvidence]

        if use_model:
            status = self.gateway.status()
            if not status.callable:
                notes.append(f"model unavailable: {status.reason}")
            elif not collection.analyses:
                notes.append(
                    "no candidate spans existed, so the model was not asked to adjudicate"
                )
            else:
                adjudication = self._adjudicate(
                    claim=claim,
                    snapshot=snapshot,
                    collection=collection,
                    declared=declared,
                )
                model_invocation_id = adjudication.invocation_id
                if adjudication.refusal is not None:
                    rejected_model = True
                    notes.append(f"model adjudication rejected: {adjudication.refusal}")
                    details["model_rejection"] = adjudication.refusal
                elif adjudication.payload is not None:
                    decision = MappingDecision(
                        status=ClaimMappingStatus(adjudication.payload.status),
                        reason=adjudication.payload.rationale,
                        rationale=adjudication.payload.rationale,
                        gaps=list(adjudication.payload.gaps),
                        counter_evidence=list(adjudication.payload.counter_evidence),
                        validation_method=decision.validation_method,
                    )
                    evidence_rows = adjudication.cited
                    author = MappingAuthor.MODEL
                    details["model"] = {
                        "confidence": adjudication.payload.confidence,
                        "status": adjudication.payload.status,
                        "evidence_refs": [
                            item.span_ref for item in adjudication.payload.evidence
                        ],
                        "evidence": [
                            {
                                "path": item.path,
                                "start_line": item.start_line,
                                "end_line": item.end_line,
                                "relation": item.relation.value,
                            }
                            for item in adjudication.cited
                        ],
                    }

        if author is not MappingAuthor.MODEL:
            evidence_rows = [
                self._build_evidence(
                    analysis,
                    snapshot=snapshot,
                    author=author,
                    relation=analysis.relation,
                )
                for analysis in selected
            ]

        decision = apply_absence_policy(
            decision,
            coverage_limited=collection.coverage_limited,
            truncated=collection.truncated,
        )
        mapping, evidence_rows = self._persist_mapping(
            claim=claim,
            snapshot=snapshot,
            decision=decision,
            evidence=evidence_rows,
            author=author,
            model_invocation_id=model_invocation_id,
            details=details,
            notes=notes,
            rejected_model=rejected_model,
        )
        return MappingOutcome(
            mapping=mapping,
            evidence=evidence_rows,
            decision=decision,
            model_used=author is MappingAuthor.MODEL,
            model_rejected=rejected_model,
            notes=notes,
        )

    # ----------------------------------------------------------- model adjudication
    def _adjudicate(
        self,
        *,
        claim: ResearchClaim,
        snapshot: RepositorySnapshot,
        collection: CollectionResult,
        declared: list[str],
    ) -> _Adjudication:
        citable = collection.analyses[:MAX_MODEL_SPANS]
        refs = {index: f"S{index + 1}" for index in range(len(citable))}
        messages = build_mapping_messages(
            claim=claim,
            analyses=citable,
            refs=refs,
            snapshot_identity=snapshot.identity_label,
        )
        request = ModelRequest(
            purpose="claim_mapping",
            messages=messages,
            json_schema=MAPPING_ADJUDICATION_SCHEMA,
            schema_name="claim_mapping",
            max_output_tokens=min(4000, self.settings.model.max_output_tokens),
            temperature=0.0,
        )
        try:
            response, invocation = self.gateway.structured(
                request,
                project_id=claim.project_id,
                snapshot_id=snapshot.id,
                purpose="claim_mapping",
            )
        except (PolicyError, ModelError) as exc:
            return _Adjudication(refusal=f"model call failed: {exc}")
        try:
            payload = MappingAdjudicationPayload.model_validate(response.structured or {})
        except ValidationError as exc:
            return _Adjudication(
                invocation_id=invocation.id,
                refusal=f"schema validation failed ({exc.error_count()} error(s))",
            )

        by_ref = {value: citable[index] for index, value in refs.items()}
        cited_analyses: list[SpanAnalysis] = []
        for item in payload.evidence:
            analysis = by_ref.get(item.span_ref)
            if analysis is None:
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal=f"evidence cited an unsupplied span reference {item.span_ref!r}",
                )
            cited_analyses.append(analysis)

        if payload.status == ClaimMappingStatus.PRESENT.value:
            if not declared:
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal=(
                        "PRESENT was rejected because the claim declares no acceptance signals"
                    ),
                )
            if not cited_analyses:
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal="PRESENT was rejected because no code span was cited",
                )
            code_signals: set[str] = set()
            for analysis in cited_analyses:
                code_signals.update(
                    matched_signals(analysis.candidate.text, declared, code_only=True)
                )
            missing = [signal for signal in declared if signal not in code_signals]
            if missing:
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal=(
                        "PRESENT was rejected because declared signal(s) "
                        + ", ".join(missing)
                        + " do not appear in executable code within the cited spans"
                    ),
                )
            if not any(analysis.definitional for analysis in cited_analyses):
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal=(
                        "PRESENT was rejected because no cited span is a definition-level span"
                    ),
                )
        elif payload.status == ClaimMappingStatus.CONFLICTING.value:
            contradicted = [
                item for item in payload.evidence if item.relation == "CONTRADICTS"
            ]
            if not contradicted:
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal="CONFLICTING was rejected without a CONTRADICTS evidence span",
                )
            if not any(analysis.forbidden_signals for analysis in cited_analyses):
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal=(
                        "CONFLICTING was rejected because no cited span contains a forbidden signal"
                    ),
                )
        elif payload.status == ClaimMappingStatus.ABSENT.value:
            found = {
                signal for analysis in collection.analyses for signal in analysis.code_signals
            }
            if any(signal in found for signal in declared):
                return _Adjudication(
                    invocation_id=invocation.id,
                    refusal=(
                        "ABSENT was rejected because declared signals were found in executable "
                        "code during discovery"
                    ),
                )

        cited_rows: list[MappingEvidence] = []
        for item, analysis in zip(payload.evidence, cited_analyses):
            cited_rows.append(
                self._build_evidence(
                    analysis,
                    snapshot=snapshot,
                    author=MappingAuthor.MODEL,
                    relation=MappingEvidenceRelation(item.relation),
                    explanation=item.explanation,
                    matched=item.matched_signals,
                )
            )
        return _Adjudication(
            payload=payload,
            invocation_id=invocation.id,
            cited=cited_rows,
        )

    # ---------------------------------------------------------------- manual path
    def set_manual_status(
        self,
        claim: ResearchClaim,
        snapshot: RepositorySnapshot,
        *,
        status: ClaimMappingStatus,
        reason: str,
        evidence_spans: list[str] | None = None,
        actor: str = "local-user",
    ) -> MappingOutcome:
        reason = reason.strip()
        if not reason:
            raise UsageError(
                "A manual mapping needs --reason",
                hint="Record the human judgement that justifies the status.",
            )
        repository = self.repositories.get(snapshot.repository_id)
        if claim.project_id != repository.project_id:
            raise UsageError("The claim and snapshot belong to different projects")
        spans = evidence_spans or []
        rows: list[MappingEvidence] = []
        for spec in spans:
            path, start, end = parse_span_spec(spec)
            file = self.files.get_by_path(snapshot.id, path)
            if file is None:
                raise NotFoundError(f"{path} is not part of snapshot {snapshot.id}")
            lines = self.files.read_span(file.id, start, end)
            if not lines:
                raise UsageError(f"{spec} is outside {path}'s indexed lines")
            excerpt = "\n".join(text for _number, text in lines)
            symbol = self._enclosing_symbol(file.id, start)
            strength = (
                EvidenceStrength.STRONG
                if symbol is not None and symbol.start_line >= start
                else EvidenceStrength.MODERATE
            )
            rows.append(
                MappingEvidence(
                    id="mev_" + new_ulid(),
                    mapping_id="pending",
                    snapshot_id=snapshot.id,
                    file_id=file.id,
                    symbol_id=symbol.id if symbol else None,
                    qualified_name=symbol.qualified_name if symbol else None,
                    path=path,
                    start_line=start,
                    end_line=end,
                    relation=(
                        MappingEvidenceRelation.CONTRADICTS
                        if status is ClaimMappingStatus.CONFLICTING
                        else MappingEvidenceRelation.IMPLEMENTS
                    ),
                    strength=strength,
                    excerpt=excerpt,
                    excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    explanation=reason[:2000],
                    source="MANUAL",
                    matched_signals=matched_signals(excerpt, claim.expected_code_signals),
                )
            )
        if status in (ClaimMappingStatus.PRESENT, ClaimMappingStatus.CONFLICTING) and not rows:
            raise UsageError(
                f"A manual {status.value} mapping needs --evidence PATH:START-END",
                hint="Point at the exact lines that support the judgement.",
            )
        gaps: list[str] = []
        if status is ClaimMappingStatus.PRESENT:
            found = {signal for row in rows for signal in row.matched_signals}
            missing = [
                signal for signal in claim.expected_code_signals if signal not in found
            ]
            if missing:
                raise ConflictError(
                    "The evidence spans do not contain the claim's declared signals: "
                    + ", ".join(missing),
                    hint="Cite spans that contain them, or lower the status to PARTIAL.",
                )
            if not claim.expected_code_signals:
                gaps.append(
                    "Manual PRESENT without declared acceptance signals: the verdict rests on "
                    "human review of the cited spans."
                )
            if not any(row.strength is EvidenceStrength.STRONG for row in rows):
                gaps.append(
                    "No cited span covers a definition start; the evidence is line-level only."
                )
        if status is ClaimMappingStatus.CONFLICTING and not any(
            matched_signals(row.excerpt, claim.forbidden_code_signals) for row in rows
        ):
            gaps.append(
                "The cited spans do not contain a forbidden signal; the conflict is a human "
                "judgement."
            )
        decision = MappingDecision(
            status=status,
            reason=reason,
            rationale=reason,
            gaps=gaps,
            counter_evidence=(
                [row.excerpt[:300] for row in rows[:3]]
                if status is ClaimMappingStatus.CONFLICTING
                else []
            ),
        )
        manual_notes = ["manual mapping recorded with reviewer attribution"]
        if status is ClaimMappingStatus.NOT_CHECKED:
            manual_notes.append(
                "NOT_CHECKED records that no mapping check was performed; it is neither "
                "ABSENT nor UNCERTAIN and must not be read as an absence verdict."
            )
        mapping, evidence_rows = self._persist_mapping(
            claim=claim,
            snapshot=snapshot,
            decision=decision,
            evidence=rows,
            author=MappingAuthor.USER,
            model_invocation_id=None,
            details={
                "manual_override": True,
                "reviewer": actor,
                "rule_status": None,
            },
            notes=manual_notes,
            rejected_model=False,
        )
        return MappingOutcome(
            mapping=mapping,
            evidence=evidence_rows,
            decision=decision,
            model_used=False,
            model_rejected=False,
            notes=manual_notes,
        )

    # ------------------------------------------------------------- persistence
    def _persist_mapping(
        self,
        *,
        claim: ResearchClaim,
        snapshot: RepositorySnapshot,
        decision: MappingDecision,
        evidence: list[MappingEvidence],
        author: MappingAuthor,
        model_invocation_id: str | None,
        details: dict,
        notes: list[str],
        rejected_model: bool,
    ) -> tuple[ClaimMapping, list[MappingEvidence]]:
        existing = self.mappings.find_for_claim(claim.id, snapshot.id)
        mapping_id = existing.id if existing else "map_" + new_ulid()
        now = utcnow()
        mapping = ClaimMapping(
            id=mapping_id,
            project_id=claim.project_id,
            claim_id=claim.id,
            snapshot_id=snapshot.id,
            status=decision.status,
            status_reason=decision.reason[:4000],
            rationale=decision.rationale[:8000],
            gaps=decision.gaps[:50],
            counter_evidence=decision.counter_evidence[:50],
            validation_method=decision.validation_method[:2000],
            rule_pack_version=MAPPING_RULE_PACK_VERSION
            + ("+manual" if author is MappingAuthor.USER else ""),
            created_by=author,
            model_invocation_id=model_invocation_id,
            review_status=(
                ClaimReviewStatus.ACCEPTED
                if author is MappingAuthor.USER
                else ClaimReviewStatus.NEEDS_REVIEW
            ),
            details={
                **details,
                "notes": notes,
                "previous_status": existing.status.value if existing else None,
            },
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        rows = [
            evidence_row.model_copy(update={"mapping_id": mapping.id})
            for evidence_row in evidence
        ]
        with self.db.transaction():
            if existing:
                self.mappings.update(mapping)
            else:
                self.mappings.create(mapping)
            self.mappings.replace_evidence(mapping.id, rows)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=(
                        EventType.CLAIM_MAPPING_UPDATED
                        if existing
                        else EventType.CLAIM_MAPPING_CREATED
                    ),
                    project_id=claim.project_id,
                    snapshot_id=snapshot.id,
                    entity_type="claim_mapping",
                    entity_id=mapping.id,
                    actor_kind=(
                        ActorKind.MODEL if author is MappingAuthor.MODEL else ActorKind.USER
                    ),
                    actor_name=(
                        "SYSTEM:mapping"
                        if author is MappingAuthor.DETERMINISTIC
                        else ("model" if author is MappingAuthor.MODEL else "local-user")
                    ),
                    payload={
                        "claim_id": claim.id,
                        "status": mapping.status.value,
                        "previous_status": mapping.details.get("previous_status"),
                        "evidence": len(rows),
                        "rule_pack_version": mapping.rule_pack_version,
                        "model_invocation_id": model_invocation_id,
                        "model_rejected": rejected_model,
                    },
                )
            )
            if rejected_model:
                self.events.append(
                    WorkflowEvent(
                        event_id="evt_" + new_ulid(),
                        event_type=EventType.CLAIM_MAPPING_REJECTED,
                        project_id=claim.project_id,
                        snapshot_id=snapshot.id,
                        entity_type="claim_mapping",
                        entity_id=mapping.id,
                        actor_kind=ActorKind.SYSTEM,
                        actor_name="SYSTEM:mapping",
                        payload={
                            "claim_id": claim.id,
                            "reason": str(details.get("model_rejection", ""))[:1000],
                        },
                    )
                )
        return mapping, rows

    # ------------------------------------------------------------------- matrix
    def build_matrix(
        self,
        project: Project,
        snapshot: RepositorySnapshot,
        *,
        claims: list[ResearchClaim] | None = None,
        repository: Repository | None = None,
    ) -> ClaimToCodeMatrix:
        repository = repository or self.repositories.get(snapshot.repository_id)
        selected = claims or self.claims.list(project_id=project.id, limit=1_000_000)
        mappings = {
            mapping.claim_id: mapping
            for mapping in self.mappings.list_by_snapshot(snapshot.id)
        }
        rows: list[ClaimMatrixRow] = []
        counts = {status.value: 0 for status in ClaimMappingStatus}
        counts["UNMAPPED"] = 0
        unmapped: list[str] = []
        rejected: list[str] = []
        for claim in selected:
            if claim.review_status is ClaimReviewStatus.REJECTED:
                rejected.append(claim.id)
            mapping = mappings.get(claim.id)
            if mapping is None:
                if claim.review_status is not ClaimReviewStatus.REJECTED:
                    unmapped.append(claim.id)
                rows.append(
                    ClaimMatrixRow(
                        claim_id=claim.id,
                        document_id=claim.document_id,
                        ordinal=claim.ordinal,
                        title=claim.title,
                        claim_type=claim.claim_type,
                        review_status=claim.review_status.value,
                    )
                )
                continue
            counts[mapping.status.value] = counts.get(mapping.status.value, 0) + 1
            verified: list[VerifiedEvidence] = []
            for evidence in self.mappings.list_evidence(mapping.id):
                ok, reason = self.verify_evidence(evidence, snapshot.id)
                verified.append(
                    VerifiedEvidence(evidence=evidence, verified=ok, verification=reason)
                )
            rows.append(
                ClaimMatrixRow(
                    claim_id=claim.id,
                    document_id=claim.document_id,
                    ordinal=claim.ordinal,
                    title=claim.title,
                    claim_type=claim.claim_type,
                    review_status=claim.review_status.value,
                    mapping_status=mapping.status.value,
                    status_reason=mapping.status_reason,
                    rationale=mapping.rationale,
                    created_by=mapping.created_by.value,
                    rule_pack_version=mapping.rule_pack_version,
                    validation_method=mapping.validation_method,
                    gaps=list(mapping.gaps),
                    counter_evidence=list(mapping.counter_evidence),
                    evidence=verified,
                )
            )
        if not selected:
            unmapped = []
        if selected:
            counts["UNMAPPED"] = len(unmapped)
        rows.sort(key=lambda row: (row.ordinal, row.claim_id))
        return ClaimToCodeMatrix(
            protocol_version=RESEARCH_PROTOCOL_VERSION,
            project_id=project.id,
            snapshot_id=snapshot.id,
            snapshot_identity=snapshot.identity_label,
            repository_name=repository.name,
            rule_pack_version=MAPPING_RULE_PACK_VERSION,
            generated_at=utcnow(),
            rows=rows,
            counts=counts,
            claims_total=len(selected),
            unmapped_claim_ids=unmapped,
            rejected_claim_ids=rejected,
        )

    def verify_evidence(
        self, evidence: MappingEvidence, snapshot_id: str
    ) -> tuple[bool, str]:
        file = self.files.get_by_path(snapshot_id, evidence.path)
        if file is None:
            return False, "the path is not part of the pinned snapshot"
        if evidence.end_line > file.line_count:
            return False, "the cited line range is beyond the indexed file"
        lines = self.files.read_span(file.id, evidence.start_line, evidence.end_line)
        if not lines:
            return False, "the cited line range contains no indexed lines"
        actual = "\n".join(text for _number, text in lines)
        digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
        if digest != evidence.excerpt_sha256:
            return False, "the stored excerpt hash does not match the pinned index"
        if actual != evidence.excerpt:
            return False, "the stored excerpt text does not match the pinned index"
        if evidence.symbol_id is not None:
            symbol = self.symbols.get(evidence.symbol_id)
            if symbol.snapshot_id != snapshot_id:
                return False, "the cited symbol belongs to another snapshot"
        return True, "excerpt matches the pinned index exactly"

    # ------------------------------------------------------------------ helpers
    def _repository_for(self, project: Project, snapshot: RepositorySnapshot) -> Repository:
        repository = self.repositories.get(snapshot.repository_id)
        if repository.project_id != project.id:
            raise UsageError("The snapshot belongs to a different project")
        return repository

    def _derived_signals(self, claim: ResearchClaim) -> list[str]:
        identifiers = derive_identifiers(
            " ".join([claim.title, claim.statement, claim.implementation_requirement])
        )
        return [identifier for identifier in identifiers if len(identifier) >= 3][:10]

    def _enclosing_symbol(self, file_id: str, line: int):  # type: ignore[no-untyped-def]
        matches = [
            symbol
            for symbol in self.symbols.list_by_file(file_id)
            if symbol.start_line <= line <= symbol.end_line
        ]
        if not matches:
            return None
        return min(matches, key=lambda symbol: symbol.end_line - symbol.start_line)

    def _build_evidence(
        self,
        analysis: SpanAnalysis,
        *,
        snapshot: RepositorySnapshot,
        author: MappingAuthor,
        relation: MappingEvidenceRelation,
        explanation: str | None = None,
        matched: list[str] | None = None,
    ) -> MappingEvidence:
        candidate = analysis.candidate
        excerpt = candidate.text
        return MappingEvidence(
            id="mev_" + new_ulid(),
            mapping_id="pending",
            snapshot_id=snapshot.id,
            file_id=candidate.file_id,
            symbol_id=candidate.symbol_id,
            qualified_name=candidate.qualified_name,
            path=candidate.path,
            start_line=candidate.start_line,
            end_line=candidate.end_line,
            relation=relation,
            strength=analysis.strength,
            excerpt=excerpt,
            excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            explanation=(explanation or analysis.explanation)[:4000],
            source=(
                "MODEL"
                if author is MappingAuthor.MODEL
                else ("MANUAL" if author is MappingAuthor.USER else "AST")
            ),
            matched_signals=(
                matched if matched is not None else analysis.matched_signals
            ),
        )


def parse_span_spec(spec: str) -> tuple[str, int, int]:
    """Parse ``path:START-END`` into a validated span."""
    match = re.match(
        r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$", spec.strip()
    )
    if match is None:
        raise UsageError(
            f"{spec!r} is not a span",
            hint="Use PATH:START-END, for example src/attention.py:30-39.",
        )
    path = match.group("path")
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        raise UsageError(f"{spec!r} has an invalid line range")
    return path, start, end


__all__ = [
    "ClaimMappingService",
    "MappingOutcome",
    "MappingRun",
    "parse_span_spec",
]

"""Research memo generation.

The memo is assembled deterministically from stored evidence, never written by a
model in this phase. Every section either cites a claim/passage or says that the
information is not in the supplied document.

Memos are append-only versions. The *source interpretation* is delimited by
markers and hashed; a later version reuses that block verbatim (verified against
the hash) and appends observed deviations, so the original reading of the paper
is never silently rewritten.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    ClaimReviewStatus,
    ClaimType,
    EventType,
    PassageKind,
    RiskCategory,
)
from mayaworkbench.domain.models import (
    ClaimMapping,
    ClaimRiskFactor,
    MappingEvidence,
    Project,
    RepositorySnapshot,
    ResearchClaim,
    ResearchDocument,
    ResearchMemo,
    SourcePassage,
    WorkflowEvent,
)
from mayaworkbench.errors import ConflictError
from mayaworkbench.ids import new_ulid
from mayaworkbench.mapping.service import ClaimMappingService
from mayaworkbench.research.claims import verify_source_location
from mayaworkbench.research.dag import validate_claim_dag
from mayaworkbench.research.models import (
    ClaimDagValidation,
    ClaimToCodeMatrix,
    MemoBuild,
    ReadinessAssessment,
)
from mayaworkbench.research.passages import normalize_quote
from mayaworkbench.research.readiness import ReadinessInput, assess_readiness
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    ClaimMappingStore,
    EventStore,
    FileStore,
    IssueStore,
    ProjectStore,
    ResearchClaimStore,
    ResearchDocumentStore,
    ResearchMemoStore,
    SnapshotStore,
    SourcePassageStore,
)
from mayaworkbench.version import MAYA_VERSION, MEMO_PROTOCOL_VERSION

INTERPRETATION_START = "<!-- maya:interpretation:start -->"
INTERPRETATION_END = "<!-- maya:interpretation:end -->"
DEVIATIONS_START = "<!-- maya:deviations:start -->"
DEVIATIONS_END = "<!-- maya:deviations:end -->"

@dataclass
class _MemoContext:
    claims: list[ResearchClaim] = field(default_factory=list)
    dag: ClaimDagValidation | None = None
    documents: list[ResearchDocument] = field(default_factory=list)
    mappings: list[ClaimMapping] = field(default_factory=list)
    evidence: list[MappingEvidence] = field(default_factory=list)
    risk_factors: list[ClaimRiskFactor] = field(default_factory=list)
    matrix: ClaimToCodeMatrix | None = None
    readiness: ReadinessAssessment | None = None


_INTERPRETATION_RE = re.compile(
    re.escape(INTERPRETATION_START) + r"(.*?)" + re.escape(INTERPRETATION_END), re.S
)
_DEVIATIONS_RE = re.compile(
    re.escape(DEVIATIONS_START) + r"(.*?)" + re.escape(DEVIATIONS_END), re.S
)
_DATA_HINTS = re.compile(
    r"(?i)\b(dataset|data set|corpus|benchmark|train(?:ing)? set|test set|validation set|"
    r"imagenet|cifar|wikitext|squad|glue|superglue|mnist)\b"
)
_COMPUTE_HINTS = re.compile(
    r"(?i)\b(gpu|tpus?|a100|h100|v100|rtx|compute|gpu[- ]hours?|training hours?|epochs)\b"
)


class MemoService:
    def __init__(
        self,
        db: Database,
        *,
        projects: ProjectStore,
        documents: ResearchDocumentStore,
        passages: SourcePassageStore,
        claims: ResearchClaimStore,
        memos: ResearchMemoStore,
        snapshots: SnapshotStore,
        files: FileStore,
        issues: IssueStore,
        mappings: ClaimMappingStore,
        mapping_service: ClaimMappingService,
        artifacts: ArtifactStore,
        events: EventStore,
        settings: Settings,
    ):
        self.db = db
        self.projects = projects
        self.documents = documents
        self.passages = passages
        self.claims = claims
        self.memos = memos
        self.snapshots = snapshots
        self.files = files
        self.issues = issues
        self.mappings = mappings
        self.mapping_service = mapping_service
        self.artifacts = artifacts
        self.events = events
        self.settings = settings

    # -------------------------------------------------------------------- build
    def build(
        self,
        project: Project,
        *,
        document: ResearchDocument | None = None,
        snapshot: RepositorySnapshot | None = None,
        question: str | None = None,
        deviations: list[str] | None = None,
        append: bool = False,
        title: str | None = None,
    ) -> MemoBuild:
        context = self._gather(project, document=document, snapshot=snapshot)
        if not context.claims and not document:
            raise ConflictError(
                "This project has no claims or research documents to memo",
                hint="Register a document with `maya research add` and extract claims first.",
            )
        claims = context.claims
        dag = context.dag
        documents = context.documents
        risk_factors = context.risk_factors
        evidence = context.evidence
        matrix = context.matrix
        readiness = context.readiness

        interpretation = self._interpretation_block(
            project=project,
            document=document,
            documents=documents,
            claims=claims,
            dag=dag,
            matrix=matrix,
            readiness=readiness,
            risk_factors=risk_factors,
            evidence=evidence,
            question=question,
        )
        interpretation_hash = hashlib.sha256(interpretation.encode("utf-8")).hexdigest()
        previous = self.memos.latest(
            project_id=project.id,
            document_id=document.id if document else None,
            snapshot_id=snapshot.id if snapshot else None,
        )
        if append and previous is None:
            raise ConflictError(
                "There is no previous memo version to append to",
                hint="Create the memo first without --append.",
            )
        deviations_block = self._deviations_block(
            deviations or [], previous=previous if append else None
        )
        if append and previous is not None:
            previous_match = _INTERPRETATION_RE.search(previous.body_markdown)
            if previous_match is None:
                raise ConflictError(
                    "The previous memo has no source-interpretation block",
                    hint="Its format is not recognised; create a new memo instead of appending.",
                )
            previous_interpretation = previous_match.group(1)
            previous_hash = hashlib.sha256(
                previous_interpretation.encode("utf-8")
            ).hexdigest()
            if previous_hash != previous.source_interpretation_sha256:
                raise ConflictError(
                    "The previous memo's source interpretation was modified after it was written",
                    hint=(
                        "Appending would rewrite history. Restore the memo or create a new memo "
                        "instead."
                    ),
                )
            interpretation = previous_interpretation
            interpretation_hash = previous_hash

        memo_title = title or (
            f"Research memo — {document.title}"
            if document
            else f"Research memo — {project.name}"
        )
        body = "\n".join(
            [
                f"# {memo_title}",
                "",
                f"> MayaWorkbench {MAYA_VERSION} · memo protocol {MEMO_PROTOCOL_VERSION} · "
                f"generated {utcnow().isoformat()}",
                "> This memo is assembled from stored evidence. Missing information is stated as "
                "missing, not filled in.",
                "",
                f"{INTERPRETATION_START}{interpretation}{INTERPRETATION_END}",
                "",
                "## 14. Observed deviations and results",
                "",
                f"{DEVIATIONS_START}{deviations_block}{DEVIATIONS_END}",
                "",
            ]
        )
        artifact = self.artifacts.put_text(
            body,
            kind=ArtifactKind.RESEARCH_MEMO,
            media_type="text/markdown; charset=utf-8",
            created_by="memo",
        )
        version = (previous.version + 1) if (append and previous) else 1
        memo = ResearchMemo(
            id="memo_" + new_ulid(),
            project_id=project.id,
            document_id=document.id if document else None,
            snapshot_id=snapshot.id if snapshot else None,
            version=version,
            supersedes_memo_id=previous.id if (append and previous) else None,
            title=memo_title,
            body_markdown=body,
            artifact_id=artifact.id,
            source_interpretation_sha256=interpretation_hash,
            readiness=readiness.model_dump(mode="json"),
            created_at=utcnow(),
        )
        with self.db.transaction():
            self.memos.create(memo)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.RESEARCH_MEMO_CREATED,
                    project_id=project.id,
                    snapshot_id=snapshot.id if snapshot else None,
                    entity_type="research_memo",
                    entity_id=memo.id,
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="SYSTEM:memo",
                    payload={
                        "version": version,
                        "document_id": memo.document_id,
                        "snapshot_id": memo.snapshot_id,
                        "claims": len(claims),
                        "readiness_label": readiness.label,
                        "readiness_score_version": readiness.score_version,
                        "artifact_id": artifact.id,
                        "appended": bool(append and previous),
                    },
                )
            )
        return MemoBuild(
            memo=memo,
            markdown=body,
            artifact_id=artifact.id,
            readiness=readiness,
            dag=dag,
            matrix=matrix,
            warnings=[
                warning
                for document_item in documents
                for warning in document_item.extraction_warnings[:3]
            ][:10],
        )

    # ------------------------------------------------------------------ assess
    def assess(
        self,
        project: Project,
        *,
        document: ResearchDocument | None = None,
        snapshot: RepositorySnapshot | None = None,
    ) -> _MemoContext:
        """Build the readiness/risk assessment without writing a memo."""
        return self._gather(project, document=document, snapshot=snapshot)

    def _gather(
        self,
        project: Project,
        *,
        document: ResearchDocument | None,
        snapshot: RepositorySnapshot | None,
    ) -> "_MemoContext":
        claims = self.claims.list(project_id=project.id, limit=1_000_000)
        if document is not None:
            # Project-level requirements and implementation decisions (no document)
            # belong to every document memo; only other documents' claims are excluded.
            claims = [
                claim for claim in claims if claim.document_id in (None, document.id)
            ]
        dependencies = self.claims.list_dependencies(
            claim_ids=[claim.id for claim in claims]
        )
        dag = validate_claim_dag(
            claims,
            dependencies,
            project_id=project.id,
            document_id=document.id if document else None,
        )
        documents = (
            [document]
            if document
            else self.documents.list_by_project(project.id, limit=200)
        )
        claim_ids = [claim.id for claim in claims]
        risk_factors = self.claims.list_risk_factors(claim_ids) if claim_ids else []
        mappings: list[ClaimMapping] = (
            self.mappings.list_by_snapshot(snapshot.id) if snapshot else []
        )
        evidence: list[MappingEvidence] = (
            self.mappings.list_evidence_for_snapshot(snapshot.id) if snapshot else []
        )
        matrix: ClaimToCodeMatrix | None = None
        unverified = 0
        if snapshot is not None:
            matrix = self.mapping_service.build_matrix(project, snapshot, claims=claims)
            unverified = sum(
                1 for row in matrix.rows for item in row.evidence if not item.verified
            )
        readiness = assess_readiness(
            ReadinessInput(
                project_id=project.id,
                claims=claims,
                mappings=mappings,
                evidence=evidence,
                risk_factors=risk_factors,
                documents=documents,
                snapshot=snapshot,
                dag=dag,
                index_issue_counts=(
                    self.issues.counts_by_kind(snapshot.id) if snapshot else {}
                ),
                coverage_counts=(
                    self.files.coverage_counts(snapshot.id) if snapshot else {}
                ),
                unverified_evidence=unverified,
            )
        )
        return _MemoContext(
            claims=claims,
            dag=dag,
            documents=documents,
            mappings=mappings,
            evidence=evidence,
            risk_factors=risk_factors,
            matrix=matrix,
            readiness=readiness,
        )

    # ---------------------------------------------------------------- sections
    def _interpretation_block(
        self,
        *,
        project: Project,
        document: ResearchDocument | None,
        documents: list[ResearchDocument],
        claims: list[ResearchClaim],
        dag: ClaimDagValidation,
        matrix: ClaimToCodeMatrix | None,
        readiness: ReadinessAssessment,
        risk_factors: list[ClaimRiskFactor],
        evidence: list[MappingEvidence],
        question: str | None,
    ) -> str:
        passages = self._referenced_passages(claims, documents)
        sections: list[str] = []

        sections.append("## 1. Research question\n")
        if question:
            sections.append(question.strip() + "\n")
        elif document:
            sections.append(
                f"How can the method described in **{document.title}** be implemented and "
                "evaluated in this repository?\n"
            )
        else:
            sections.append(
                f"What does the registered research require for project **{project.name}**, and "
                "how much of it is already implemented?\n"
            )

        sections.append("## 2. Paper identity and version\n")
        if not documents:
            sections.append(
                "_No research document is registered. Bibliographic identity is unknown._\n"
            )
        for item in documents:
            sections.extend(self._document_identity(item))

        sections.append("## 3. Central contribution\n")
        roots = [
            claim
            for claim in claims
            if claim.parent_claim_id is None
            and claim.claim_type is ClaimType.EXPLICIT_SOURCE_CLAIM
        ]
        if roots:
            sections.append(
                "_Top-level source claims at the root of the claim DAG. This is a structural "
                "selection, not an editorial summary._\n"
            )
            for claim in roots[:5]:
                sections.append(f"- **{claim.title}** — {claim.statement}")
                reference = self._claim_reference(claim, passages)
                if reference:
                    sections.append(f"  - source: {reference}")
            sections.append("")
        else:
            sections.append(
                "_No explicit top-level source claim exists. The document's contribution is "
                "therefore not asserted here._\n"
            )

        sections.append("## 4. Method breakdown\n")
        if claims:
            sections.extend(self._claim_tree(claims, dag, passages))
        else:
            sections.append("_No claims have been extracted or authored._\n")

        sections.append("## 5. Equations and assumptions\n")
        equations = [
            passage
            for passage in passages.values()
            if passage.kind is PassageKind.EQUATION
        ]
        if equations:
            for passage in sorted(equations, key=lambda item: item.ordinal)[:30]:
                label = passage.equation_label or "unlabelled equation"
                excerpt = normalize_quote(passage.text)[:240]
                sections.append(
                    f"- **{label}** ({self._passage_location(passage)}) — `{excerpt}`"
                )
            sections.append("")
        else:
            sections.append(
                "_No equation was detected in the extracted text. Equations embedded as images "
                "or unsupported font encodings cannot be recovered._\n"
            )
        assumptions = [
            claim
            for claim in claims
            if re.search(r"(?i)\bassum(?:e|es|ed|ption|ptions)\b", claim.statement)
        ]
        if assumptions:
            sections.append("### Assumptions stated or required\n")
            for claim in assumptions[:20]:
                sections.append(
                    f"- [{claim.claim_type.value}] {claim.statement} "
                    f"({self._claim_reference(claim, passages) or 'no source reference'})"
                )
            sections.append("")
        else:
            sections.append("_No assumption is explicitly recorded in the claims._\n")

        sections.append("## 6. Required implementation behaviour\n")
        grouped: dict[ClaimType, list[ResearchClaim]] = {}
        for claim in claims:
            grouped.setdefault(claim.claim_type, []).append(claim)
        for claim_type in (
            ClaimType.EXPLICIT_SOURCE_CLAIM,
            ClaimType.INFERRED_REQUIREMENT,
            ClaimType.IMPLEMENTATION_DECISION,
            ClaimType.USER_REQUIREMENT,
        ):
            items = grouped.get(claim_type)
            if not items:
                continue
            sections.append(f"### {claim_type.value}\n")
            for claim in items:
                requirement = claim.implementation_requirement or claim.statement
                sections.append(f"- **{claim.title}** — {requirement}")
                if claim.claim_type is not ClaimType.EXPLICIT_SOURCE_CLAIM:
                    note = claim.derivation_note or "no derivation note recorded"
                    sections.append(f"  - derivation: {note}")
                reference = self._claim_reference(claim, passages)
                if reference:
                    sections.append(f"  - source: {reference}")
            sections.append("")
        if not claims:
            sections.append("_No required behaviour has been recorded._\n")
        rejected = [
            claim
            for claim in claims
            if claim.review_status is ClaimReviewStatus.REJECTED
        ]
        if rejected:
            sections.append("### Rejected claims (not treated as requirements)\n")
            for claim in rejected:
                note = claim.review_note or "no reason recorded"
                sections.append(f"- **{claim.title}** — rejected: {note}")
            sections.append("")

        sections.append("## 7. Paper-specified vs inferred values and requirements\n")
        sections.append(
            "| category | claim | specified by | note |\n| --- | --- | --- | --- |"
        )
        for claim in claims:
            if claim.claim_type is ClaimType.EXPLICIT_SOURCE_CLAIM:
                specified = "source text"
                note = self._claim_reference(claim, passages) or "no verified reference"
            elif claim.claim_type is ClaimType.USER_REQUIREMENT:
                specified = "user"
                note = claim.created_by_detail or "local user"
            elif claim.claim_type is ClaimType.IMPLEMENTATION_DECISION:
                specified = "engineering decision (not the paper)"
                note = claim.derivation_note or "no derivation note"
            else:
                specified = "inference (not the paper)"
                note = claim.derivation_note or "no derivation note"
            sections.append(
                f"| {claim.claim_type.value} | {_cell(claim.title)} | {specified} | "
                f"{_cell(note)} |"
            )
        if not claims:
            sections.append("| — | — | — | no claims recorded |")
        sections.append("")

        sections.append("## 8. Repository mapping\n")
        if matrix is None:
            sections.append(
                "_No repository snapshot was mapped. Create a snapshot, index it and run "
                "`maya map`._\n"
            )
        else:
            sections.append(
                f"Snapshot `{matrix.snapshot_identity}` of **{matrix.repository_name}** · rule pack "
                f"`{matrix.rule_pack_version}` · {matrix.claims_total} claim(s).\n"
            )
            sections.append(
                "| claim | status | evidence (verified/total) | reason |\n| --- | --- | --- | --- |"
            )
            for row in matrix.rows:
                evidence_ok = sum(1 for item in row.evidence if item.verified)
                sections.append(
                    f"| {_cell(row.title)} | {row.mapping_status or 'UNMAPPED'} | "
                    f"{evidence_ok}/{len(row.evidence)} | {_cell(row.status_reason[:200])} |"
                )
            sections.append("")
            if not matrix.rows:
                sections.append("_The claim-to-code matrix is empty._\n")
            sections.append(
                "Statuses: PRESENT = required behaviour appears implemented; PARTIAL = some "
                "behaviour found; ABSENT = none found in the inspected scope; UNCERTAIN = "
                "insufficient evidence; CONFLICTING = code contradicts the claim; NOT_APPLICABLE "
                "= human decision. PRESENT does not imply experimental validation.\n"
            )

        sections.append("## 9. Unresolved ambiguities\n")
        ambiguities: list[str] = []
        for claim in claims:
            for detail in claim.missing_details:
                ambiguities.append(f"claim {claim.id} ({claim.title}): {detail}")
        for issue in dag.issues:
            ambiguities.append(f"DAG {issue.code}: {issue.detail}")
        if matrix is not None:
            for row in matrix.rows:
                if row.mapping_status not in (None, "PRESENT"):
                    reason = row.status_reason or "no reason recorded"
                    ambiguities.append(
                        f"claim {row.claim_id} mapping is {row.mapping_status}: {reason[:200]}"
                    )
                for gap in row.gaps[:2]:
                    ambiguities.append(f"claim {row.claim_id}: {gap}")
        for item in documents:
            for warning in item.extraction_warnings:
                ambiguities.append(f"{item.title}: {warning}")
            for page in item.missing_pages:
                ambiguities.append(f"{item.title}: page {page} has no extractable text.")
        if not ambiguities:
            sections.append(
                "_No ambiguity is recorded. This means none was captured, not that none exists._\n"
            )
        else:
            sections.extend(f"- {value}" for value in _dedupe(ambiguities)[:40])
            sections.append("")

        sections.append("## 10. Data and compute requirements\n")
        risk_rows = [
            factor
            for factor in risk_factors
            if factor.category
            in (RiskCategory.DATA_ACCESS, RiskCategory.COMPUTE, RiskCategory.LICENSING)
        ]
        if risk_rows:
            for factor in risk_rows:
                sections.append(
                    f"- **{factor.category.value}** · {factor.severity.value} · "
                    f"claim {factor.claim_id}: {factor.detail}"
                )
            sections.append("")
        else:
            sections.append(
                "_No data, compute or licensing requirement is recorded. This is unknown "
                "information, not evidence that none is needed._\n"
            )
        mentioned_data = [
            claim for claim in claims if _DATA_HINTS.search(claim.statement)
        ]
        mentioned_compute = [
            claim for claim in claims if _COMPUTE_HINTS.search(claim.statement)
        ]
        if mentioned_data:
            sections.append("### Dataset mentions without a recorded access requirement\n")
            sections.extend(f"- {claim.title}: {claim.statement[:240]}" for claim in mentioned_data[:10])
            sections.append("")
        if mentioned_compute:
            sections.append("### Compute mentions without a recorded requirement\n")
            sections.extend(
                f"- {claim.title}: {claim.statement[:240]}" for claim in mentioned_compute[:10]
            )
            sections.append("")

        sections.append("## 11. Proposed validation\n")
        validation_claims = [claim for claim in claims if claim.validation_methods]
        if validation_claims:
            for claim in validation_claims:
                for method in claim.validation_methods:
                    sections.append(f"- {claim.title}: {method}")
            sections.append("")
        else:
            sections.append(
                "_No claim declares a validation method. The evaluation factor in the readiness "
                "assessment is therefore unknown._\n"
            )
        test_evidence = [
            evidence_row
            for evidence_row in evidence
            if evidence_row.relation.value == "TEST_COVERS"
        ]
        if test_evidence:
            sections.append("### Tests observed in mapped evidence\n")
            for item in test_evidence[:10]:
                sections.append(f"- `{item.path}:{item.start_line}-{item.end_line}`")
            sections.append("")

        sections.append("## 12. Reproduction limitations\n")
        sections.append(
            f"**Reproduction Readiness / Risk Assessment** — score version "
            f"`{readiness.score_version}`. This is a weighted heuristic, **not a probability**. "
            f"Result: `{readiness.label}`"
            + (
                f" (overall risk {readiness.overall_risk:.2f} over "
                f"{readiness.known_weight:.2f} of the weight)."
                if readiness.overall_risk is not None
                else " (insufficient information for an overall value)."
            )
            + "\n"
        )
        sections.append(
            "| factor | weight | state | risk | explanation |\n| --- | --- | --- | --- | --- |"
        )
        for factor in readiness.factors:
            sections.append(
                f"| {factor.label} | {factor.weight:.2f} | {factor.state} | "
                f"{'—' if factor.risk is None else f'{factor.risk:.2f}'} | "
                f"{_cell(factor.explanation)} |"
            )
        sections.append("")
        if readiness.blockers:
            sections.append("### Blockers\n")
            sections.extend(f"- {value}" for value in readiness.blockers[:20])
            sections.append("")
        if readiness.limitations:
            sections.append("### Limitations\n")
            sections.extend(f"- {value}" for value in readiness.limitations[:20])
            sections.append("")
        sections.append("### Assumptions of this assessment\n")
        sections.extend(f"- {value}" for value in readiness.assumptions)
        sections.append("")

        sections.append("## 13. Source references\n")
        if passages:
            for passage in sorted(passages.values(), key=lambda item: (item.document_id, item.ordinal))[:40]:
                quote = normalize_quote(passage.text)[:180]
                sections.append(
                    f"- `{passage.id}` · {self._passage_location(passage)} — \"{quote}\" "
                    f"(quote sha256 `{passage.quote_hash[:16]}`)"
                )
            if len(passages) > 40:
                sections.append(
                    f"- … {len(passages) - 40} further referenced passage(s) are stored with the "
                    "document."
                )
            sections.append("")
        else:
            sections.append(
                "_No source passage is referenced by any claim. Quotes cannot be verified._\n"
            )
        sections.append(
            "All quotes above are verbatim passages from the stored extracted text; the quote "
            "hash can be re-verified against that text. Extraction tool and version are recorded "
            "per document.\n"
        )
        return "\n".join(sections)

    def _deviations_block(
        self, deviations: list[str], *, previous: ResearchMemo | None
    ) -> str:
        """Deviations are an append-only bullet list; the heading lives outside."""
        lines: list[str] = []
        if previous is not None:
            match = _DEVIATIONS_RE.search(previous.body_markdown)
            if match is not None:
                for line in match.group(1).splitlines():
                    if line.strip().startswith("- "):
                        lines.append(line.strip())
        for value in deviations:
            cleaned = re.sub(r"\s+", " ", value).strip()
            if cleaned:
                lines.append(f"- {cleaned}")
        if not lines:
            lines.append(
                "_This memo records the source interpretation before implementation. Observed "
                "deviations and results are appended later; this section is intentionally "
                "empty._"
                if previous is None
                else "_No new deviations were recorded in this memo version._"
            )
        return "\n" + "\n".join(lines) + "\n"

    # ---------------------------------------------------------------- helpers
    def _document_identity(self, document: ResearchDocument) -> list[str]:
        lines: list[str] = [f"### {document.title}\n"]
        lines.append(f"- document id: `{document.id}` (key `{document.document_key}`, version {document.version})")
        if document.supersedes_document_id:
            lines.append(f"- supersedes: `{document.supersedes_document_id}`")
        lines.append(
            "- authors: "
            + (", ".join(document.authors) if document.authors else "_not stated in the supplied file_")
        )
        lines.append(
            f"- organisation: {document.organization or '_not stated in the supplied file_'}"
        )
        lines.append(
            f"- publication date: {document.published_at or '_not stated in the supplied file_'}"
        )
        lines.append(f"- media: {document.media_kind.value} · sha256 `{document.content_sha256[:16]}…`")
        if document.doi:
            lines.append(f"- DOI: {document.doi}")
        if document.source_url:
            lines.append(f"- source: {document.source_url}")
        lines.append(
            f"- licence: {document.license or '_not stated in the supplied file_'}"
        )
        lines.append(
            f"- extraction: {document.extraction_tool or 'unknown'} "
            f"{document.extraction_version or ''} · {document.passage_count} passage(s)"
        )
        lines.append(
            "- metadata provenance: "
            + ", ".join(
                f"{field}={source}"
                for field, source in sorted(document.metadata.get("sources", {}).items())
            )
        )
        if document.missing_pages:
            lines.append(
                "- missing pages: " + ", ".join(str(page) for page in document.missing_pages)
            )
        if document.extraction_warnings:
            lines.append(
                "- extraction warnings: " + "; ".join(document.extraction_warnings[:6])
            )
        lines.append("")
        return lines

    def _claim_tree(
        self,
        claims: list[ResearchClaim],
        dag: ClaimDagValidation,
        passages: dict[str, SourcePassage],
    ) -> list[str]:
        by_id = {claim.id: claim for claim in claims}
        children: dict[str | None, list[ResearchClaim]] = {}
        for claim in claims:
            parent = claim.parent_claim_id if claim.parent_claim_id in by_id else None
            children.setdefault(parent, []).append(claim)
        for group in children.values():
            group.sort(key=lambda item: (item.ordinal, item.id))
        lines: list[str] = []
        visited: set[str] = set()

        def walk(parent: str | None, depth: int) -> None:
            for claim in children.get(parent, []):
                if claim.id in visited:
                    continue
                visited.add(claim.id)
                marker = "  " * depth + "- "
                reference = self._claim_reference(claim, passages)
                lines.append(
                    f"{marker}**{claim.title}** · `{claim.claim_type.value}` · "
                    f"review `{claim.review_status.value}`"
                    + (f" · {reference}" if reference else "")
                )
                if claim.statement and claim.statement != claim.title:
                    lines.append(f"{'  ' * (depth + 1)}- {claim.statement[:300]}")
                walk(claim.id, depth + 1)

        walk(None, 0)
        for claim in claims:
            if claim.id in visited:
                continue
            lines.append(
                f"- **{claim.title}** · `{claim.claim_type.value}` · (parent outside this set)"
            )
        order_note = (
            "Ordering follows the validated claim DAG; unorderable claims are listed separately: "
            + ", ".join(dag.unorderable[:10])
            if dag.unorderable
            else "Ordering follows the validated claim DAG."
        )
        lines.append("")
        lines.append(f"_{order_note}_")
        lines.append("")
        return lines

    def _claim_reference(
        self, claim: ResearchClaim, passages: dict[str, SourcePassage]
    ) -> str:
        labels: list[str] = []
        for location in claim.source_locations[:2]:
            passage = passages.get(location.passage_id)
            if passage is None:
                labels.append(f"`{location.passage_id}` (missing)")
                continue
            ok, _reason = verify_source_location(passage, location)
            state = "verified" if ok else "UNVERIFIED"
            labels.append(f"{self._passage_location(passage)} [{state}]")
        for passage_id in claim.basis_passage_ids[:2]:
            passage = passages.get(passage_id)
            if passage is not None:
                labels.append(f"basis {self._passage_location(passage)}")
        return "; ".join(labels)

    def _passage_location(self, passage: SourcePassage) -> str:
        parts = [f"`{passage.id}`"]
        if passage.page:
            parts.append(f"p.{passage.page}")
        if passage.section_path:
            parts.append(passage.section_path)
        if passage.equation_label:
            parts.append(passage.equation_label)
        return " · ".join(parts)

    def _referenced_passages(
        self, claims: list[ResearchClaim], documents: list[ResearchDocument]
    ) -> dict[str, SourcePassage]:
        ids: list[str] = []
        for claim in claims:
            ids.extend(location.passage_id for location in claim.source_locations)
            ids.extend(claim.basis_passage_ids)
        referenced = self.passages.find_many(ids)
        for document in documents:
            for passage in self.passages.list_by_document(document.id):
                if passage.kind is PassageKind.EQUATION:
                    referenced.setdefault(passage.id, passage)
        return referenced


def _cell(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text.replace("|", "\\|")[:400] or "—"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = ["MemoService", "INTERPRETATION_START", "INTERPRETATION_END"]

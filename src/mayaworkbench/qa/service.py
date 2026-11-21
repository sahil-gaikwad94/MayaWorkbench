"""Repository Q&A.

Pipeline:

1. retrieve evidence for the question from the pinned snapshot;
2. if no evidence exists, say so and stop;
3. if no model is usable, return a deterministic evidence digest;
4. otherwise ask the model for a schema-validated answer, then verify every
   citation against the retrieved spans;
5. persist the answer, its context, and its validation result as artifacts.

A model answer that cannot be cited is never presented as an answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
    QaStatus,
    SnapshotKind,
)
from mayaworkbench.domain.models import Citation, Project, QaRecord, Repository, RepositorySnapshot, WorkflowEvent
from mayaworkbench.errors import ModelError, NotFoundError, PolicyError
from mayaworkbench.ids import new_ulid, new_correlation_id
from mayaworkbench.models.base import ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.qa.citations import citation_summary, validate_citations
from mayaworkbench.qa.prompts import QA_RESPONSE_SCHEMA, build_qa_messages
from mayaworkbench.retrieval.models import ContextPack, RetrievalBundle
from mayaworkbench.retrieval.service import RetrievalService
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    EventStore,
    FileStore,
    ProjectStore,
    QaStore,
    RepositoryStore,
    SnapshotStore,
)
from mayaworkbench.version import QA_PROTOCOL_VERSION

log = get_logger("qa.service")

MAX_DIGEST_SYMBOLS = 8
MAX_DIGEST_LINES = 8
MAX_QUOTE_CHARS = 240


class ModelCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = ""
    reason: str = ""

    @field_validator("end_line")
    @classmethod
    def _range_order(cls, value: int, info: Any) -> int:
        start = info.data.get("start_line")
        if start is not None and value < start:
            raise ValueError("end_line must be >= start_line")
        return value


class QaAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_markdown: str
    citations: list[ModelCitation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)


@dataclass
class AskResult:
    record: QaRecord
    bundle: RetrievalBundle
    model_used: bool
    notes: list[str] = field(default_factory=list)


class AskService:
    def __init__(
        self,
        db: Database,
        *,
        projects: ProjectStore,
        repositories: RepositoryStore,
        snapshots: SnapshotStore,
        files: FileStore,
        qa_store: QaStore,
        artifacts: ArtifactStore,
        events: EventStore,
        retrieval: RetrievalService,
        gateway: ModelGateway,
        settings: Settings,
    ):
        self.db = db
        self.projects = projects
        self.repositories = repositories
        self.snapshots = snapshots
        self.files = files
        self.qa_store = qa_store
        self.artifacts = artifacts
        self.events = events
        self.retrieval = retrieval
        self.gateway = gateway
        self.settings = settings

    # -------------------------------------------------------------------- entry
    def ask(
        self,
        snapshot_id: str,
        question: str,
        *,
        token_budget: int | None = None,
        use_model: bool = True,
        selection: str = "greedy",
        use_semantic: bool = False,
        use_ppr: bool = False,
    ) -> AskResult:
        question = question.strip()
        if not question:
            raise NotFoundError("The question is empty")
        snapshot = self.snapshots.get(snapshot_id)
        repository = self.repositories.get(snapshot.repository_id)
        project = self.projects.get(repository.project_id)
        budget = token_budget or self.settings.retrieval.default_token_budget

        bundle = self.retrieval.retrieve(
            snapshot.id,
            question,
            budget=budget,
            selection=selection,  # type: ignore[arg-type]
            use_semantic=use_semantic,
            use_ppr=use_ppr,
        )
        limitations = self._limitations(snapshot)
        digest = self._digest(snapshot, repository, bundle, limitations)
        status = self.gateway.status()
        notes: list[str] = list(bundle.diagnostics.notes)

        if not bundle.context.has_evidence() or not bundle.diagnostics.evidence_sufficient:
            return self._record(
                project=project,
                repository=repository,
                snapshot=snapshot,
                question=question,
                status=QaStatus.INSUFFICIENT_EVIDENCE,
                answer=(
                    digest
                    + "\n\n**No answer was produced.** "
                    + (
                        "The index contains no retrieved evidence for this question, so any "
                        "answer would be speculation."
                        if not bundle.context.has_evidence()
                        else "The retrieved spans cover too little of the question to support "
                        "an answer; nothing was synthesized."
                    )
                ),
                citations=[],
                uncertainties=[
                    "No matching source spans were retrieved for this question."
                    if not bundle.context.has_evidence()
                    else "Retrieved spans did not cover enough of the question to support an answer."
                ],
                unsupported_claims=[],
                bundle=bundle,
                model_invocation_id=None,
                model_used=False,
                notes=[*notes, "insufficient evidence; model was not called"],
            )

        if not use_model or not status.callable:
            reason = "model use was disabled for this request" if not use_model else (
                status.reason or "model unavailable"
            )
            if use_model and status.reason:
                notes.append(f"model unavailable: {status.reason}")
            return self._record(
                project=project,
                repository=repository,
                snapshot=snapshot,
                question=question,
                status=QaStatus.NO_MODEL,
                answer=(
                    digest
                    + "\n\n**No synthesized answer.** "
                    + (f"Reason: {status.reason}." if status.reason else f"Reason: {reason}.")
                ),
                citations=self._citations_from_context(bundle.context),
                uncertainties=[
                    "This response is retrieved evidence only; it has not been synthesized or "
                    "checked as an answer by a model."
                ],
                unsupported_claims=[],
                bundle=bundle,
                model_invocation_id=None,
                model_used=False,
                notes=notes,
            )

        messages = build_qa_messages(
            question=question,
            context=bundle.context,
            snapshot_identity=self._identity(snapshot),
            limitations=limitations,
        )
        request = ModelRequest(
            purpose="repository_qa",
            messages=messages,
            json_schema=QA_RESPONSE_SCHEMA,
            schema_name="repository_answer",
            max_output_tokens=self.settings.model.max_output_tokens,
            temperature=0.0,
        )
        correlation_id = new_correlation_id()
        try:
            response, invocation = self.gateway.structured(
                request,
                project_id=project.id,
                snapshot_id=snapshot.id,
                purpose="repository_qa",
                correlation_id=correlation_id,
            )
        except (PolicyError, ModelError) as exc:
            notes.append(f"model call failed: {exc}")
            failed_invocation = getattr(exc, "invocation_id", None)
            return self._record(
                project=project,
                repository=repository,
                snapshot=snapshot,
                question=question,
                status=QaStatus.MODEL_UNAVAILABLE,
                answer=(
                    digest
                    + "\n\n**No synthesized answer.** The model call did not succeed, so the "
                    f"evidence above is shown without interpretation. Reason: {exc}"
                ),
                citations=self._citations_from_context(bundle.context),
                uncertainties=[f"Model call failed: {exc}"],
                unsupported_claims=[],
                bundle=bundle,
                model_invocation_id=failed_invocation,
                model_used=False,
                notes=notes,
            )

        try:
            payload = QaAnswerPayload.model_validate(response.structured or {})
        except ValidationError as exc:
            notes.append("model output failed schema validation")
            return self._record(
                project=project,
                repository=repository,
                snapshot=snapshot,
                question=question,
                status=QaStatus.FAILED,
                answer=(
                    digest
                    + "\n\n**No answer was produced.** The model response did not match the "
                    "required schema, so it was discarded rather than coerced.\n\n"
                    f"Schema errors: {exc.error_count()}"
                ),
                citations=self._citations_from_context(bundle.context),
                uncertainties=["Model output failed schema validation."],
                unsupported_claims=[],
                bundle=bundle,
                model_invocation_id=invocation.id,
                model_used=True,
                notes=notes,
            )

        citations = validate_citations(
            [
                Citation(
                    path=citation.path,
                    start_line=citation.start_line,
                    end_line=citation.end_line,
                    quote=citation.quote[:2000],
                    reason=citation.reason[:500],
                )
                for citation in payload.citations
            ],
            bundle.context,
        )
        _valid_count, invalid_count = citation_summary(citations)

        # A single fabricated citation discredits the whole answer: any invalid
        # citation means the model cited something that is not in the evidence.
        if bundle.context.has_evidence() and invalid_count > 0:
            notes.append(
                "model answer withheld: "
                f"{invalid_count} of {len(citations)} citation(s) failed verification"
            )
            return self._record(
                project=project,
                repository=repository,
                snapshot=snapshot,
                question=question,
                status=QaStatus.CITATIONS_REJECTED,
                answer=(
                    digest
                    + "\n\n**The model's answer was withheld.** None of its citations could be "
                    "verified against the retrieved source spans, so presenting it would be "
                    "unsupported. Rejected citations are recorded with this QA record."
                ),
                citations=citations,
                uncertainties=[
                    "Model answer rejected: at least one citation could not be verified "
                    "against the retrieved source."
                ],
                unsupported_claims=list(payload.unsupported_claims),
                bundle=bundle,
                model_invocation_id=invocation.id,
                model_used=True,
                notes=notes,
                extra={"rejected_model_answer": payload.answer_markdown},
            )

        final_status = (
            QaStatus.ANSWERED_WITH_UNCERTAINTY if payload.uncertainties else QaStatus.ANSWERED
        )
        uncertainties = list(payload.uncertainties)
        return self._record(
            project=project,
            repository=repository,
            snapshot=snapshot,
            question=question,
            status=final_status,
            answer=payload.answer_markdown,
            citations=citations,
            uncertainties=uncertainties,
            unsupported_claims=list(payload.unsupported_claims),
            bundle=bundle,
            model_invocation_id=invocation.id,
            model_used=True,
            notes=notes,
        )

    # ------------------------------------------------------------------ records
    def _record(
        self,
        *,
        project: Project,
        repository: Repository,
        snapshot: RepositorySnapshot,
        question: str,
        status: QaStatus,
        answer: str,
        citations: list[Citation],
        uncertainties: list[str],
        unsupported_claims: list[str],
        bundle: RetrievalBundle,
        model_invocation_id: str | None,
        model_used: bool,
        notes: list[str],
        extra: dict | None = None,
    ) -> AskResult:
        context = bundle.context
        context_artifact = self.artifacts.put_text(
            json.dumps(context.model_dump(mode="json"), indent=2, sort_keys=True),
            kind=ArtifactKind.CONTEXT_PACK,
            media_type="application/json",
            created_by="qa",
        )
        payload = {
            "protocol_version": QA_PROTOCOL_VERSION,
            "created_at": utcnow().isoformat(),
            "question": question,
            "status": status.value,
            "answer_markdown": answer,
            "citations": [citation.model_dump(mode="json") for citation in citations],
            "uncertainties": uncertainties,
            "unsupported_claims": unsupported_claims,
            "snapshot": {
                "snapshot_id": snapshot.id,
                "repository_id": repository.id,
                "kind": snapshot.kind.value,
                "commit_sha": snapshot.commit_sha,
                "patch_hash": snapshot.patch_hash,
                "indexer_version": snapshot.indexer_version,
            },
            "retrieval": bundle.diagnostics.model_dump(mode="json"),
            "context": {
                "artifact_id": context_artifact.id,
                "token_estimate": context.token_estimate,
                "token_budget": context.token_budget,
                "estimator": context.estimator,
                "files_covered": context.files_covered,
                "truncated": context.truncated,
                "excluded_count": len(context.excluded),
            },
            "model": {
                "used": model_used,
                "invocation_id": model_invocation_id,
                "provider": self.settings.model.provider,
                "model": self.settings.model.model,
            },
            "notes": notes,
            **(extra or {}),
        }
        answer_artifact = self.artifacts.put_text(
            json.dumps(payload, indent=2, sort_keys=True),
            kind=ArtifactKind.QA_ANSWER,
            media_type="application/json",
            created_by="qa",
        )
        record = QaRecord(
            id="qa_" + new_ulid(),
            project_id=project.id,
            snapshot_id=snapshot.id,
            question=question,
            status=status,
            answer_markdown=answer,
            citations=citations,
            uncertainties=uncertainties,
            unsupported_claims=unsupported_claims,
            model_invocation_id=model_invocation_id,
            artifact_id=answer_artifact.id,
            context_token_estimate=context.token_estimate,
            token_budget=context.token_budget,
            details={
                "context_artifact_id": context_artifact.id,
                "notes": notes,
                "diagnostics": bundle.diagnostics.model_dump(mode="json"),
                "files_covered": context.files_covered,
                "truncated": context.truncated,
                "excluded_count": len(context.excluded),
            },
        )
        with self.db.transaction():
            self.qa_store.create(record)
            if status in (QaStatus.ANSWERED, QaStatus.ANSWERED_WITH_UNCERTAINTY):
                event_type = EventType.QA_ANSWERED
            elif status == QaStatus.NO_MODEL:
                event_type = EventType.QA_EVIDENCE_ONLY
            else:
                event_type = EventType.QA_REJECTED
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=event_type,
                    project_id=project.id,
                    repository_id=repository.id,
                    snapshot_id=snapshot.id,
                    entity_type="qa_record",
                    entity_id=record.id,
                    actor_kind=ActorKind.USER,
                    actor_name="local-user",
                    payload={
                        "status": status.value,
                        "question": question[:500],
                        "citations_total": len(citations),
                        "citations_valid": sum(1 for citation in citations if citation.valid),
                        "context_tokens": context.token_estimate,
                        "model_invocation_id": model_invocation_id,
                        "artifact_id": answer_artifact.id,
                    },
                )
            )
        return AskResult(record=record, bundle=bundle, model_used=model_used, notes=notes)

    # ------------------------------------------------------------------ digest
    def _digest(
        self,
        snapshot: RepositorySnapshot,
        repository: Repository,
        bundle: RetrievalBundle,
        limitations: list[str],
    ) -> str:
        lines: list[str] = [
            f"**Retrieval evidence for snapshot `{snapshot.id}`**",
            "",
            f"- repository: `{repository.name}` ({repository.root_path})",
            f"- revision: `{self._identity(snapshot)}`",
            f"- budget: {bundle.context.token_estimate}/{bundle.context.token_budget} estimated tokens "
            f"({bundle.context.estimator})",
        ]
        if bundle.symbols:
            lines.extend(["", "### Symbols matched", ""])
            for symbol in bundle.symbols[:MAX_DIGEST_SYMBOLS]:
                doc = f" — {symbol.docstring.splitlines()[0][:120]}" if symbol.docstring else ""
                lines.append(
                    f"- `{symbol.qualified_name}` ({symbol.kind}) — `{symbol.signature}` — "
                    f"{symbol.path}:{symbol.start_line}-{symbol.end_line}{doc}"
                )
        if bundle.hits:
            lines.extend(["", "### Matching lines", ""])
            for hit in bundle.hits[:MAX_DIGEST_LINES]:
                text = hit.text.strip()
                if len(text) > MAX_QUOTE_CHARS:
                    text = text[:MAX_QUOTE_CHARS] + "…"
                lines.append(f"- {hit.path}:{hit.line_no} — `{text}`")
        if limitations:
            lines.extend(["", "### Index limitations", ""])
            lines.extend(f"- {limitation}" for limitation in limitations)
        lines.extend(
            [
                "",
                f"_Retrieved from the pinned {snapshot.kind.value} snapshot only. "
                "No repository content outside these spans was consulted._",
            ]
        )
        return "\n".join(lines)

    def _citations_from_context(self, context: ContextPack) -> list[Citation]:
        citations: list[Citation] = []
        for item in context.items[:10]:
            quote = ""
            for line in item.text.splitlines():
                _prefix, sep, remainder = line.partition(": ")
                if sep and remainder.strip():
                    quote = remainder.strip()[:MAX_QUOTE_CHARS]
                    break
            citations.append(
                Citation(
                    path=item.path,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    quote=quote,
                    reason=f"retrieved span ({item.reason})",
                    valid=True,
                    validation="retrieved from the pinned index; not model-verified",
                )
            )
        return citations

    def _limitations(self, snapshot: RepositorySnapshot) -> list[str]:
        limitations = [
            "Python files are parsed with the standard-library AST; other languages are "
            "indexed lexically only."
        ]
        try:
            stats = self.files.language_stats(snapshot.id)
        except Exception:  # pragma: no cover - defensive only
            stats = []
        non_python = sorted(
            str(row["language"]) for row in stats if str(row["language"]) not in ("python", "unknown")
        )
        if non_python:
            limitations.append(
                "Lexical-only languages present: " + ", ".join(non_python[:12]) + "."
            )
        if snapshot.kind == SnapshotKind.COMMITTED and snapshot.dirty:
            limitations.append(
                "Uncommitted working-tree changes are not part of this snapshot."
            )
        if snapshot.includes_worktree_patch:
            limitations.append(
                "This snapshot includes an uncommitted patch "
                f"({(snapshot.patch_hash or '')[:12]}), so it is not a committed revision."
            )
        return limitations

    def _identity(self, snapshot: RepositorySnapshot) -> str:
        return snapshot.identity_label


__all__ = ["AskService", "AskResult", "ModelCitation", "QaAnswerPayload"]

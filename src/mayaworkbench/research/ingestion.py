"""Research document registration and extraction.

One call registers one immutable *content version* of a document:

1. hash the bytes and store them as a ``SOURCE_DOCUMENT`` artifact;
2. extract text with a tool whose name and version are recorded;
3. segment the text into passages with exact source ranges;
4. store the extracted text and an extraction report as artifacts;
5. write the document row, its passages and a ``SOURCE_INGESTED`` event in one
   transaction.

Failures are persisted as ``FAILED`` document versions with a report artifact
before the error is raised, so a broken PDF is visible in `maya research list`
instead of vanishing.

Versioning is explicit. Re-registering different bytes under the same
``document_key`` requires ``version_of``; the same bytes are idempotent and
return the existing version.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    DocumentMediaKind,
    DocumentOrigin,
    EventType,
    ExtractionStatus,
)
from mayaworkbench.domain.models import (
    Project,
    ResearchDocument,
    SourcePassage,
    WorkflowEvent,
)
from mayaworkbench.errors import ConflictError, ExtractionError, NotFoundError, UsageError
from mayaworkbench.ids import new_ulid, short_hash
from mayaworkbench.research.fetch import DocumentFetcher, fetcher_from_settings
from mayaworkbench.research.metadata import (
    DocumentMetadata,
    metadata_from_markdown,
    metadata_from_pdf,
    metadata_from_text,
)
from mayaworkbench.research.models import ExtractionReport, IngestResult
from mayaworkbench.research.passages import (
    quote_hash,
    segment_document,
    text_sha256,
)
from mayaworkbench.research.pdf import extract_pdf_text
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    EventStore,
    ProjectStore,
    ResearchClaimStore,
    ResearchDocumentStore,
    SourcePassageStore,
)
from mayaworkbench.version import EXTRACTION_VERSION, RESEARCH_PROTOCOL_VERSION

MEDIA_TYPES = {
    DocumentMediaKind.MARKDOWN: "text/markdown; charset=utf-8",
    DocumentMediaKind.TEXT: "text/plain; charset=utf-8",
    DocumentMediaKind.PDF: "application/pdf",
}

_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
_PDF_SUFFIXES = {".pdf"}


@dataclass
class MetadataOverrides:
    title: str | None = None
    authors: list[str] | None = None
    organization: str | None = None
    published_at: str | None = None
    doi: str | None = None
    source_url: str | None = None
    license: str | None = None


class DocumentIngestionService:
    def __init__(
        self,
        db: Database,
        *,
        projects: ProjectStore,
        documents: ResearchDocumentStore,
        passages: SourcePassageStore,
        claims: ResearchClaimStore,
        artifacts: ArtifactStore,
        events: EventStore,
        settings: Settings,
        fetcher: DocumentFetcher | None = None,
    ):
        self.db = db
        self.projects = projects
        self.documents = documents
        self.passages = passages
        self.claims = claims
        self.artifacts = artifacts
        self.events = events
        self.settings = settings
        self.fetcher = fetcher or fetcher_from_settings(settings)

    # ------------------------------------------------------------------ inputs
    def ingest_file(
        self,
        project: Project,
        path: str | Path,
        *,
        overrides: MetadataOverrides | None = None,
        document_key: str | None = None,
        version_of: str | None = None,
    ) -> IngestResult:
        source = Path(path).expanduser()
        if not source.exists():
            raise NotFoundError(f"{source} does not exist")
        if not source.is_file():
            raise UsageError(f"{source} is not a file")
        limit = self.settings.research.max_document_bytes
        try:
            size = source.stat().st_size
            if size > limit:
                raise UsageError(
                    f"{source.name} is {size} bytes, above the {limit} byte document limit",
                    hint="Raise research.max_document_bytes or register a smaller extract.",
                )
            # Read one byte past the limit so a file that grew after ``stat`` is
            # still rejected without ever materialising the whole input.
            with source.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise ExtractionError(f"Cannot read {source}: {exc}") from exc
        if len(data) > limit:
            raise UsageError(
                f"{source.name} grew past the {limit} byte document limit while being read",
                hint="Copy the file to a stable location and register it again.",
            )
        return self._ingest_bytes(
            project,
            data,
            filename=source.name,
            origin=DocumentOrigin.FILE,
            origin_uri=str(source.resolve()),
            overrides=overrides or MetadataOverrides(),
            document_key=document_key,
            version_of=version_of,
        )

    def ingest_url(
        self,
        project: Project,
        url: str,
        *,
        overrides: MetadataOverrides | None = None,
        document_key: str | None = None,
        version_of: str | None = None,
    ) -> IngestResult:
        fetched = self.fetcher.fetch(url)
        filename = Path(_url_path(fetched.final_url)).name or "document"
        result = self._ingest_bytes(
            project,
            fetched.data,
            filename=filename,
            origin=DocumentOrigin.URL,
            origin_uri=fetched.requested_url,
            overrides=overrides or MetadataOverrides(),
            document_key=document_key or fetched.final_url,
            version_of=version_of,
            media_hint=fetched.media_hint,
            retrieval={
                "requested_url": fetched.requested_url,
                "final_url": fetched.final_url,
                "content_type": fetched.content_type,
                "status": fetched.status,
                "retrieved_at": fetched.retrieved_at.isoformat(),
            },
        )
        return result

    def ingest_doi(
        self,
        project: Project,
        doi: str,
        *,
        overrides: MetadataOverrides | None = None,
        document_key: str | None = None,
        version_of: str | None = None,
    ) -> IngestResult:
        fetched = self.fetcher.fetch_doi(doi)
        filename = Path(_url_path(fetched.final_url)).name or "document"
        merged = overrides or MetadataOverrides()
        if merged.doi is None:
            merged = MetadataOverrides(**{**merged.__dict__, "doi": doi})
        return self._ingest_bytes(
            project,
            fetched.data,
            filename=filename,
            origin=DocumentOrigin.DOI,
            origin_uri=doi,
            overrides=merged,
            document_key=document_key or f"doi:{doi}",
            version_of=version_of,
            media_hint=fetched.media_hint,
            retrieval={
                "requested_url": fetched.requested_url,
                "final_url": fetched.final_url,
                "content_type": fetched.content_type,
                "status": fetched.status,
                "retrieved_at": fetched.retrieved_at.isoformat(),
            },
        )

    # ------------------------------------------------------------------ engine
    def _ingest_bytes(
        self,
        project: Project,
        data: bytes,
        *,
        filename: str,
        origin: DocumentOrigin,
        origin_uri: str | None,
        overrides: MetadataOverrides,
        document_key: str | None,
        version_of: str | None,
        media_hint: str | None = None,
        retrieval: dict | None = None,
    ) -> IngestResult:
        if not data:
            raise ExtractionError("The document is empty")
        limit = self.settings.research.max_document_bytes
        if len(data) > limit:
            raise UsageError(
                f"{filename} is {len(data)} bytes, above the {limit} byte document limit",
                hint="Raise research.max_document_bytes or register a smaller extract.",
            )
        content_sha = hashlib.sha256(data).hexdigest()
        key = document_key or _derive_document_key(
            origin=origin, origin_uri=origin_uri, doi=overrides.doi, filename=filename
        )

        previous: ResearchDocument | None = None
        if version_of:
            previous = self.documents.get(version_of)
            if previous.project_id != project.id:
                raise ConflictError(
                    f"Document {version_of} belongs to another project",
                    hint="Register the version in the project that owns the document.",
                )
            key = previous.document_key
            if previous.content_sha256 == content_sha:
                raise ConflictError(
                    f"These bytes are already registered as version {previous.version} of "
                    f"{previous.title!r}",
                    hint="Nothing was changed; edit the file before adding a new version.",
                )
        else:
            existing = self.documents.find_by_content(project.id, key, content_sha)
            if existing is not None:
                return IngestResult(
                    document=existing,
                    passages=self.passages.list_by_document(existing.id),
                    created=False,
                    warnings=["This exact document content is already registered."],
                )
            latest = self.documents.latest_for_key(project.id, key)
            if latest is not None:
                raise ConflictError(
                    f"{latest.title!r} already has version {latest.version} with different content",
                    hint=(
                        f"Register the new content as a version: "
                        f"`maya research add <path> --version-of {latest.id}`."
                    ),
                )

        version = previous.version + 1 if previous else self.documents.next_version(project.id, key)
        document_id = "doc_" + new_ulid()
        media_kind, media_warnings = detect_media_kind(filename, data, media_hint)

        try:
            text, extraction = _extract_text(data, filename=filename, media_kind=media_kind)
        except ExtractionError as exc:
            return self._store_failure(
                project,
                document_id=document_id,
                key=key,
                version=version,
                previous=previous,
                filename=filename,
                origin=origin,
                origin_uri=origin_uri,
                data=data,
                content_sha=content_sha,
                error=exc,
                warnings=media_warnings,
            )

        document_artifact = self.artifacts.put_bytes(
            data,
            kind=ArtifactKind.SOURCE_DOCUMENT,
            media_type=MEDIA_TYPES[media_kind],
            created_by="ingestion",
        )
        text_artifact = self.artifacts.put_text(
            text,
            kind=ArtifactKind.SOURCE_TEXT,
            media_type="text/plain; charset=utf-8",
            created_by="ingestion",
        )

        segmentation = segment_document(
            text,
            media_kind,
            max_passage_chars=self.settings.research.max_passage_chars,
            min_passage_chars=self.settings.research.min_passage_chars,
            body_start=extraction.metadata.body_start,
            max_passages=self.settings.research.max_passages_per_document,
        )
        warnings = [*media_warnings, *extraction.warnings, *segmentation.warnings]
        if not segmentation.passages and extraction.metadata.body_start > 0:
            warnings.append("DOCUMENT_HAS_ONLY_FRONT_MATTER")

        passages = _build_passages(document_id, segmentation.passages, text)
        metadata = _apply_overrides(
            extraction.metadata,
            overrides,
            origin_uri=origin_uri,
            origin=origin,
            retrieval=retrieval or {},
        )
        title = metadata.title or f"Untitled document ({filename})"

        report = ExtractionReport(
            protocol_version=RESEARCH_PROTOCOL_VERSION,
            document_id=document_id,
            media_kind=media_kind,
            origin=origin,
            tool=extraction.tool,
            tool_version=extraction.version,
            content_sha256=content_sha,
            text_sha256=text_sha256(text),
            char_count=len(text),
            page_count=extraction.page_count,
            missing_pages=extraction.missing_pages,
            warnings=warnings,
            section_count=segmentation.section_count,
            passage_counts=_passage_counts(passages),
            metadata_sources=metadata.sources,
            body_start=metadata.body_start,
        )
        report_artifact = self.artifacts.put_text(
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
            kind=ArtifactKind.EXTRACTION_REPORT,
            media_type="application/json",
            created_by="ingestion",
        )

        document = ResearchDocument(
            id=document_id,
            project_id=project.id,
            document_key=key,
            version=version,
            supersedes_document_id=previous.id if previous else None,
            title=title,
            authors=metadata.authors,
            organization=metadata.organization,
            published_at=metadata.published_at,
            media_kind=media_kind,
            origin=origin,
            origin_uri=origin_uri,
            source_url=metadata.source_url,
            doi=metadata.doi,
            license=metadata.license,
            content_sha256=content_sha,
            size_bytes=len(data),
            document_artifact_id=document_artifact.id,
            text_artifact_id=text_artifact.id,
            extraction_artifact_id=report_artifact.id,
            extraction_status=ExtractionStatus.EXTRACTED,
            extraction_tool=extraction.tool,
            extraction_version=extraction.version,
            extraction_warnings=warnings,
            missing_pages=extraction.missing_pages,
            page_count=extraction.page_count,
            section_count=segmentation.section_count,
            passage_count=len(passages),
            metadata={
                "sources": metadata.sources,
                "extra": metadata.extra,
                "retrieval": retrieval or {},
                "filename": filename,
                "body_start": metadata.body_start,
                "extraction_version": EXTRACTION_VERSION,
            },
            created_at=utcnow(),
            extracted_at=utcnow(),
        )

        with self.db.transaction():
            self.documents.create(document)
            self.passages.insert_many(passages)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.SOURCE_INGESTED,
                    project_id=project.id,
                    entity_type="research_document",
                    entity_id=document.id,
                    actor_kind=ActorKind.USER,
                    actor_name="local-user",
                    payload={
                        "document_key": key,
                        "version": version,
                        "title": title[:200],
                        "media_kind": media_kind.value,
                        "content_sha256": content_sha,
                        "passages": len(passages),
                        "sections": segmentation.section_count,
                        "page_count": extraction.page_count,
                        "warnings": len(warnings),
                        "extraction_tool": extraction.tool,
                        "extraction_version": extraction.version,
                        "supersedes": previous.id if previous else None,
                    },
                )
            )
        return IngestResult(
            document=document,
            passages=passages,
            created=True,
            warnings=warnings,
            report=report,
        )

    # ------------------------------------------------------------------ failure
    def _store_failure(
        self,
        project: Project,
        *,
        document_id: str,
        key: str,
        version: int,
        previous: ResearchDocument | None,
        filename: str,
        origin: DocumentOrigin,
        origin_uri: str | None,
        data: bytes,
        content_sha: str,
        error: ExtractionError,
        warnings: list[str],
    ) -> IngestResult:
        media_kind, _ = detect_media_kind(filename, data, None)
        document_artifact = self.artifacts.put_bytes(
            data,
            kind=ArtifactKind.SOURCE_DOCUMENT,
            media_type=MEDIA_TYPES[media_kind],
            created_by="ingestion",
        )
        message = str(error)
        report = ExtractionReport(
            protocol_version=RESEARCH_PROTOCOL_VERSION,
            document_id=document_id,
            media_kind=media_kind,
            origin=origin,
            tool="none",
            tool_version="",
            content_sha256=content_sha,
            text_sha256="",
            char_count=0,
            warnings=[*warnings, f"EXTRACTION_FAILED: {message}"],
            metadata_sources={},
        )
        report_artifact = self.artifacts.put_text(
            json.dumps(
                {
                    **report.model_dump(mode="json"),
                    "error": message,
                    "hint": error.hint,
                },
                indent=2,
                sort_keys=True,
            ),
            kind=ArtifactKind.EXTRACTION_REPORT,
            media_type="application/json",
            created_by="ingestion",
        )
        document = ResearchDocument(
            id=document_id,
            project_id=project.id,
            document_key=key,
            version=version,
            supersedes_document_id=previous.id if previous else None,
            title=f"Unreadable document ({filename})",
            media_kind=media_kind,
            origin=origin,
            origin_uri=origin_uri,
            content_sha256=content_sha,
            size_bytes=len(data),
            document_artifact_id=document_artifact.id,
            extraction_artifact_id=report_artifact.id,
            extraction_status=ExtractionStatus.FAILED,
            extraction_warnings=[*warnings, f"EXTRACTION_FAILED: {message}"],
            metadata={"filename": filename, "error": message, "hint": error.hint},
            created_at=utcnow(),
        )
        with self.db.transaction():
            self.documents.create(document)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.SOURCE_EXTRACTION_FAILED,
                    project_id=project.id,
                    entity_type="research_document",
                    entity_id=document.id,
                    actor_kind=ActorKind.USER,
                    actor_name="local-user",
                    payload={
                        "document_key": key,
                        "version": version,
                        "filename": filename,
                        "media_kind": media_kind.value,
                        "error": message[:500],
                    },
                )
            )
        raise ExtractionError(
            f"Could not extract text from {filename}: {message}",
            hint=error.hint,
            detail=f"The failed document version is stored as {document.id}.",
        )

    # --------------------------------------------------------------- reextract
    def reextract(self, document_id: str, *, force: bool = False) -> IngestResult:
        """Re-run extraction with the current tool without changing identity.

        Refused when claims exist: claims point at passage ids and quote hashes
        that a re-segmentation could invalidate. Register a new document version
        instead, which preserves the old evidence.
        """
        document = self.documents.get(document_id)
        if document.extraction_status == ExtractionStatus.FAILED:
            raise ConflictError(
                "This document version failed extraction; register the file again as a new version"
            )
        if self.claims.count(document_id=document_id) > 0:
            raise ConflictError(
                "Claims already reference this document's passages",
                hint=(
                    "Re-extraction could invalidate their quotes. Register the file as a new "
                    "version with `maya research add <path> --version-of <id>`."
                ),
            )
        if not force and document.extraction_version == EXTRACTION_VERSION:
            raise ConflictError(
                f"This document was already extracted with tool version {EXTRACTION_VERSION}",
                hint="Pass --force to re-run extraction anyway.",
            )
        if not document.document_artifact_id:
            raise NotFoundError("The original document bytes are not available")
        data = self.artifacts.read_bytes(document.document_artifact_id)
        project = self.projects.get(document.project_id)
        media_kind, media_warnings = detect_media_kind(
            str(document.metadata.get("filename") or "document"), data, None
        )
        text, extraction = _extract_text(
            data,
            filename=str(document.metadata.get("filename") or "document"),
            media_kind=media_kind,
        )
        text_artifact = self.artifacts.put_text(
            text,
            kind=ArtifactKind.SOURCE_TEXT,
            media_type="text/plain; charset=utf-8",
            created_by="ingestion",
        )
        segmentation = segment_document(
            text,
            media_kind,
            max_passage_chars=self.settings.research.max_passage_chars,
            min_passage_chars=self.settings.research.min_passage_chars,
            body_start=extraction.metadata.body_start,
            max_passages=self.settings.research.max_passages_per_document,
        )
        passages = _build_passages(document.id, segmentation.passages, text)
        warnings = [*media_warnings, *extraction.warnings, *segmentation.warnings]
        report = ExtractionReport(
            protocol_version=RESEARCH_PROTOCOL_VERSION,
            document_id=document.id,
            media_kind=media_kind,
            origin=document.origin,
            tool=extraction.tool,
            tool_version=extraction.version,
            content_sha256=document.content_sha256,
            text_sha256=text_sha256(text),
            char_count=len(text),
            page_count=extraction.page_count,
            missing_pages=extraction.missing_pages,
            warnings=warnings,
            section_count=segmentation.section_count,
            passage_counts=_passage_counts(passages),
            metadata_sources=document.metadata.get("sources", {}),
            body_start=extraction.metadata.body_start,
        )
        report_artifact = self.artifacts.put_text(
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
            kind=ArtifactKind.EXTRACTION_REPORT,
            media_type="application/json",
            created_by="ingestion",
        )
        updated = document.model_copy(
            update={
                "text_artifact_id": text_artifact.id,
                "extraction_artifact_id": report_artifact.id,
                "extraction_status": ExtractionStatus.EXTRACTED,
                "extraction_tool": extraction.tool,
                "extraction_version": extraction.version,
                "extraction_warnings": warnings,
                "missing_pages": extraction.missing_pages,
                "page_count": extraction.page_count,
                "section_count": segmentation.section_count,
                "passage_count": len(passages),
                "extracted_at": utcnow(),
                "metadata": {
                    **document.metadata,
                    "extraction_version": EXTRACTION_VERSION,
                },
            }
        )
        with self.db.transaction():
            self.passages.delete_for_document(document.id)
            self.passages.insert_many(passages)
            self.documents.update_extraction(document.id, updated)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.SOURCE_INGESTED,
                    project_id=project.id,
                    entity_type="research_document",
                    entity_id=document.id,
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="SYSTEM:ingestion",
                    payload={
                        "reextracted": True,
                        "document_key": document.document_key,
                        "version": document.version,
                        "passages": len(passages),
                        "extraction_tool": extraction.tool,
                        "extraction_version": extraction.version,
                        "warnings": len(warnings),
                    },
                )
            )
        return IngestResult(
            document=updated,
            passages=passages,
            created=False,
            warnings=warnings,
            report=report,
        )


# ----------------------------------------------------------------------- helpers
def detect_media_kind(
    filename: str, data: bytes, hint: str | None
) -> tuple[DocumentMediaKind, list[str]]:
    warnings: list[str] = []
    suffix = Path(filename).suffix.lower()
    is_pdf = data[:1024].lstrip().startswith(b"%PDF-")
    if is_pdf:
        if suffix and suffix not in _PDF_SUFFIXES:
            warnings.append(
                f"EXTENSION_MEDIA_MISMATCH: {filename} has a {suffix} extension but PDF content; "
                "the bytes were treated as PDF."
            )
        return DocumentMediaKind.PDF, warnings
    if suffix in _PDF_SUFFIXES:
        warnings.append(
            f"EXTENSION_MEDIA_MISMATCH: {filename} has a .pdf extension but no PDF header; "
            "the bytes were treated as text."
        )
    if hint == "MARKDOWN" or suffix in _MARKDOWN_SUFFIXES:
        return DocumentMediaKind.MARKDOWN, warnings
    return DocumentMediaKind.TEXT, warnings


def _extract_text(
    data: bytes, *, filename: str, media_kind: DocumentMediaKind
) -> tuple[str, "_Extraction"]:
    if media_kind == DocumentMediaKind.PDF:
        extraction = extract_pdf_text(data)
        pages = [page.text.strip("\n") for page in extraction.pages]
        missing = [page.number for page in extraction.pages if not page.text.strip()]
        if not any(page.strip() for page in pages):
            raise ExtractionError(
                "The PDF has no extractable text layer",
                hint=(
                    "It is probably a scanned document. Run OCR first, or install the optional "
                    'pypdf extra with pip install "mayaworkbench[pdf]".'
                ),
            )
        text = "\n\f\n".join(pages)
        warnings = list(extraction.warnings)
        for number in missing:
            warnings.append(
                f"MISSING_PAGE_TEXT: page {number} produced no text; treat its content as unknown."
            )
        metadata = metadata_from_pdf(extraction, filename=filename)
        metadata.warnings.extend(warnings)
        return text, _Extraction(
            tool=extraction.tool,
            version=extraction.version,
            warnings=metadata.warnings,
            missing_pages=missing,
            page_count=extraction.page_count,
            metadata=metadata,
        )

    text, decode_warnings = _decode_text(data, filename=filename)
    if media_kind == DocumentMediaKind.MARKDOWN:
        metadata = metadata_from_markdown(text, filename=filename)
    else:
        metadata = metadata_from_text(text, filename=filename)
    metadata.warnings.extend(decode_warnings)
    return text, _Extraction(
        tool="maya-text",
        version=EXTRACTION_VERSION,
        warnings=metadata.warnings,
        missing_pages=[],
        page_count=None,
        metadata=metadata,
    )


@dataclass
class _Extraction:
    tool: str
    version: str
    warnings: list[str]
    missing_pages: list[int]
    page_count: int | None
    metadata: DocumentMetadata


def _decode_text(data: bytes, *, filename: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if b"\x00" in data[:4096]:
        raise ExtractionError(
            f"{filename} looks binary, not text",
            hint="Register binary formats as PDF, or convert the file to Markdown/plain text.",
        )
    if data.startswith(b"\xef\xbb\xbf"):
        raw = data.decode("utf-8-sig", errors="strict")
        warnings.append("TEXT_ENCODING_BOM_STRIPPED: a UTF-8 BOM was removed.")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raw = data.decode("utf-16")
        warnings.append("TEXT_ENCODING_UTF16: decoded as UTF-16 based on the byte-order mark.")
    else:
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionError(
                f"{filename} is not valid UTF-8 (byte offset {exc.start})",
                hint="Convert it to UTF-8 and register it again; no text was guessed.",
            ) from exc
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    return text, warnings


def _build_passages(
    document_id: str, chunks, text: str  # type: ignore[no-untyped-def]
) -> list[SourcePassage]:
    passages: list[SourcePassage] = []
    for chunk in chunks:
        # The invariant that makes quote verification meaningful: the stored
        # passage is exactly the slice it claims to be.
        if text[chunk.char_start : chunk.char_end] != chunk.text:
            raise ExtractionError(
                "Internal segmentation error: passage range does not match its text",
                detail=f"offsets {chunk.char_start}-{chunk.char_end} of document {document_id}",
            )
        digest = text_sha256(chunk.text)
        passages.append(
            SourcePassage(
                id="psg_" + short_hash(f"{document_id}\x1f{chunk.ordinal}\x1f{digest}", 32),
                document_id=document_id,
                ordinal=chunk.ordinal,
                kind=chunk.kind,
                page=chunk.page,
                page_end=chunk.page_end,
                section_path=chunk.section_path,
                section_title=chunk.section_title,
                heading_level=chunk.heading_level,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                text=chunk.text,
                text_sha256=digest,
                quote_hash=quote_hash(chunk.text),
                equation_label=chunk.equation_label,
                chunk_index=chunk.chunk_index,
            )
        )
    return passages


def _apply_overrides(
    metadata: DocumentMetadata,
    overrides: MetadataOverrides,
    *,
    origin_uri: str | None,
    origin: DocumentOrigin,
    retrieval: dict | None = None,
) -> DocumentMetadata:
    if overrides.title:
        metadata.title = overrides.title.strip()[:400]
        metadata.sources["title"] = "cli"
    if overrides.authors:
        metadata.authors = [author.strip() for author in overrides.authors if author.strip()]
        metadata.sources["authors"] = "cli"
    if overrides.organization:
        metadata.organization = overrides.organization.strip()
        metadata.sources["organization"] = "cli"
    if overrides.published_at:
        metadata.published_at = overrides.published_at.strip()
        metadata.sources["published_at"] = "cli"
    if overrides.doi:
        metadata.doi = overrides.doi.strip()
        metadata.sources["doi"] = "cli"
    if overrides.source_url:
        metadata.source_url = overrides.source_url.strip()
        metadata.sources["source_url"] = "cli"
    if overrides.license:
        metadata.license = overrides.license.strip()
        metadata.sources["license"] = "cli"
    if metadata.source_url is None and origin in (DocumentOrigin.URL, DocumentOrigin.DOI):
        metadata.source_url = (retrieval or {}).get("final_url") or origin_uri
        metadata.sources["source_url"] = "retrieval"
    return metadata


def _derive_document_key(
    *,
    origin: DocumentOrigin,
    origin_uri: str | None,
    doi: str | None,
    filename: str,
) -> str:
    if doi:
        identity = "doi:" + doi.strip().lower()
    elif origin == DocumentOrigin.URL and origin_uri:
        identity = "url:" + origin_uri.strip().lower()
    elif origin == DocumentOrigin.FILE and origin_uri:
        identity = "file:" + str(Path(origin_uri).resolve()).lower()
    else:
        identity = "name:" + filename.strip().lower()
    return "dok_" + short_hash(identity, 32)


def _passage_counts(passages: list[SourcePassage]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for passage in passages:
        counts[passage.kind.value] = counts.get(passage.kind.value, 0) + 1
    return counts


def _url_path(url: str) -> str:
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+(/.*)?$", url)
    if not match:
        return ""
    return (match.group(1) or "").split("?")[0]


__all__ = [
    "DocumentIngestionService",
    "MetadataOverrides",
    "detect_media_kind",
]

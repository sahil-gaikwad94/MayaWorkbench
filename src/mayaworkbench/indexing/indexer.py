"""Repository indexer.

The index is a rebuildable projection of a pinned snapshot:

* file records carry content hashes and the artifact holding their exact bytes;
* per-line text supports lexical, exact, and regex search;
* symbols, imports, and reference edges are extracted from the Python AST;
* resolution into edges happens after all files are known, with confidence.

Indexing is atomic per snapshot: a rebuild wipes and repopulates the derived
rows in one transaction, so a failure leaves the previous index (if any)
untouched. Failures are recorded as events either way.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EdgeConfidence,
    EdgeType,
    EventType,
    FileStatus,
    IssueKind,
    IssueSeverity,
    ParseCoverage,
    SnapshotStatus,
    SymbolKind,
)
from mayaworkbench.domain.models import (
    IndexIssue,
    IndexedFile,
    Repository,
    RepositorySnapshot,
    Symbol,
    SymbolEdge,
    WorkflowEvent,
)
from mayaworkbench.errors import ConflictError, IndexingError
from mayaworkbench.ids import new_ulid, short_hash, stable_edge_id, stable_file_id, stable_symbol_id
from mayaworkbench.indexing.languages import (
    detect_language,
    is_config_path,
    is_doc_path,
    is_test_path,
    module_name_for_path,
)
from mayaworkbench.indexing.python_parser import ParsedModule, parse_python
from mayaworkbench.indexing.resolve import EdgeResolver, clean_target_name
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.repositories.discovery import Candidate, DiscoveryPolicy, inspect_content
from mayaworkbench.repositories.snapshots import RepositoryService
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    EventStore,
    FileStore,
    IssueStore,
    RepositoryStore,
    SnapshotStore,
    SymbolStore,
)
from mayaworkbench.version import INDEXER_VERSION

log = get_logger("indexing.indexer")

MAX_LINE_CHARS = 8_000
MAX_LINES_PER_FILE = 200_000
READ_BATCH_FILES = 120
READ_BATCH_BYTES = 16 * 1024 * 1024


class IndexReport(BaseModel):
    snapshot_id: str
    repository_id: str
    status: str
    files_indexed: int
    files_skipped: int
    symbols: int
    edges: int
    edges_resolved: int
    edges_unresolved: int
    edges_low_confidence: int
    issues: int
    bytes_indexed: int
    lines_indexed: int
    languages: dict[str, int] = {}
    parse_coverage: dict[str, int] = {}
    duration_ms: int
    manifest_artifact_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


@dataclass
class _FileBatch:
    files: list[IndexedFile] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[SymbolEdge] = field(default_factory=list)
    issues: list[IndexIssue] = field(default_factory=list)
    lines: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)
    aliases: list[tuple[str, str, str, str, int, int, int]] = field(default_factory=list)
    manifest: list[dict] = field(default_factory=list)
    bytes_indexed: int = 0
    lines_indexed: int = 0

    def reset(self) -> None:
        self.files.clear()
        self.symbols.clear()
        self.edges.clear()
        self.issues.clear()
        self.lines.clear()
        self.aliases.clear()
        self.manifest.clear()
        self.bytes_indexed = 0
        self.lines_indexed = 0


class Indexer:
    def __init__(
        self,
        db: Database,
        *,
        files: FileStore,
        symbols: SymbolStore,
        issues: IssueStore,
        snapshots: SnapshotStore,
        repositories: RepositoryStore,
        events: EventStore,
        artifacts: ArtifactStore,
        repository_service: RepositoryService,
        settings: Settings,
    ):
        self.db = db
        self.files = files
        self.symbols = symbols
        self.issues = issues
        self.snapshots = snapshots
        self.repositories = repositories
        self.events = events
        self.artifacts = artifacts
        self.repository_service = repository_service
        self.settings = settings
        self.policy: DiscoveryPolicy = DiscoveryPolicy.from_settings(settings.indexing)

    # ------------------------------------------------------------------- entry
    def index(self, snapshot_id: str) -> IndexReport:
        snapshot = self.snapshots.get(snapshot_id)
        repository = self.repositories.get(snapshot.repository_id)
        if snapshot.status == SnapshotStatus.INDEXING:
            raise ConflictError(
                f"Snapshot {snapshot_id} is already being indexed",
                hint="Wait for the current run or remove the stale status after a crash.",
            )
        previous_status = snapshot.status
        started = utcnow()
        started_monotonic = time.monotonic()

        self._begin(snapshot, repository)
        try:
            discovery, source = self.repository_service.discover(snapshot, repository)
            collector = _Collector(snapshot, repository)

            with self.db.transaction():
                self.files.clear_snapshot_index(snapshot_id)
                discovery_issues = discovery.issues(snapshot_id)
                if discovery_issues:
                    self.issues.insert_many(discovery_issues)
                    collector.issues += len(discovery_issues)
                collector.skipped += len(discovery.skipped)
                for candidates in _batches(discovery.candidates):
                    contents = source.read_many(candidates)
                    batch = _FileBatch()
                    for candidate in candidates:
                        data = contents.get(candidate.path)
                        if data is None:
                            batch.issues.append(
                                _issue(
                                    snapshot_id,
                                    IssueKind.SKIPPED_UNREADABLE,
                                    "content source returned no bytes",
                                    path=candidate.path,
                                    severity=IssueSeverity.WARNING,
                                )
                            )
                            continue
                        self._index_file(snapshot, candidate, data, batch, collector)
                    self._flush(snapshot_id, batch, collector)

                unresolved_issues = self._resolve_edges(snapshot, collector)
                if unresolved_issues:
                    self.issues.insert_many(unresolved_issues)
                    collector.issues += len(unresolved_issues)

                collector.manifest_artifact_id = self._write_manifest(snapshot, collector)
                report = self._finish(snapshot, repository, collector, started_monotonic)
                self._complete(snapshot, repository, collector, report, started)
            return report
        except BaseException as exc:  # noqa: BLE001 - status must be repaired on any exit
            message = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, IndexingError):
                message = str(exc)
            self._fail(snapshot, repository, message, previous_status)
            if isinstance(exc, Exception):
                raise IndexingError(
                    f"Indexing failed for snapshot {snapshot_id}: {message}",
                    hint="The previous index (if any) was preserved. See `maya events` for details.",
                ) from exc
            raise

    # --------------------------------------------------------------- lifecycle
    def _begin(self, snapshot: RepositorySnapshot, repository: Repository) -> None:
        with self.db.transaction():
            self.snapshots.set_indexing(snapshot.id)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.INDEX_STARTED,
                    project_id=repository.project_id,
                    repository_id=repository.id,
                    snapshot_id=snapshot.id,
                    entity_type="snapshot",
                    entity_id=snapshot.id,
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="indexer",
                    payload={
                        "indexer_version": INDEXER_VERSION,
                        "python_version": platform.python_version(),
                        "kind": snapshot.kind.value,
                    },
                )
            )

    def _complete(
        self,
        snapshot: RepositorySnapshot,
        repository: Repository,
        collector: "_Collector",
        report: IndexReport,
        started,
    ) -> None:
        self.snapshots.set_indexed(
            snapshot.id,
            file_count=report.files_indexed,
            symbol_count=report.symbols,
            total_bytes=report.bytes_indexed,
            index_metadata={
                **collector.metadata(report, self.repository_service.policy_hash()),
                "started_at": started.isoformat(),
                "completed_at": utcnow().isoformat(),
            },
        )
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.INDEX_COMPLETED,
                project_id=repository.project_id,
                repository_id=repository.id,
                snapshot_id=snapshot.id,
                entity_type="snapshot",
                entity_id=snapshot.id,
                actor_kind=ActorKind.SYSTEM,
                actor_name="indexer",
                payload={
                    "files_indexed": report.files_indexed,
                    "files_skipped": report.files_skipped,
                    "symbols": report.symbols,
                    "edges": report.edges,
                    "edges_unresolved": report.edges_unresolved,
                    "issues": report.issues,
                    "bytes_indexed": report.bytes_indexed,
                    "duration_ms": report.duration_ms,
                    "manifest_artifact_id": report.manifest_artifact_id,
                },
            )
        )

    def _fail(
        self,
        snapshot: RepositorySnapshot,
        repository: Repository,
        message: str,
        previous_status: SnapshotStatus,
    ) -> None:
        try:
            with self.db.transaction():
                if previous_status == SnapshotStatus.INDEXED:
                    self.snapshots.restore_indexed(snapshot.id, message)
                else:
                    self.snapshots.set_failed(snapshot.id, message)
                self.events.append(
                    WorkflowEvent(
                        event_id="evt_" + new_ulid(),
                        event_type=EventType.INDEX_FAILED,
                        project_id=repository.project_id,
                        repository_id=repository.id,
                        snapshot_id=snapshot.id,
                        entity_type="snapshot",
                        entity_id=snapshot.id,
                        actor_kind=ActorKind.SYSTEM,
                        actor_name="indexer",
                        payload={"error": message[:2000], "preserved_previous_index": previous_status == SnapshotStatus.INDEXED},
                    )
                )
        except Exception:  # pragma: no cover - never mask the original failure
            log.exception("failed to record indexing failure", extra={"snapshot_id": snapshot.id})

    # ------------------------------------------------------------------- files
    def _index_file(
        self,
        snapshot: RepositorySnapshot,
        candidate: Candidate,
        data: bytes,
        batch: _FileBatch,
        collector: "_Collector",
    ) -> None:
        content_sha = hashlib.sha256(data).hexdigest()
        indexable, skip_kind, detail = inspect_content(data, self.policy)
        if not indexable:
            batch.issues.append(
                _issue(
                    snapshot.id,
                    skip_kind or IssueKind.SKIPPED_BINARY,
                    detail,
                    path=candidate.path,
                    severity=IssueSeverity.WARNING,
                )
            )
            collector.skipped += 1
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - inspect_content already filtered
            batch.issues.append(
                _issue(
                    snapshot.id,
                    IssueKind.ENCODING_ERROR,
                    "content is not valid UTF-8",
                    path=candidate.path,
                )
            )
            collector.skipped += 1
            return

        language = detect_language(candidate.path)
        file_id = stable_file_id(snapshot.id, candidate.path)
        raw_lines = text.splitlines()
        if len(raw_lines) > MAX_LINES_PER_FILE:
            batch.issues.append(
                _issue(
                    snapshot.id,
                    IssueKind.BUDGET_EXCEEDED,
                    f"file has {len(raw_lines)} lines; only the first {MAX_LINES_PER_FILE} are indexed",
                    path=candidate.path,
                    severity=IssueSeverity.WARNING,
                )
            )
            raw_lines = raw_lines[:MAX_LINES_PER_FILE]
        truncated = False
        lines: list[tuple[str, int, str]] = []
        for line_number, line in enumerate(raw_lines, start=1):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS]
                truncated = True
            lines.append((file_id, line_number, line))
        if truncated:
            batch.issues.append(
                _issue(
                    snapshot.id,
                    IssueKind.LINE_TRUNCATED,
                    f"lines longer than {MAX_LINE_CHARS} characters were truncated for indexing",
                    path=candidate.path,
                    severity=IssueSeverity.INFO,
                )
            )

        content_artifact_id = None
        if self.settings.indexing.store_file_content:
            artifact = self.artifacts.put_bytes(
                data,
                kind=ArtifactKind.FILE_CONTENT,
                media_type="text/plain; charset=utf-8",
                created_by="indexer",
            )
            content_artifact_id = artifact.id

        status = FileStatus.LEXICAL_ONLY
        coverage = ParseCoverage.LEXICAL_ONLY
        parsed: ParsedModule | None = None
        if language == "python":
            parsed = parse_python(
                text, module_name=module_name_for_path(candidate.path), path=candidate.path
            )
            if parsed.parse_error:
                status = FileStatus.LEXICAL_ONLY
                coverage = ParseCoverage.PARTIAL
                batch.issues.append(
                    _issue(
                        snapshot.id,
                        IssueKind.PARSE_ERROR,
                        parsed.parse_error,
                        path=candidate.path,
                        file_id=file_id,
                        severity=IssueSeverity.WARNING,
                    )
                )
            else:
                status = FileStatus.PARSED
                coverage = ParseCoverage.FULL

        record = IndexedFile(
            id=file_id,
            snapshot_id=snapshot.id,
            path=candidate.path,
            directory=str(candidate.path.rsplit("/", 1)[0]) if "/" in candidate.path else "",
            extension=("." + candidate.path.rsplit(".", 1)[-1].lower()) if "." in candidate.path else "",
            language=language,
            size_bytes=len(data),
            line_count=len(raw_lines),
            content_sha256=content_sha,
            git_blob_sha=candidate.blob_sha,
            mode=candidate.mode,
            status=status,
            parse_coverage=coverage,
            is_test=is_test_path(candidate.path),
            is_doc=is_doc_path(candidate.path),
            is_config=is_config_path(candidate.path),
            is_binary=False,
            content_artifact_id=content_artifact_id,
        )
        batch.files.append(record)
        batch.lines[file_id] = lines
        batch.bytes_indexed += len(data)
        batch.lines_indexed += len(lines)
        batch.manifest.append(
            {
                "path": candidate.path,
                "sha256": content_sha,
                "size_bytes": len(data),
                "language": language,
                "status": status.value,
                "parse_coverage": coverage.value,
                "git_blob_sha": candidate.blob_sha,
                "content_artifact_id": content_artifact_id,
            }
        )
        collector.languages[language] += 1
        collector.coverage[coverage.value] += 1
        collector.bytes_indexed += len(data)
        collector.line_count += len(lines)
        if parsed is not None and parsed.has_main_guard:
            collector.main_guard_paths.add(candidate.path)

        if parsed is not None:
            self._add_symbols(snapshot, record, parsed, batch, collector)
        if parsed is not None and parsed.truncated:
            batch.issues.append(
                _issue(
                    snapshot.id,
                    IssueKind.SYMBOL_LIMIT_REACHED,
                    "file exceeded symbol/call extraction limits; results are partial",
                    path=candidate.path,
                    file_id=file_id,
                    severity=IssueSeverity.WARNING,
                )
            )

    # ----------------------------------------------------------------- symbols
    def _add_symbols(
        self,
        snapshot: RepositorySnapshot,
        record: IndexedFile,
        parsed: ParsedModule,
        batch: _FileBatch,
        collector: "_Collector",
    ) -> None:
        used_ids: set[str] = set()
        id_by_qualified: dict[str, str] = {}
        module_symbol_id: str | None = None
        added = 0
        for parsed_symbol in parsed.symbols:
            symbol_id = stable_symbol_id(
                snapshot.id, record.path, parsed_symbol.kind.value, parsed_symbol.qualified_name
            )
            if symbol_id in used_ids:
                symbol_id = symbol_id + "_" + short_hash(
                    f"{parsed_symbol.qualified_name}:{parsed_symbol.start_line}:{parsed_symbol.name}", 8
                )
            used_ids.add(symbol_id)
            parent_id = (
                id_by_qualified.get(parsed_symbol.parent_qualified)
                if parsed_symbol.parent_qualified
                else None
            )
            symbol = Symbol(
                id=symbol_id,
                snapshot_id=snapshot.id,
                file_id=record.id,
                path=record.path,
                kind=parsed_symbol.kind,
                name=parsed_symbol.name,
                qualified_name=parsed_symbol.qualified_name,
                signature=parsed_symbol.signature,
                start_line=parsed_symbol.start_line,
                end_line=parsed_symbol.end_line,
                docstring=parsed_symbol.docstring,
                decorators=list(parsed_symbol.decorators),
                confidence=EdgeConfidence.HIGH,
                parent_symbol_id=parent_id,
            )
            batch.symbols.append(symbol)
            added += 1
            id_by_qualified[parsed_symbol.qualified_name] = symbol_id
            if parsed_symbol.kind == SymbolKind.MODULE:
                module_symbol_id = symbol_id

        source_id = module_symbol_id or record.id
        edge_ordinals: Counter[tuple[str | None, str, int, str]] = Counter()

        def add_edge(
            *,
            source_symbol_id: str | None,
            edge_type: EdgeType,
            target_name: str,
            line: int,
        ) -> None:
            # The same callee can appear twice on one line (``f(f())``); the
            # ordinal keeps the stable id unique without depending on insert order.
            key = (source_symbol_id, edge_type.value, line, target_name)
            ordinal = edge_ordinals[key]
            edge_ordinals[key] = ordinal + 1
            batch.edges.append(
                self._edge(
                    snapshot.id,
                    source_symbol_id=source_symbol_id,
                    source_file_id=record.id,
                    edge_type=edge_type,
                    target_name=target_name,
                    line=line,
                    ordinal=ordinal,
                )
            )

        for parsed_import in parsed.imports:
            dotted_prefix = "." * parsed_import.level
            target_name = f"{dotted_prefix}{parsed_import.module}"
            if not target_name:
                target_name = "."
            add_edge(
                source_symbol_id=source_id,
                edge_type=EdgeType.IMPORTS,
                target_name=target_name,
                line=parsed_import.line,
            )
            for imported_name, alias in parsed_import.names:
                batch.aliases.append(
                    (
                        record.id,
                        alias,
                        parsed_import.module,
                        imported_name,
                        parsed_import.level,
                        1 if parsed_import.is_from else 0,
                        parsed_import.line,
                    )
                )
        for base in parsed.bases:
            source = id_by_qualified.get(base.class_qualified, source_id)
            add_edge(
                source_symbol_id=source,
                edge_type=EdgeType.INHERITS,
                target_name=clean_target_name(base.base),
                line=base.line,
            )
        for decorator in parsed.decorators:
            source = id_by_qualified.get(decorator.target_qualified, source_id)
            add_edge(
                source_symbol_id=source,
                edge_type=EdgeType.DECORATES,
                target_name=clean_target_name(decorator.decorator),
                line=decorator.line,
            )
        for call in parsed.calls:
            source = id_by_qualified.get(call.caller or "", source_id)
            add_edge(
                source_symbol_id=source,
                edge_type=EdgeType.CALLS,
                target_name=clean_target_name(call.callee),
                line=call.line,
            )
        collector.symbols += added
        

    def _edge(
        self,
        snapshot_id: str,
        *,
        source_symbol_id: str | None,
        source_file_id: str,
        edge_type: EdgeType,
        target_name: str,
        line: int,
        ordinal: int = 0,
    ) -> SymbolEdge:
        edge_id = stable_edge_id(
            snapshot_id,
            source_symbol_id,
            source_file_id,
            edge_type.value,
            f"{line}:{target_name}:{ordinal}",
        )
        return SymbolEdge(
            id=edge_id,
            snapshot_id=snapshot_id,
            source_symbol_id=source_symbol_id,
            source_file_id=source_file_id,
            target_symbol_id=None,
            target_file_id=None,
            edge_type=edge_type,
            target_name=target_name,
            line=line,
            confidence=EdgeConfidence.LOW,
            provenance=f"ast.{edge_type.value.lower()}",
        )

    # --------------------------------------------------------------- resolution
    def _resolve_edges(
        self, snapshot: RepositorySnapshot, collector: "_Collector"
    ) -> list[IndexIssue]:
        files = self.files.iter_files(snapshot.id)
        symbols = self.symbols.resolution_symbols(snapshot.id)
        edges = self.symbols.pending_edges(snapshot.id)
        aliases = self.symbols.import_alias_rows(snapshot.id)
        resolver = EdgeResolver(files=files, symbols=symbols, edges=edges, aliases=aliases)
        updates, unresolved_imports = resolver.resolve_all()
        if updates:
            self.symbols.update_edge_targets(updates)
        confidence_counts = self.symbols.edge_confidence_counts(snapshot.id)
        collector.edges = sum(confidence_counts.values())
        collector.edges_low = confidence_counts.get(EdgeConfidence.LOW.value, 0)
        collector.edges_resolved = collector.edges - collector.edges_low
        issues: list[IndexIssue] = []
        for unresolved_import in unresolved_imports[:500]:
            issues.append(
                _issue(
                    snapshot.id,
                    IssueKind.UNRESOLVED_IMPORT,
                    f"relative import {unresolved_import['target']} could not be resolved",
                    path=unresolved_import["path"],
                    file_id=unresolved_import["file_id"],
                    severity=IssueSeverity.INFO,
                )
            )
        return issues

    # ------------------------------------------------------------------ persist
    def _flush(self, snapshot_id: str, batch: _FileBatch, collector: "_Collector") -> None:
        collector.add_manifest(batch.manifest)
        if batch.files:
            self.files.insert_files(batch.files)
        for file_id, lines in batch.lines.items():
            self.files.insert_lines(snapshot_id, lines)
        if batch.symbols:
            self.symbols.insert_many(batch.symbols)
        if batch.aliases:
            self.files.insert_import_aliases(snapshot_id, batch.aliases)
        if batch.edges:
            self.symbols.insert_edges(batch.edges)
        if batch.issues:
            self.issues.insert_many(batch.issues)
            collector.issues += len(batch.issues)
        collector.files += len(batch.files)
        batch.reset()

    def _write_manifest(
        self, snapshot: RepositorySnapshot, collector: "_Collector"
    ) -> str | None:
        manifest = {
            "version": 1,
            "snapshot_id": snapshot.id,
            "repo_kind": snapshot.kind.value,
            "commit_sha": snapshot.commit_sha,
            "patch_hash": snapshot.patch_hash,
            "indexer_version": INDEXER_VERSION,
            "policy_hash": self.repository_service.policy_hash(),
            "generated_at": utcnow().isoformat(),
            "files": collector.manifest,
        }
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        artifact = self.artifacts.put_bytes(
            payload,
            kind=ArtifactKind.FILE_MANIFEST,
            media_type="application/json",
            created_by="indexer",
        )
        return artifact.id

    def _finish(
        self,
        snapshot: RepositorySnapshot,
        repository: Repository,
        collector: "_Collector",
        started_monotonic: float,
    ) -> IndexReport:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        unresolved = collector.edges - collector.edges_resolved
        languages = {
            row["language"]: int(row["files"]) for row in self.files.language_stats(snapshot.id)
        }
        coverage = dict(collector.coverage)
        warnings: list[str] = []
        if collector.skipped:
            warnings.append(
                f"{collector.skipped} file(s) were skipped while reading; see `maya issues`."
            )
        report = IndexReport(
            snapshot_id=snapshot.id,
            repository_id=repository.id,
            status=SnapshotStatus.INDEXED.value,
            files_indexed=collector.files,
            files_skipped=collector.skipped,
            symbols=collector.symbols,
            edges=collector.edges,
            edges_resolved=collector.edges_resolved,
            edges_unresolved=unresolved,
            edges_low_confidence=collector.edges_low,
            issues=collector.issues,
            bytes_indexed=collector.bytes_indexed,
            lines_indexed=collector.line_count,
            languages=languages,
            parse_coverage=coverage,
            duration_ms=duration_ms,
            manifest_artifact_id=collector.manifest_artifact_id,
            warnings=warnings,
        )
        return report


class _Collector:
    """Mutable aggregation for one indexing run."""

    def __init__(self, snapshot: RepositorySnapshot, repository: Repository):
        self.snapshot_id = snapshot.id
        self.repository_id = repository.id
        self.files = 0
        self.skipped = 0
        self.symbols = 0
        self.edges = 0
        self.edges_resolved = 0
        self.edges_low = 0
        self.issues = 0
        self.bytes_indexed = 0
        self.line_count = 0
        self.languages: Counter[str] = Counter()
        self.coverage: Counter[str] = Counter()
        self.manifest: list[dict] = []
        self.main_guard_paths: set[str] = set()
        self.manifest_artifact_id: str | None = None

    def add_manifest(self, entries: list[dict]) -> None:
        self.manifest.extend(entries)

    def metadata(self, report: IndexReport, policy_hash: str) -> dict:
        return {
            "indexer_version": INDEXER_VERSION,
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "policy_hash": policy_hash,
            "main_guard_paths": sorted(self.main_guard_paths),
            "files_indexed": report.files_indexed,
            "files_skipped": report.files_skipped,
            "symbols": report.symbols,
            "edges": report.edges,
            "edges_resolved": report.edges_resolved,
            "edges_unresolved": report.edges_unresolved,
            "issues": report.issues,
            "bytes_indexed": report.bytes_indexed,
            "lines_indexed": report.lines_indexed,
            "languages": report.languages,
            "parse_coverage": report.parse_coverage,
            "duration_ms": report.duration_ms,
            "manifest_artifact_id": report.manifest_artifact_id,
        }


def _issue(
    snapshot_id: str,
    kind: IssueKind,
    detail: str,
    *,
    path: str | None = None,
    file_id: str | None = None,
    severity: IssueSeverity = IssueSeverity.INFO,
) -> IndexIssue:
    return IndexIssue(
        id="iss_" + new_ulid(),
        snapshot_id=snapshot_id,
        file_id=file_id,
        path=path,
        kind=kind,
        severity=severity,
        detail=detail,
    )


def _batches(
    candidates: Sequence[Candidate],
    *,
    max_files: int = READ_BATCH_FILES,
    max_bytes: int = READ_BATCH_BYTES,
) -> Iterator[list[Candidate]]:
    batch: list[Candidate] = []
    size = 0
    for candidate in candidates:
        if batch and (len(batch) >= max_files or size + candidate.size_bytes > max_bytes):
            yield batch
            batch = []
            size = 0
        batch.append(candidate)
        size += candidate.size_bytes
    if batch:
        yield batch


__all__ = ["Indexer", "IndexReport", "MAX_LINE_CHARS"]

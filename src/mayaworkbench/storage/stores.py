"""Storage repositories.

Each store maps one domain record type to relational rows. Column names match
model field names except for ``*_json`` columns, which are declared explicitly
so JSON never silently becomes the only representation of a queryable field.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.domain.enums import (
    ApprovalStatus,
    ClaimKind,
    ClaimReviewStatus,
    ClaimType,
    DisagreementStatus,
    IssueKind,
    IssueSeverity,
    PaperStatus,
    SnapshotStatus,
    SymbolKind,
    WorkflowState,
    WorkerJobStatus,
)
from mayaworkbench.domain.models import (
    ClaimCitation,
    ClaimDependency,
    ClaimMapping,
    ClaimRevision,
    ClaimRiskFactor,
    EvidenceDisagreement,
    IndexIssue,
    IndexedFile,
    MappingEvidence,
    ModelInvocation,
    Project,
    QaRecord,
    Record,
    Repository,
    RepositorySnapshot,
    ResearchClaim,
    ResearchDocument,
    ResearchExtraction,
    ResearchMemo,
    SourceMetadata,
    SourcePassage,
    Symbol,
    SymbolEdge,
    ApprovalRequest,
    ExecutionWorkspace,
    EvidenceLink,
    ExperimentComparison,
    ExperimentDefinition,
    ExperimentManifest,
    ExperimentRun,
    MemorySource,
    MetricObservation,
    ProjectMemory,
    Task,
    TaskContract,
    TaskDiff,
    TaskPlan,
    TaskReview,
    ToolCall,
    VerificationCheck,
    VerificationFinding,
    VerificationReport,
    VerificationRun,
    WorkflowEvent,
    WorkflowRun,
    WorkerJob,
    Workspace,
    ResourceAttachment,
    EnvironmentSpec,
    WorkspaceSession,
    EvidenceItem,
)
from mayaworkbench.errors import ConflictError, DatabaseError, NotFoundError
from mayaworkbench.ids import short_hash
from mayaworkbench.storage.db import Database
from mayaworkbench.tokenize import expand_line

ModelT = TypeVar("ModelT", bound=Record)


# --------------------------------------------------------------------- helpers
def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        raise DatabaseError(f"Expected JSON text column, found {type(raw).__name__}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatabaseError(f"Corrupted JSON column in local database: {exc}") from exc


def _row_to_model(
    model_cls: type[ModelT],
    row: Any,
    *,
    json_fields: dict[str, tuple[str, Any]] | None = None,
    bool_fields: Sequence[str] = (),
) -> ModelT:
    data: dict[str, Any] = dict(row)
    for field, (column, default) in (json_fields or {}).items():
        data[field] = _load_json(data.pop(column, None), default)
    for field in bool_fields:
        data[field] = bool(data[field])
    return model_cls.model_validate(data)


def _model_to_row(
    model: Record,
    *,
    json_fields: dict[str, tuple[str, Any]] | None = None,
    bool_fields: Sequence[str] = (),
) -> dict[str, Any]:
    data = model.model_dump(mode="json")
    for field, (column, _default) in (json_fields or {}).items():
        data[column] = _dump_json(data.pop(field))
    for field in bool_fields:
        data[field] = 1 if data[field] else 0
    return data


def _insert(db: Database, table: str, row: dict[str, Any]) -> None:
    columns = list(row.keys())
    placeholders = ", ".join(f":{column}" for column in columns)
    db.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        row,
    )


def _insert_many(db: Database, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join(f":{column}" for column in columns)
    db.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        rows,
    )


def _update_row(db: Database, table: str, row: dict[str, Any], *, key: str = "id") -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in row if column != key)
    db.execute(f"UPDATE {table} SET {assignments} WHERE {key} = :{key}", row)


def like_pattern(value: str) -> str:
    """Escape LIKE wildcards; callers add ``ESCAPE '\\'`` to the SQL."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------------- projects
class ProjectStore:
    JSON = {"settings": ("settings_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, project: Project) -> Project:
        _insert(self.db, "projects", _model_to_row(project, json_fields=self.JSON))
        return project

    def get(self, identifier: str) -> Project:
        row = self.db.fetchone(
            """
            SELECT * FROM projects
            WHERE id = ? OR slug = ?
            ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (identifier, identifier, identifier),
        )
        if row is None:
            raise NotFoundError(
                f"Project {identifier!r} not found",
                hint="Run `maya project list` to see available projects.",
            )
        return _row_to_model(Project, row, json_fields=self.JSON)

    def find(self, identifier: str) -> Project | None:
        try:
            return self.get(identifier)
        except NotFoundError:
            return None

    def list(self) -> list[Project]:
        rows = self.db.fetchall("SELECT * FROM projects ORDER BY created_at")
        return [_row_to_model(Project, row, json_fields=self.JSON) for row in rows]

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM projects")[0])

    def touch(self, project_id: str) -> None:
        self.db.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?",
            (utcnow().isoformat(), project_id),
        )


# --------------------------------------------------------------- repositories
class RepositoryStore:
    JSON = {"details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def add(self, repository: Repository) -> Repository:
        _insert(self.db, "repositories", _model_to_row(repository, json_fields=self.JSON))
        return repository

    def get(self, repository_id: str) -> Repository:
        row = self.db.fetchone("SELECT * FROM repositories WHERE id = ?", (repository_id,))
        if row is None:
            raise NotFoundError(f"Repository {repository_id!r} is not registered")
        return _row_to_model(Repository, row, json_fields=self.JSON)

    def list_by_project(self, project_id: str) -> list[Repository]:
        rows = self.db.fetchall(
            "SELECT * FROM repositories WHERE project_id = ? ORDER BY created_at", (project_id,)
        )
        return [_row_to_model(Repository, row, json_fields=self.JSON) for row in rows]

    def find_by_path(self, project_id: str, root_path: str) -> Repository | None:
        row = self.db.fetchone(
            "SELECT * FROM repositories WHERE project_id = ? AND root_path = ?",
            (project_id, root_path),
        )
        return _row_to_model(Repository, row, json_fields=self.JSON) if row else None

    def touch(self, repository_id: str) -> None:
        self.db.execute(
            "UPDATE repositories SET updated_at = ? WHERE id = ?",
            (utcnow().isoformat(), repository_id),
        )

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM repositories")[0])


# ------------------------------------------------------------------ snapshots
class SnapshotStore:
    JSON = {
        "included_paths": ("included_paths_json", []),
        "submodules": ("submodules_json", {}),
        "index_metadata": ("index_metadata_json", {}),
    }
    BOOLS = ("dirty", "includes_worktree_patch")

    def __init__(self, db: Database):
        self.db = db

    def create(self, snapshot: RepositorySnapshot) -> RepositorySnapshot:
        row = _model_to_row(snapshot, json_fields=self.JSON, bool_fields=self.BOOLS)
        _insert(self.db, "repository_snapshots", row)
        return snapshot

    def get(self, snapshot_id: str) -> RepositorySnapshot:
        row = self.db.fetchone("SELECT * FROM repository_snapshots WHERE id = ?", (snapshot_id,))
        if row is None:
            raise NotFoundError(
                f"Snapshot {snapshot_id!r} not found",
                hint="Run `maya snapshot list` to see pinned snapshots.",
            )
        return _row_to_model(
            RepositorySnapshot, row, json_fields=self.JSON, bool_fields=self.BOOLS
        )

    def list_by_repository(self, repository_id: str, limit: int = 50) -> list[RepositorySnapshot]:
        rows = self.db.fetchall(
            "SELECT * FROM repository_snapshots WHERE repository_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (repository_id, limit),
        )
        return [
            _row_to_model(
                RepositorySnapshot, row, json_fields=self.JSON, bool_fields=self.BOOLS
            )
            for row in rows
        ]

    def list_for_project(self, project_id: str, limit: int = 50) -> list[RepositorySnapshot]:
        rows = self.db.fetchall(
            """
            SELECT s.* FROM repository_snapshots s
            JOIN repositories r ON r.id = s.repository_id
            WHERE r.project_id = ?
            ORDER BY s.created_at DESC LIMIT ?
            """,
            (project_id, limit),
        )
        return [
            _row_to_model(
                RepositorySnapshot, row, json_fields=self.JSON, bool_fields=self.BOOLS
            )
            for row in rows
        ]

    def set_indexing(self, snapshot_id: str) -> None:
        self.db.execute(
            "UPDATE repository_snapshots SET status = ?, error = NULL WHERE id = ?",
            (SnapshotStatus.INDEXING.value, snapshot_id),
        )

    def set_indexed(
        self,
        snapshot_id: str,
        *,
        file_count: int,
        symbol_count: int,
        total_bytes: int,
        index_metadata: dict[str, Any],
    ) -> None:
        self.db.execute(
            """
            UPDATE repository_snapshots
            SET status = ?, indexed_at = ?, file_count = ?, symbol_count = ?,
                total_bytes = ?, index_metadata_json = ?, error = NULL
            WHERE id = ?
            """,
            (
                SnapshotStatus.INDEXED.value,
                utcnow().isoformat(),
                file_count,
                symbol_count,
                total_bytes,
                _dump_json(index_metadata),
                snapshot_id,
            ),
        )

    def set_failed(self, snapshot_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE repository_snapshots SET status = ?, error = ? WHERE id = ?",
            (SnapshotStatus.FAILED.value, error[:4000], snapshot_id),
        )

    def restore_indexed(self, snapshot_id: str, error: str) -> None:
        """Record a failed rebuild without discarding an already-valid index.

        The rebuild runs in a transaction, so the previous index is still intact;
        the status must return to INDEXED (a failed attempt is not a failed
        snapshot), while the error stays visible for diagnosis.
        """
        self.db.execute(
            "UPDATE repository_snapshots SET status = ?, error = ? WHERE id = ?",
            (SnapshotStatus.INDEXED.value, error[:4000], snapshot_id),
        )

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM repository_snapshots")[0])


# ---------------------------------------------------------------------- files
class FileStore:
    def __init__(self, db: Database):
        self.db = db

    def clear_snapshot_index(self, snapshot_id: str) -> None:
        """Remove every derived row for a snapshot before a rebuild.

        FTS5 rows are deleted first: deleting ``indexed_files`` would cascade
        into ``file_lines`` without maintaining the standalone FTS table.
        """
        self.db.execute("DELETE FROM search_lines WHERE snapshot_id = ?", (snapshot_id,))
        for table in (
            "import_aliases",
            "file_lines",
            "symbol_edges",
            "symbols",
            "index_issues",
            "indexed_files",
        ):
            self.db.execute(f"DELETE FROM {table} WHERE snapshot_id = ?", (snapshot_id,))

    def insert_files(self, files: Iterable[IndexedFile]) -> None:
        rows = [file.model_dump(mode="json") for file in files]
        for row in rows:
            for field in ("is_test", "is_doc", "is_config", "is_binary"):
                row[field] = 1 if row[field] else 0
        _insert_many(self.db, "indexed_files", rows)

    def insert_lines(self, snapshot_id: str, rows: Iterable[tuple[str, int, str]]) -> None:
        """Store raw lines for exact/regex search and their token expansion."""
        raw: list[tuple[str, str, int, str]] = []
        tokens: list[tuple[str, str, int, str]] = []
        for file_id, line_no, text in rows:
            raw.append((snapshot_id, file_id, line_no, text))
            tokens.append((snapshot_id, file_id, line_no, expand_line(text)))
        if raw:
            self.db.executemany(
                "INSERT INTO file_lines (snapshot_id, file_id, line_no, text) "
                "VALUES (?, ?, ?, ?)",
                raw,
            )
            self.db.executemany(
                "INSERT INTO search_lines (snapshot_id, file_id, line_no, tokens) "
                "VALUES (?, ?, ?, ?)",
                tokens,
            )

    def get(self, file_id: str) -> IndexedFile:
        row = self.db.fetchone("SELECT * FROM indexed_files WHERE id = ?", (file_id,))
        if row is None:
            raise NotFoundError(f"Indexed file {file_id} not found")
        return _row_to_model(
            IndexedFile,
            row,
            bool_fields=("is_test", "is_doc", "is_config", "is_binary"),
        )

    def get_by_path(self, snapshot_id: str, path: str) -> IndexedFile | None:
        row = self.db.fetchone(
            "SELECT * FROM indexed_files WHERE snapshot_id = ? AND path = ?", (snapshot_id, path)
        )
        return (
            _row_to_model(
                IndexedFile,
                row,
                bool_fields=("is_test", "is_doc", "is_config", "is_binary"),
            )
            if row
            else None
        )

    def list_by_snapshot(
        self,
        snapshot_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
        include_skipped: bool = False,
        status: str | None = None,
    ) -> list[IndexedFile]:
        clauses = ["snapshot_id = ?"]
        params: list[Any] = [snapshot_id]
        if not include_skipped:
            clauses.append("status <> 'SKIPPED'")
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.extend([limit, offset])
        rows = self.db.fetchall(
            f"SELECT * FROM indexed_files WHERE {' AND '.join(clauses)} "
            "ORDER BY path LIMIT ? OFFSET ?",
            params,
        )
        return [
            _row_to_model(
                IndexedFile,
                row,
                bool_fields=("is_test", "is_doc", "is_config", "is_binary"),
            )
            for row in rows
        ]

    def iter_files(self, snapshot_id: str) -> list[IndexedFile]:
        rows = self.db.fetchall(
            "SELECT * FROM indexed_files WHERE snapshot_id = ? ORDER BY path", (snapshot_id,)
        )
        return [
            _row_to_model(
                IndexedFile,
                row,
                bool_fields=("is_test", "is_doc", "is_config", "is_binary"),
            )
            for row in rows
        ]

    def insert_import_aliases(
        self, snapshot_id: str, rows: Iterable[tuple[str, str, str, str, int, int, int]]
    ) -> None:
        """Rows are ``(file_id, alias, module, imported_name, level, is_from, line)``."""
        payload = [
            {
                "id": "ima_" + short_hash(f"{snapshot_id}:{file_id}:{line}:{alias}:{imported_name}", 32),
                "snapshot_id": snapshot_id,
                "file_id": file_id,
                "alias": alias,
                "module": module,
                "imported_name": imported_name,
                "level": level,
                "is_from": is_from,
                "line": line,
            }
            for file_id, alias, module, imported_name, level, is_from, line in rows
        ]
        _insert_many(self.db, "import_aliases", payload)

    def status_counts(self, snapshot_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM indexed_files WHERE snapshot_id = ? GROUP BY status",
            (snapshot_id,),
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    def coverage_counts(self, snapshot_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT parse_coverage, COUNT(*) AS n FROM indexed_files "
            "WHERE snapshot_id = ? AND status <> 'SKIPPED' GROUP BY parse_coverage",
            (snapshot_id,),
        )
        return {str(row["parse_coverage"]): int(row["n"]) for row in rows}

    def count_by_coverage(self, snapshot_id: str) -> int:
        """Files whose structural parse coverage is not FULL."""
        row = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM indexed_files WHERE snapshot_id = ? "
            "AND status <> 'SKIPPED' AND parse_coverage <> 'FULL'",
            (snapshot_id,),
        )
        return int(row["n"])

    def language_stats(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT language, COUNT(*) AS files, SUM(line_count) AS lines,
                   SUM(size_bytes) AS bytes
            FROM indexed_files
            WHERE snapshot_id = ? AND status <> 'SKIPPED'
            GROUP BY language ORDER BY files DESC, language
            """,
            (snapshot_id,),
        )
        return [dict(row) for row in rows]

    def directory_stats(self, snapshot_id: str, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT CASE WHEN directory = '' THEN '.' ELSE directory END AS dir,
                   COUNT(*) AS files, SUM(line_count) AS lines
            FROM indexed_files
            WHERE snapshot_id = ? AND status <> 'SKIPPED'
            GROUP BY dir ORDER BY lines DESC LIMIT ?
            """,
            (snapshot_id, limit),
        )
        return [dict(row) for row in rows]

    def totals(self, snapshot_id: str) -> tuple[int, int]:
        row = self.db.fetchone(
            "SELECT COALESCE(SUM(size_bytes), 0) AS bytes, COUNT(*) AS files "
            "FROM indexed_files WHERE snapshot_id = ? AND status <> 'SKIPPED'",
            (snapshot_id,),
        )
        return int(row["bytes"]), int(row["files"])

    def read_text(self, file_id: str) -> str:
        """Reconstruct a file's text from its indexed lines."""
        rows = self.db.fetchall(
            "SELECT text FROM file_lines WHERE file_id = ? ORDER BY line_no", (file_id,)
        )
        return "\n".join(str(row["text"]) for row in rows)

    def read_span(self, file_id: str, start_line: int, end_line: int) -> list[tuple[int, str]]:
        rows = self.db.fetchall(
            "SELECT line_no, text FROM file_lines WHERE file_id = ? AND line_no BETWEEN ? AND ? "
            "ORDER BY line_no",
            (file_id, start_line, end_line),
        )
        return [(int(row["line_no"]), str(row["text"])) for row in rows]

    def get_index_cache(self, cache_key: str) -> str | None:
        """Return the artifact id holding prepared rows for this cache key."""
        row = self.db.fetchone(
            "SELECT artifact_id FROM index_file_cache WHERE cache_key = ?", (cache_key,)
        )
        return None if row is None else str(row["artifact_id"])

    def set_index_cache(self, cache_key: str, artifact_id: str, snapshot_id: str) -> None:
        self.db.execute(
            "INSERT INTO index_file_cache (cache_key, artifact_id, snapshot_id, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
            "artifact_id = excluded.artifact_id, snapshot_id = excluded.snapshot_id, "
            "created_at = excluded.created_at",
            (cache_key, artifact_id, snapshot_id, to_iso(utcnow())),
        )

    def main_guard_paths(self, snapshot_id: str) -> list[str]:
        rows = self.db.fetchall(
            """
            SELECT DISTINCT f.path FROM file_lines l
            JOIN indexed_files f ON f.id = l.file_id
            WHERE l.snapshot_id = ? AND l.text LIKE '%__main__%' AND l.text LIKE '%__name__%'
            ORDER BY f.path
            """,
            (snapshot_id,),
        )
        return [str(row["path"]) for row in rows]


# -------------------------------------------------------------------- symbols
class SymbolStore:
    JSON = {"decorators": ("decorators_json", [])}

    def __init__(self, db: Database):
        self.db = db

    def insert_many(self, symbols: Iterable[Symbol]) -> None:
        rows = [_model_to_row(symbol, json_fields=self.JSON) for symbol in symbols]
        _insert_many(self.db, "symbols", rows)

    def insert_edges(self, edges: Iterable[SymbolEdge]) -> None:
        rows = [edge.model_dump(mode="json") for edge in edges]
        _insert_many(self.db, "symbol_edges", rows)

    def get(self, symbol_id: str) -> Symbol:
        row = self.db.fetchone("SELECT * FROM symbols WHERE id = ?", (symbol_id,))
        if row is None:
            raise NotFoundError(f"Symbol {symbol_id} not found")
        return _row_to_model(Symbol, row, json_fields=self.JSON)

    def find_by_qualified(
        self, snapshot_id: str, qualified_name: str, kind: SymbolKind | None = None
    ) -> list[Symbol]:
        clauses = ["snapshot_id = ?", "qualified_name = ?"]
        params: list[Any] = [snapshot_id, qualified_name]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        rows = self.db.fetchall(
            f"SELECT * FROM symbols WHERE {' AND '.join(clauses)} ORDER BY path, start_line",
            params,
        )
        return [_row_to_model(Symbol, row, json_fields=self.JSON) for row in rows]

    def search(
        self,
        snapshot_id: str,
        query: str,
        *,
        kind: SymbolKind | None = None,
        limit: int = 25,
    ) -> list[Symbol]:
        pattern = "%" + like_pattern(query) + "%"
        clauses = ["snapshot_id = ?"]
        params: list[Any] = [snapshot_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        params.extend([pattern, pattern])
        # Placeholders bind in textual order: WHERE clause, then ORDER BY CASE, then LIMIT.
        params.extend([query, query, query.lower() + "%", pattern, limit])
        rows = self.db.fetchall(
            f"""
            SELECT id, snapshot_id, file_id, path, kind, name, qualified_name, signature,
                   start_line, end_line, docstring, decorators_json, confidence, parent_symbol_id
            FROM symbols
            WHERE {' AND '.join(clauses)}
              AND (name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')
            ORDER BY
              CASE
                WHEN qualified_name = ? THEN 0
                WHEN name = ? THEN 1
                WHEN lower(qualified_name) LIKE ? THEN 2
                WHEN name LIKE ? ESCAPE '\\' THEN 3
                ELSE 4
              END,
              length(name), qualified_name, path, start_line
            LIMIT ?
            """,
            params,
        )
        return [_row_to_model(Symbol, row, json_fields=self.JSON) for row in rows]

    def list_by_file(self, file_id: str) -> list[Symbol]:
        rows = self.db.fetchall(
            "SELECT * FROM symbols WHERE file_id = ? ORDER BY start_line", (file_id,)
        )
        return [_row_to_model(Symbol, row, json_fields=self.JSON) for row in rows]

    def count(self, snapshot_id: str) -> int:
        return int(
            self.db.fetchone("SELECT COUNT(*) FROM symbols WHERE snapshot_id = ?", (snapshot_id,))[
                0
            ]
        )

    def count_by_kind(self, snapshot_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM symbols WHERE snapshot_id = ? GROUP BY kind",
            (snapshot_id,),
        )
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def edges_from(self, symbol_id: str, edge_type: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT e.*, t.qualified_name AS target_qualified_name, t.path AS target_path,
                   t.start_line AS target_start_line, t.kind AS target_kind
            FROM symbol_edges e
            LEFT JOIN symbols t ON t.id = e.target_symbol_id
            WHERE e.source_symbol_id = ?
        """
        params: list[Any] = [symbol_id]
        if edge_type:
            sql += " AND e.edge_type = ?"
            params.append(edge_type)
        return [dict(row) for row in self.db.fetchall(sql + " ORDER BY e.line", params)]

    def edges_to(self, symbol_id: str, edge_type: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT e.*, s.qualified_name AS source_qualified_name, s.path AS source_path,
                   s.start_line AS source_start_line, s.kind AS source_kind
            FROM symbol_edges e
            LEFT JOIN symbols s ON s.id = e.source_symbol_id
            WHERE e.target_symbol_id = ?
        """
        params: list[Any] = [symbol_id]
        if edge_type:
            sql += " AND e.edge_type = ?"
            params.append(edge_type)
        return [dict(row) for row in self.db.fetchall(sql + " ORDER BY e.line", params)]

    def list_by_snapshot(self, snapshot_id: str) -> list[Symbol]:
        """Return every symbol indexed for a snapshot, in file/line order."""
        rows = self.db.fetchall(
            "SELECT * FROM symbols WHERE snapshot_id = ? ORDER BY path, start_line",
            (snapshot_id,),
        )
        return [_row_to_model(Symbol, row, json_fields=self.JSON) for row in rows]

    def list_edges(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Return every resolved symbol edge recorded for a snapshot.

        Unlike ``pending_edges`` (which is used by the resolver and omits the
        resolved target columns), this returns ``target_symbol_id`` and
        ``target_file_id`` so callers can traverse the dependency graph.
        """
        rows = self.db.fetchall(
            """
            SELECT id, source_symbol_id, source_file_id, edge_type, target_name, line,
                   provenance, confidence, target_symbol_id, target_file_id
            FROM symbol_edges WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        )
        return [dict(row) for row in rows]

    def top_level_symbols(self, snapshot_id: str, limit: int = 200) -> list[Symbol]:
        rows = self.db.fetchall(
            "SELECT * FROM symbols WHERE snapshot_id = ? AND parent_symbol_id IS NULL "
            "ORDER BY path, start_line LIMIT ?",
            (snapshot_id, limit),
        )
        return [_row_to_model(Symbol, row, json_fields=self.JSON) for row in rows]

    def edge_confidence_counts(self, snapshot_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT confidence, COUNT(*) AS n FROM symbol_edges WHERE snapshot_id = ? "
            "GROUP BY confidence",
            (snapshot_id,),
        )
        return {str(row["confidence"]): int(row["n"]) for row in rows}

    # -------------------------------------------------- resolution support
    def resolution_symbols(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT id, name, qualified_name, kind, path, file_id, parent_symbol_id
            FROM symbols WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        )
        return [dict(row) for row in rows]

    def pending_edges(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT id, source_symbol_id, source_file_id, edge_type, target_name, line,
                   provenance, confidence
            FROM symbol_edges WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        )
        return [dict(row) for row in rows]

    def update_edge_targets(
        self, rows: Iterable[tuple[str | None, str | None, str, str]]
    ) -> None:
        """Rows are ``(target_symbol_id, target_file_id, confidence, edge_id)``."""
        self.db.executemany(
            "UPDATE symbol_edges SET target_symbol_id = ?, target_file_id = ?, confidence = ? "
            "WHERE id = ?",
            list(rows),
        )

    def import_alias_rows(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM import_aliases WHERE snapshot_id = ?", (snapshot_id,)
        )
        return [dict(row) for row in rows]


# --------------------------------------------------------------------- issues
class IssueStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_many(self, issues: Iterable[IndexIssue]) -> None:
        rows = [issue.model_dump(mode="json") for issue in issues]
        _insert_many(self.db, "index_issues", rows)

    def list(
        self,
        snapshot_id: str,
        *,
        kind: IssueKind | None = None,
        severity: IssueSeverity | None = None,
        limit: int = 200,
    ) -> list[IndexIssue]:
        clauses = ["snapshot_id = ?"]
        params: list[Any] = [snapshot_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity.value)
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM index_issues WHERE {' AND '.join(clauses)} "
            "ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, "
            "path, kind LIMIT ?",
            params,
        )
        return [IndexIssue.model_validate(dict(row)) for row in rows]

    def counts_by_kind(self, snapshot_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM index_issues WHERE snapshot_id = ? GROUP BY kind",
            (snapshot_id,),
        )
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def count(self, snapshot_id: str) -> int:
        return int(
            self.db.fetchone(
                "SELECT COUNT(*) FROM index_issues WHERE snapshot_id = ?", (snapshot_id,)
            )[0]
        )


# --------------------------------------------------------------------- events
class EventStore:
    def __init__(self, db: Database):
        self.db = db

    def append(self, event: WorkflowEvent) -> WorkflowEvent:
        row = _model_to_row(event, json_fields={"payload": ("payload_json", {})})
        _insert(self.db, "workflow_events", row)
        return event

    def list(
        self,
        *,
        project_id: str | None = None,
        snapshot_id: str | None = None,
        entity_id: str | None = None,
        event_type: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        after_seq: int = 0,
    ) -> list[WorkflowEvent]:
        clauses = ["seq > ?"]
        params: list[Any] = [after_seq]
        for column, value in (
            ("project_id", project_id),
            ("snapshot_id", snapshot_id),
            ("entity_id", entity_id),
            ("event_type", event_type),
            ("task_id", task_id),
            ("run_id", run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.append(limit)
        rows = self.db.fetchall(
            "SELECT event_id, occurred_at, event_type, project_id, repository_id, snapshot_id, "
            "task_id, run_id, entity_type, entity_id, actor_kind, actor_name, correlation_id, "
            "schema_version, payload_json FROM workflow_events "
            f"WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
            params,
        )
        return [
            _row_to_model(WorkflowEvent, row, json_fields={"payload": ("payload_json", {})})
            for row in rows
        ]

    def count(self, project_id: str | None = None) -> int:
        if project_id is None:
            return int(self.db.fetchone("SELECT COUNT(*) FROM workflow_events")[0])
        return int(
            self.db.fetchone(
                "SELECT COUNT(*) FROM workflow_events WHERE project_id = ?", (project_id,)
            )[0]
        )


# --------------------------------------------------------- model invocations
class InvocationStore:
    JSON = {"details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, invocation: ModelInvocation) -> ModelInvocation:
        _insert(
            self.db, "model_invocations", _model_to_row(invocation, json_fields=self.JSON)
        )
        return invocation

    def complete(
        self,
        invocation_id: str,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        latency_ms: int,
        retry_count: int,
        provider_request_id: str | None,
        cost_usd: float | None,
        cost_basis: str | None,
        provider: str | None = None,
        model: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE model_invocations
            SET status = 'SUCCEEDED', prompt_tokens = ?, completion_tokens = ?,
                total_tokens = ?, latency_ms = ?, retry_count = ?, provider_request_id = ?,
                cost_usd = ?, cost_basis = ?, completed_at = ?, details_json = ?,
                provider = COALESCE(?, provider), model = COALESCE(?, model)
            WHERE id = ?
            """,
            (
                prompt_tokens,
                completion_tokens,
                total_tokens,
                latency_ms,
                retry_count,
                provider_request_id,
                cost_usd,
                cost_basis,
                utcnow().isoformat(),
                _dump_json(details or {}),
                provider,
                model,
                invocation_id,
            ),
        )

    def fail(
        self,
        invocation_id: str,
        *,
        error_class: str,
        latency_ms: int,
        retry_count: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE model_invocations
            SET status = 'FAILED', error_class = ?, latency_ms = ?, retry_count = ?,
                completed_at = ?, details_json = ?
            WHERE id = ?
            """,
            (
                error_class,
                latency_ms,
                retry_count,
                utcnow().isoformat(),
                _dump_json(details or {}),
                invocation_id,
            ),
        )

    def mark_refused(self, invocation_id: str, *, reason: str) -> None:
        self.db.execute(
            "UPDATE model_invocations SET status = 'REFUSED', error_class = ?, "
            "completed_at = ?, details_json = ? WHERE id = ?",
            ("policy_refused", utcnow().isoformat(), _dump_json({"reason": reason}), invocation_id),
        )

    def get(self, invocation_id: str) -> ModelInvocation:
        row = self.db.fetchone("SELECT * FROM model_invocations WHERE id = ?", (invocation_id,))
        if row is None:
            raise NotFoundError(f"Model invocation {invocation_id} not found")
        return _row_to_model(ModelInvocation, row, json_fields=self.JSON)

    def list_for_snapshot(self, snapshot_id: str, limit: int = 20) -> list[ModelInvocation]:
        rows = self.db.fetchall(
            "SELECT * FROM model_invocations WHERE snapshot_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (snapshot_id, limit),
        )
        return [_row_to_model(ModelInvocation, row, json_fields=self.JSON) for row in rows]


# ------------------------------------------------------------------------- qa
class QaStore:
    JSON = {
        "citations": ("citations_json", []),
        "uncertainties": ("uncertainties_json", []),
        "unsupported_claims": ("unsupported_claims_json", []),
        "details": ("details_json", {}),
    }

    def __init__(self, db: Database):
        self.db = db

    def create(self, record: QaRecord) -> QaRecord:
        _insert(self.db, "qa_records", _model_to_row(record, json_fields=self.JSON))
        return record

    def get(self, record_id: str) -> QaRecord:
        row = self.db.fetchone("SELECT * FROM qa_records WHERE id = ?", (record_id,))
        if row is None:
            raise NotFoundError(f"Q&A record {record_id} not found")
        return _row_to_model(QaRecord, row, json_fields=self.JSON)

    def list(self, *, project_id: str | None = None, snapshot_id: str | None = None, limit: int = 20) -> list[QaRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM qa_records {where} ORDER BY created_at DESC LIMIT ?", params
        )
        return [_row_to_model(QaRecord, row, json_fields=self.JSON) for row in rows]

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM qa_records")[0])


# -------------------------------------------------------- research documents
class ResearchDocumentStore:
    JSON = {
        "authors": ("authors_json", []),
        "extraction_warnings": ("extraction_warnings_json", []),
        "missing_pages": ("missing_pages_json", []),
        "metadata": ("metadata_json", {}),
    }

    def __init__(self, db: Database):
        self.db = db

    def create(self, document: ResearchDocument) -> ResearchDocument:
        _insert(self.db, "research_documents", _model_to_row(document, json_fields=self.JSON))
        return document

    def get(self, document_id: str) -> ResearchDocument:
        row = self.db.fetchone(
            "SELECT * FROM research_documents WHERE id = ?", (document_id,)
        )
        if row is None:
            raise NotFoundError(
                f"Research document {document_id!r} not found",
                hint="Run `maya research list` to see registered documents.",
            )
        return _row_to_model(ResearchDocument, row, json_fields=self.JSON)

    def find(self, document_id: str) -> ResearchDocument | None:
        try:
            return self.get(document_id)
        except NotFoundError:
            return None

    def list_by_project(self, project_id: str, limit: int = 50) -> list[ResearchDocument]:
        rows = self.db.fetchall(
            "SELECT * FROM research_documents WHERE project_id = ? "
            "ORDER BY created_at DESC, version DESC LIMIT ?",
            (project_id, limit),
        )
        return [_row_to_model(ResearchDocument, row, json_fields=self.JSON) for row in rows]

    def list_versions(self, project_id: str, document_key: str) -> list[ResearchDocument]:
        rows = self.db.fetchall(
            "SELECT * FROM research_documents WHERE project_id = ? AND document_key = ? "
            "ORDER BY version DESC",
            (project_id, document_key),
        )
        return [_row_to_model(ResearchDocument, row, json_fields=self.JSON) for row in rows]

    def find_by_content(
        self, project_id: str, document_key: str, content_sha256: str
    ) -> ResearchDocument | None:
        row = self.db.fetchone(
            "SELECT * FROM research_documents WHERE project_id = ? AND document_key = ? "
            "AND content_sha256 = ? ORDER BY version DESC LIMIT 1",
            (project_id, document_key, content_sha256),
        )
        return _row_to_model(ResearchDocument, row, json_fields=self.JSON) if row else None

    def latest_for_key(self, project_id: str, document_key: str) -> ResearchDocument | None:
        row = self.db.fetchone(
            "SELECT * FROM research_documents WHERE project_id = ? AND document_key = ? "
            "ORDER BY version DESC LIMIT 1",
            (project_id, document_key),
        )
        return _row_to_model(ResearchDocument, row, json_fields=self.JSON) if row else None

    def next_version(self, project_id: str, document_key: str) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(version), 0) AS v FROM research_documents "
            "WHERE project_id = ? AND document_key = ?",
            (project_id, document_key),
        )
        return int(row["v"]) + 1

    def update_extraction(self, document_id: str, document: ResearchDocument) -> None:
        row = _model_to_row(document, json_fields=self.JSON)
        _update_row(self.db, "research_documents", row)

    def count(self, project_id: str | None = None) -> int:
        if project_id is None:
            return int(self.db.fetchone("SELECT COUNT(*) FROM research_documents")[0])
        return int(
            self.db.fetchone(
                "SELECT COUNT(*) FROM research_documents WHERE project_id = ?", (project_id,)
            )[0]
        )


# ---------------------------------------------------------- source passages
class SourcePassageStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_many(self, passages: Iterable[SourcePassage]) -> None:
        rows = [passage.model_dump(mode="json") for passage in passages]
        _insert_many(self.db, "source_passages", rows)

    def get(self, passage_id: str) -> SourcePassage:
        row = self.db.fetchone("SELECT * FROM source_passages WHERE id = ?", (passage_id,))
        if row is None:
            raise NotFoundError(f"Source passage {passage_id!r} not found")
        return SourcePassage.model_validate(dict(row))

    def find(self, passage_id: str) -> SourcePassage | None:
        row = self.db.fetchone("SELECT * FROM source_passages WHERE id = ?", (passage_id,))
        return SourcePassage.model_validate(dict(row)) if row else None

    def list_by_document(
        self,
        document_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        section_prefix: str | None = None,
    ) -> list[SourcePassage]:
        clauses = ["document_id = ?"]
        params: list[Any] = [document_id]
        if section_prefix:
            clauses.append("section_path LIKE ? ESCAPE '\\'")
            params.append(like_pattern(section_prefix) + "%")
        sql = (
            f"SELECT * FROM source_passages WHERE {' AND '.join(clauses)} ORDER BY ordinal"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = self.db.fetchall(sql, params)
        return [SourcePassage.model_validate(dict(row)) for row in rows]

    def find_many(self, passage_ids: Sequence[str]) -> dict[str, SourcePassage]:
        if not passage_ids:
            return {}
        placeholders = ", ".join("?" for _ in passage_ids)
        rows = self.db.fetchall(
            f"SELECT * FROM source_passages WHERE id IN ({placeholders})", list(passage_ids)
        )
        return {str(row["id"]): SourcePassage.model_validate(dict(row)) for row in rows}

    def counts_by_kind(self, document_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM source_passages WHERE document_id = ? GROUP BY kind",
            (document_id,),
        )
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def count(self, document_id: str) -> int:
        return int(
            self.db.fetchone(
                "SELECT COUNT(*) FROM source_passages WHERE document_id = ?", (document_id,)
            )[0]
        )

    def delete_for_document(self, document_id: str) -> None:
        self.db.execute("DELETE FROM source_passages WHERE document_id = ?", (document_id,))


# -------------------------------------------------------------- research claims
class ResearchClaimStore:
    JSON = {
        "source_locations": ("source_locations_json", []),
        "basis_passage_ids": ("basis_passage_ids_json", []),
        "expected_code_signals": ("expected_code_signals_json", []),
        "forbidden_code_signals": ("forbidden_code_signals_json", []),
        "target_paths": ("target_paths_json", []),
        "target_symbols": ("target_symbols_json", []),
        "missing_details": ("missing_details_json", []),
        "validation_methods": ("validation_methods_json", []),
        "target_resources": ("target_resources_json", []),
        "validation_ids": ("validation_ids_json", []),
        "uncertainty": ("uncertainty_json", []),
    }

    def __init__(self, db: Database):
        self.db = db

    def create(self, claim: ResearchClaim) -> ResearchClaim:
        _insert(self.db, "research_claims", _model_to_row(claim, json_fields=self.JSON))
        return claim

    def update(self, claim: ResearchClaim) -> ResearchClaim:
        _update_row(self.db, "research_claims", _model_to_row(claim, json_fields=self.JSON))
        return claim

    def get(self, claim_id: str) -> ResearchClaim:
        row = self.db.fetchone("SELECT * FROM research_claims WHERE id = ?", (claim_id,))
        if row is None:
            raise NotFoundError(
                f"Claim {claim_id!r} not found",
                hint="Run `maya research claim list` to see stored claims.",
            )
        return _row_to_model(ResearchClaim, row, json_fields=self.JSON)

    def list(
        self,
        *,
        project_id: str | None = None,
        document_id: str | None = None,
        document_ids: Sequence[str] | None = None,
        include_project_level: bool = False,
        claim_type: ClaimType | None = None,
        kind: ClaimKind | None = None,
        review_status: ClaimReviewStatus | None = None,
        limit: int = 500,
    ) -> list[ResearchClaim]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if document_ids is not None:
            ids = list(dict.fromkeys(document_ids))
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                clause = f"document_id IN ({placeholders})"
                if include_project_level:
                    clause = f"({clause} OR document_id IS NULL)"
                clauses.append(clause)
                params.extend(ids)
            elif include_project_level:
                clauses.append("document_id IS NULL")
            else:
                return []
        if claim_type is not None:
            clauses.append("claim_type = ?")
            params.append(claim_type.value)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if review_status is not None:
            clauses.append("review_status = ?")
            params.append(review_status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM research_claims {where} ORDER BY ordinal, created_at, id LIMIT ?",
            params,
        )
        return [_row_to_model(ResearchClaim, row, json_fields=self.JSON) for row in rows]

    def next_ordinal(self, *, project_id: str, document_id: str | None) -> int:
        if document_id is None:
            row = self.db.fetchone(
                "SELECT COALESCE(MAX(ordinal), -1) AS o FROM research_claims "
                "WHERE project_id = ? AND document_id IS NULL",
                (project_id,),
            )
        else:
            row = self.db.fetchone(
                "SELECT COALESCE(MAX(ordinal), -1) AS o FROM research_claims WHERE document_id = ?",
                (document_id,),
            )
        return int(row["o"]) + 1

    def count(
        self,
        *,
        project_id: str | None = None,
        document_id: str | None = None,
        claim_type: ClaimType | None = None,
        kind: ClaimKind | None = None,
        review_status: ClaimReviewStatus | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if claim_type is not None:
            clauses.append("claim_type = ?")
            params.append(claim_type.value)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if review_status is not None:
            clauses.append("review_status = ?")
            params.append(review_status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM research_claims {where}", params
        )
        return int(row["n"])

    def counts_by_type(self, document_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT claim_type, COUNT(*) AS n FROM research_claims WHERE document_id = ? "
            "GROUP BY claim_type",
            (document_id,),
        )
        return {str(row["claim_type"]): int(row["n"]) for row in rows}

    def counts_by_review(self, project_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT review_status, COUNT(*) AS n FROM research_claims WHERE project_id = ? "
            "GROUP BY review_status",
            (project_id,),
        )
        return {str(row["review_status"]): int(row["n"]) for row in rows}

    def delete_machine_claims(
        self, document_id: str, *, keep: Sequence[str] = ()
    ) -> list[str]:
        """Delete unreviewed machine claims, preserving explicitly kept ids."""
        clauses = [
            "document_id = ?",
            "created_by IN ('HEURISTIC', 'MODEL')",
            "review_status = 'NEEDS_REVIEW'",
        ]
        params: list[Any] = [document_id]
        if keep:
            placeholders = ", ".join("?" for _ in keep)
            clauses.append(f"id NOT IN ({placeholders})")
            params.extend(keep)
        rows = self.db.fetchall(
            f"SELECT id FROM research_claims WHERE {' AND '.join(clauses)}", params
        )
        ids = [str(row["id"]) for row in rows]
        for claim_id in ids:
            self.db.execute("DELETE FROM research_claims WHERE id = ?", (claim_id,))
        return ids

    # ------------------------------------------------------------ dependencies
    def add_dependency(self, dependency: ClaimDependency) -> ClaimDependency:
        _insert(self.db, "claim_dependencies", dependency.model_dump(mode="json"))
        return dependency

    def clear_dependencies(self, claim_id: str) -> None:
        self.db.execute("DELETE FROM claim_dependencies WHERE claim_id = ?", (claim_id,))

    def list_dependencies(
        self, *, project_id: str | None = None, claim_ids: Sequence[str] | None = None
    ) -> list[ClaimDependency]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append(
                "claim_id IN (SELECT id FROM research_claims WHERE project_id = ?)"
            )
            params.append(project_id)
        if claim_ids is not None:
            if not claim_ids:
                return []
            placeholders = ", ".join("?" for _ in claim_ids)
            clauses.append(f"claim_id IN ({placeholders})")
            params.extend(claim_ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(f"SELECT * FROM claim_dependencies {where}", params)
        return [ClaimDependency.model_validate(dict(row)) for row in rows]

    # --------------------------------------------------------------- revisions
    def append_revision(self, revision: ClaimRevision) -> ClaimRevision:
        _insert(
            self.db,
            "claim_revisions",
            _model_to_row(
                revision,
                json_fields={"before": ("before_json", {}), "after": ("after_json", {})},
            ),
        )
        return revision

    def list_revisions(self, claim_id: str) -> list[ClaimRevision]:
        rows = self.db.fetchall(
            "SELECT * FROM claim_revisions WHERE claim_id = ? ORDER BY revision", (claim_id,)
        )
        return [
            _row_to_model(
                ClaimRevision,
                row,
                json_fields={"before": ("before_json", {}), "after": ("after_json", {})},
            )
            for row in rows
        ]

    def next_revision(self, claim_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM claim_revisions WHERE claim_id = ?",
            (claim_id,),
        )
        return int(row["r"]) + 1

    # ------------------------------------------------------------ risk factors
    def insert_risk_factors(self, factors: Iterable[ClaimRiskFactor]) -> None:
        rows = [factor.model_dump(mode="json") for factor in factors]
        _insert_many(self.db, "claim_risk_factors", rows)

    def clear_risk_factors(self, claim_id: str) -> None:
        self.db.execute("DELETE FROM claim_risk_factors WHERE claim_id = ?", (claim_id,))

    def list_risk_factors(
        self, claim_ids: Sequence[str] | None = None
    ) -> list[ClaimRiskFactor]:
        if claim_ids is not None and not claim_ids:
            return []
        params: list[Any] = []
        where = ""
        if claim_ids is not None:
            placeholders = ", ".join("?" for _ in claim_ids)
            where = f"WHERE claim_id IN ({placeholders})"
            params.extend(claim_ids)
        rows = self.db.fetchall(
            f"SELECT * FROM claim_risk_factors {where} ORDER BY claim_id, category", params
        )
        return [ClaimRiskFactor.model_validate(dict(row)) for row in rows]


# ------------------------------------------------------- discovery provenance
class SourceMetadataStore:
    """Append-only index over immutable, content-addressed source metadata.

    No update or delete method exists on purpose: a retrieval record describes
    what was observed at a point in time and must not be rewritten.
    """

    JSON = {"authors": ("authors_json", [])}

    def __init__(self, db: Database):
        self.db = db

    def create(self, metadata: SourceMetadata) -> SourceMetadata:
        _insert(self.db, "source_metadata", _model_to_row(metadata, json_fields=self.JSON))
        return metadata

    def get(self, metadata_id: str) -> SourceMetadata:
        row = self.db.fetchone("SELECT * FROM source_metadata WHERE id = ?", (metadata_id,))
        if row is None:
            raise NotFoundError(f"Source metadata {metadata_id!r} not found")
        return _row_to_model(SourceMetadata, row, json_fields=self.JSON)

    def find_by_hash(
        self, *, project_id: str, document_key: str, metadata_sha256: str
    ) -> SourceMetadata | None:
        row = self.db.fetchone(
            "SELECT * FROM source_metadata WHERE project_id = ? AND document_key = ? "
            "AND metadata_sha256 = ? ORDER BY created_at LIMIT 1",
            (project_id, document_key, metadata_sha256),
        )
        return _row_to_model(SourceMetadata, row, json_fields=self.JSON) if row else None

    def list_by_key(self, project_id: str, document_key: str) -> list[SourceMetadata]:
        rows = self.db.fetchall(
            "SELECT * FROM source_metadata WHERE project_id = ? AND document_key = ? "
            "ORDER BY created_at DESC, id DESC",
            (project_id, document_key),
        )
        return [_row_to_model(SourceMetadata, row, json_fields=self.JSON) for row in rows]

    def count(self, *, project_id: str | None = None) -> int:
        if project_id is None:
            row = self.db.fetchone("SELECT COUNT(*) AS n FROM source_metadata")
        else:
            row = self.db.fetchone(
                "SELECT COUNT(*) AS n FROM source_metadata WHERE project_id = ?",
                (project_id,),
            )
        return int(row["n"])

    def list_by_project(self, project_id: str, limit: int = 500) -> list[SourceMetadata]:
        rows = self.db.fetchall(
            "SELECT * FROM source_metadata WHERE project_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (project_id, limit),
        )
        return [_row_to_model(SourceMetadata, row, json_fields=self.JSON) for row in rows]

    def _non_active(
        self, clause: str, params: Sequence[Any]
    ) -> list[SourceMetadata]:
        rows = self.db.fetchall(
            "SELECT * FROM source_metadata WHERE status != ? "
            f"AND {clause} ORDER BY created_at DESC, id DESC",
            [PaperStatus.ACTIVE.value, *params],
        )
        return [_row_to_model(SourceMetadata, row, json_fields=self.JSON) for row in rows]

    def list_non_active_by_key(
        self, project_id: str, document_key: str
    ) -> list[SourceMetadata]:
        """Source metadata for a key whose status needs a version alert."""
        return self._non_active(
            "project_id = ? AND document_key = ?", [project_id, document_key]
        )

    def list_non_active(self, project_id: str) -> list[SourceMetadata]:
        """All alerting source metadata in a project (key-independent view)."""
        return self._non_active("project_id = ?", [project_id])


# ------------------------------------------------------- research citations
class ClaimCitationStore:
    """Append-only store for version-bound, granular claim citations."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, citation: ClaimCitation) -> ClaimCitation:
        _insert(self.db, "claim_citations", citation.model_dump(mode="json"))
        return citation

    def get(self, citation_id: str) -> ClaimCitation:
        row = self.db.fetchone(
            "SELECT * FROM claim_citations WHERE id = ?", (citation_id,)
        )
        if row is None:
            raise NotFoundError(f"Claim citation {citation_id!r} not found")
        return ClaimCitation.model_validate(dict(row))

    def _list(self, clause: str, params: Sequence[Any], limit: int) -> list[ClaimCitation]:
        rows = self.db.fetchall(
            f"SELECT * FROM claim_citations WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, limit],
        )
        return [ClaimCitation.model_validate(dict(row)) for row in rows]

    def list_by_claim(self, claim_id: str, limit: int = 500) -> list[ClaimCitation]:
        return self._list("claim_id = ?", [claim_id], limit)

    def list_by_document(self, document_id: str, limit: int = 500) -> list[ClaimCitation]:
        return self._list("document_id = ?", [document_id], limit)

    def list_by_project(self, project_id: str, limit: int = 500) -> list[ClaimCitation]:
        return self._list("project_id = ?", [project_id], limit)

    def list_by_source(
        self, source_metadata_id: str, limit: int = 500
    ) -> list[ClaimCitation]:
        return self._list("source_metadata_id = ?", [source_metadata_id], limit)

    def list_by_documents(
        self, document_ids: Sequence[str], limit: int = 500
    ) -> list[ClaimCitation]:
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        return self._list(f"document_id IN ({placeholders})", ids, limit)


# ------------------------------------------------- research disagreements
class EvidenceDisagreementStore:
    """Append-only store for typed evidence disagreements and their resolutions.

    A resolution is a *new* record with its own id; the original OPEN row is
    never updated or deleted. There is deliberately no ``update``/``delete``
    method on this store.
    """

    def __init__(self, db: Database):
        self.db = db

    def create(self, disagreement: EvidenceDisagreement) -> EvidenceDisagreement:
        _insert(self.db, "evidence_disagreements", disagreement.model_dump(mode="json"))
        return disagreement

    def append_resolution(
        self, disagreement: EvidenceDisagreement
    ) -> EvidenceDisagreement:
        """Append a resolved/dismissed record without rewriting history."""
        if disagreement.status is DisagreementStatus.OPEN:
            raise ValueError("a resolution record cannot be OPEN")
        return self.create(disagreement)

    def get(self, disagreement_id: str) -> EvidenceDisagreement:
        row = self.db.fetchone(
            "SELECT * FROM evidence_disagreements WHERE id = ?", (disagreement_id,)
        )
        if row is None:
            raise NotFoundError(f"Evidence disagreement {disagreement_id!r} not found")
        return EvidenceDisagreement.model_validate(dict(row))

    def _list(
        self, clause: str, params: Sequence[Any], limit: int
    ) -> list[EvidenceDisagreement]:
        rows = self.db.fetchall(
            f"SELECT * FROM evidence_disagreements WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, limit],
        )
        return [EvidenceDisagreement.model_validate(dict(row)) for row in rows]

    def list_by_claim(
        self, claim_id: str, limit: int = 500
    ) -> list[EvidenceDisagreement]:
        return self._list("claim_id = ?", [claim_id], limit)

    def list_by_document(
        self, document_id: str, limit: int = 500
    ) -> list[EvidenceDisagreement]:
        return self._list("document_id = ?", [document_id], limit)

    def list_by_project(
        self, project_id: str, limit: int = 500
    ) -> list[EvidenceDisagreement]:
        return self._list("project_id = ?", [project_id], limit)


# ------------------------------------------------------------ claim mappings
class ClaimMappingStore:
    JSON = {
        "gaps": ("gaps_json", []),
        "counter_evidence": ("counter_evidence_json", []),
        "details": ("details_json", {}),
    }

    def __init__(self, db: Database):
        self.db = db

    def create(self, mapping: ClaimMapping) -> ClaimMapping:
        _insert(self.db, "claim_mappings", _model_to_row(mapping, json_fields=self.JSON))
        return mapping

    def update(self, mapping: ClaimMapping) -> ClaimMapping:
        _update_row(self.db, "claim_mappings", _model_to_row(mapping, json_fields=self.JSON))
        return mapping

    def get(self, mapping_id: str) -> ClaimMapping:
        row = self.db.fetchone("SELECT * FROM claim_mappings WHERE id = ?", (mapping_id,))
        if row is None:
            raise NotFoundError(f"Claim mapping {mapping_id!r} not found")
        return _row_to_model(ClaimMapping, row, json_fields=self.JSON)

    def find_for_claim(self, claim_id: str, snapshot_id: str) -> ClaimMapping | None:
        row = self.db.fetchone(
            "SELECT * FROM claim_mappings WHERE claim_id = ? AND snapshot_id = ?",
            (claim_id, snapshot_id),
        )
        return _row_to_model(ClaimMapping, row, json_fields=self.JSON) if row else None

    def list_by_snapshot(self, snapshot_id: str) -> list[ClaimMapping]:
        rows = self.db.fetchall(
            "SELECT * FROM claim_mappings WHERE snapshot_id = ? ORDER BY claim_id",
            (snapshot_id,),
        )
        return [_row_to_model(ClaimMapping, row, json_fields=self.JSON) for row in rows]

    def counts_by_status(self, snapshot_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM claim_mappings WHERE snapshot_id = ? "
            "GROUP BY status",
            (snapshot_id,),
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    # ---------------------------------------------------------------- evidence
    def replace_evidence(self, mapping_id: str, evidence: Iterable[MappingEvidence]) -> None:
        self.db.execute("DELETE FROM mapping_evidence WHERE mapping_id = ?", (mapping_id,))
        rows = [
            {
                **item.model_dump(mode="json"),
                "matched_signals_json": json.dumps(
                    item.matched_signals, ensure_ascii=False, sort_keys=True
                ),
            }
            for item in evidence
        ]
        for row in rows:
            row.pop("matched_signals", None)
        _insert_many(self.db, "mapping_evidence", rows)

    def list_evidence(self, mapping_id: str) -> list[MappingEvidence]:
        rows = self.db.fetchall(
            "SELECT * FROM mapping_evidence WHERE mapping_id = ? ORDER BY path, start_line",
            (mapping_id,),
        )
        return [
            _row_to_model(
                MappingEvidence,
                row,
                json_fields={"matched_signals": ("matched_signals_json", [])},
            )
            for row in rows
        ]

    def list_evidence_for_snapshot(self, snapshot_id: str) -> list[MappingEvidence]:
        rows = self.db.fetchall(
            "SELECT * FROM mapping_evidence WHERE snapshot_id = ? ORDER BY path, start_line",
            (snapshot_id,),
        )
        return [
            _row_to_model(
                MappingEvidence,
                row,
                json_fields={"matched_signals": ("matched_signals_json", [])},
            )
            for row in rows
        ]


# --------------------------------------------------------- research memos
class ResearchMemoStore:
    JSON = {"readiness": ("readiness_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, memo: ResearchMemo) -> ResearchMemo:
        _insert(self.db, "research_memos", _model_to_row(memo, json_fields=self.JSON))
        return memo

    def get(self, memo_id: str) -> ResearchMemo:
        row = self.db.fetchone("SELECT * FROM research_memos WHERE id = ?", (memo_id,))
        if row is None:
            raise NotFoundError(f"Research memo {memo_id!r} not found")
        return _row_to_model(ResearchMemo, row, json_fields=self.JSON)

    def list(
        self,
        *,
        project_id: str | None = None,
        document_id: str | None = None,
        snapshot_id: str | None = None,
        limit: int = 20,
    ) -> list[ResearchMemo]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM research_memos {where} ORDER BY created_at DESC LIMIT ?", params
        )
        return [_row_to_model(ResearchMemo, row, json_fields=self.JSON) for row in rows]

    def latest(
        self, *, project_id: str, document_id: str | None, snapshot_id: str | None
    ) -> ResearchMemo | None:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if document_id is None:
            clauses.append("document_id IS NULL")
        else:
            clauses.append("document_id = ?")
            params.append(document_id)
        if snapshot_id is None:
            clauses.append("snapshot_id IS NULL")
        else:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        row = self.db.fetchone(
            f"SELECT * FROM research_memos WHERE {' AND '.join(clauses)} "
            "ORDER BY version DESC LIMIT 1",
            params,
        )
        return _row_to_model(ResearchMemo, row, json_fields=self.JSON) if row else None

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM research_memos")[0])


# ------------------------------------------------------- extraction run log
class ResearchExtractionStore:
    JSON = {"warnings": ("warnings_json", []), "parameters": ("parameters_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, extraction: ResearchExtraction) -> ResearchExtraction:
        _insert(self.db, "research_extractions", _model_to_row(extraction, json_fields=self.JSON))
        return extraction

    def find_by_key(self, document_id: str, extraction_key: str) -> ResearchExtraction | None:
        row = self.db.fetchone(
            "SELECT * FROM research_extractions WHERE document_id = ? AND extraction_key = ?",
            (document_id, extraction_key),
        )
        return _row_to_model(ResearchExtraction, row, json_fields=self.JSON) if row else None

    def list_by_document(self, document_id: str, limit: int = 20) -> list[ResearchExtraction]:
        rows = self.db.fetchall(
            "SELECT * FROM research_extractions WHERE document_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (document_id, limit),
        )
        return [_row_to_model(ResearchExtraction, row, json_fields=self.JSON) for row in rows]

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM research_extractions")[0])


# --------------------------------------------------------------- task workflow
class TaskStore:
    JSON = {"details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, task: Task) -> Task:
        _insert(
            self.db,
            "tasks",
            _model_to_row(task, json_fields=self.JSON),
        )
        return task

    def get(self, task_id: str) -> Task:
        row = self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return _row_to_model(Task, row, json_fields=self.JSON)

    def list(
        self, *, project_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return [_row_to_model(Task, row, json_fields=self.JSON) for row in rows]

    def touch(self, task_id: str) -> None:
        self.db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (to_iso(utcnow()), task_id)
        )

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM tasks")[0])


class ContractStore:
    JSON = {
        "scope": ("scope_json", {}),
        "constraints": ("constraints_json", {}),
        "acceptance_criteria": ("acceptance_criteria_json", []),
        "unknowns": ("unknowns_json", []),
        "research_context": ("research_context_json", None),
        "verification_checks": ("verification_checks_json", []),
        "required_approvals": ("required_approvals_json", []),
    }
    BOOL = ("require_experiment",)

    def __init__(self, db: Database):
        self.db = db

    def create(self, contract: TaskContract) -> TaskContract:
        _insert(
            self.db,
            "task_contracts",
            _model_to_row(contract, json_fields=self.JSON, bool_fields=self.BOOL),
        )
        return contract

    def get(self, contract_id: str) -> TaskContract:
        row = self.db.fetchone("SELECT * FROM task_contracts WHERE id = ?", (contract_id,))
        if row is None:
            raise NotFoundError(f"Task contract not found: {contract_id}")
        return _row_to_model(
            TaskContract, row, json_fields=self.JSON, bool_fields=self.BOOL
        )

    def latest(self, task_id: str) -> TaskContract:
        row = self.db.fetchone(
            "SELECT * FROM task_contracts WHERE task_id = ? ORDER BY version DESC LIMIT 1",
            (task_id,),
        )
        if row is None:
            raise NotFoundError(f"Task has no contract: {task_id}")
        return _row_to_model(
            TaskContract, row, json_fields=self.JSON, bool_fields=self.BOOL
        )

    def list_by_task(self, task_id: str) -> list[TaskContract]:
        rows = self.db.fetchall(
            "SELECT * FROM task_contracts WHERE task_id = ? ORDER BY version",
            (task_id,),
        )
        return [
            _row_to_model(TaskContract, row, json_fields=self.JSON, bool_fields=self.BOOL)
            for row in rows
        ]

    def next_version(self, task_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(version), 0) AS n FROM task_contracts WHERE task_id = ?",
            (task_id,),
        )
        return int(row["n"]) + 1

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM task_contracts")[0])


class ExperimentStore:
    """Experiment definitions, pre-run manifests, runs, observations, comparisons.

    Definitions and manifests are immutable content records. Approval state on a
    definition is the only mutable field, and it is versioned by the definition's
    own content hash: approving a different hash is a different decision.
    """

    DEFINITION_JSON = {
        "command": ("command_json", []),
        "seeds": ("seeds_json", []),
        "metrics": ("metrics_json", []),
        "metrics_source": ("metrics_source_json", {}),
        "protocol": ("protocol_json", {}),
        "treatment_dimensions": ("treatment_json", []),
        "allowed_variation": ("variation_json", []),
        "budget": ("budget_json", {}),
        "details": ("details_json", {}),
    }
    MANIFEST_JSON = {
        "command": ("command_json", []),
        "limits": ("limits_json", {}),
        "environment": ("environment_json", {}),
        "verified_inputs": ("verified_inputs_json", {}),
        "protocol": ("protocol_json", {}),
        "details": ("details_json", {}),
    }
    MANIFEST_BOOL = ("includes_worktree_patch",)
    RUN_JSON = {
        "command": ("command_json", []),
        "details": ("details_json", {}),
    }
    RUN_BOOL = ("timed_out", "blocked", "cancelled", "metrics_ingested", "workspace_kept")
    COMPARISON_JSON = {
        "findings": ("findings_json", []),
        "metrics": ("metrics_json", []),
        "baseline_run_ids": ("baseline_run_ids_json", []),
        "candidate_run_ids": ("candidate_run_ids_json", []),
        "limitations": ("limitations_json", []),
        "details": ("details_json", {}),
    }
    COMPARISON_BOOL = ("comparable",)
    OBSERVATION_JSON = {"details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------- definitions
    def create_definition(self, definition: ExperimentDefinition) -> ExperimentDefinition:
        _insert(
            self.db,
            "experiment_definitions",
            _model_to_row(definition, json_fields=self.DEFINITION_JSON),
        )
        return definition

    def get_definition(self, experiment_id: str) -> ExperimentDefinition:
        row = self.db.fetchone(
            "SELECT * FROM experiment_definitions WHERE id = ?", (experiment_id,)
        )
        if row is None:
            raise NotFoundError(f"Experiment {experiment_id!r} does not exist")
        return _row_to_model(
            ExperimentDefinition, row, json_fields=self.DEFINITION_JSON
        )

    def list_definitions(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        role: str | None = None,
        limit: int = 50,
    ) -> list[ExperimentDefinition]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if role:
            clauses.append("role = ?")
            params.append(role)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM experiment_definitions {where} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [
            _row_to_model(ExperimentDefinition, row, json_fields=self.DEFINITION_JSON)
            for row in rows
        ]

    def update_definition_approval(
        self,
        experiment_id: str,
        *,
        status: str,
        approved_by: str | None,
        approved_at: Any,
        approval_sha256: str | None,
        revoked_reason: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE experiment_definitions SET status = ?, approved_by = ?, approved_at = ?, "
            "approval_sha256 = ?, revoked_reason = ? WHERE id = ?",
            (
                status,
                approved_by,
                to_iso(approved_at) if approved_at is not None else None,
                approval_sha256,
                revoked_reason,
                experiment_id,
            ),
        )

    # ------------------------------------------------------------ manifests
    def create_manifest(self, manifest: ExperimentManifest) -> ExperimentManifest:
        _insert(
            self.db,
            "experiment_manifests",
            _model_to_row(
                manifest,
                json_fields=self.MANIFEST_JSON,
                bool_fields=self.MANIFEST_BOOL,
            ),
        )
        return manifest

    def get_manifest(self, manifest_id: str) -> ExperimentManifest:
        row = self.db.fetchone(
            "SELECT * FROM experiment_manifests WHERE id = ?", (manifest_id,)
        )
        if row is None:
            raise NotFoundError(f"Experiment manifest {manifest_id} does not exist")
        return _row_to_model(
            ExperimentManifest,
            row,
            json_fields=self.MANIFEST_JSON,
            bool_fields=self.MANIFEST_BOOL,
        )

    def manifest_for(self, experiment_id: str, seed: int, attempt: int) -> ExperimentManifest:
        row = self.db.fetchone(
            "SELECT * FROM experiment_manifests WHERE experiment_id = ? AND seed = ? "
            "AND attempt = ?",
            (experiment_id, seed, attempt),
        )
        if row is None:
            raise NotFoundError(
                f"No manifest for experiment {experiment_id} seed {seed} attempt {attempt}"
            )
        return _row_to_model(
            ExperimentManifest,
            row,
            json_fields=self.MANIFEST_JSON,
            bool_fields=self.MANIFEST_BOOL,
        )

    # ----------------------------------------------------------------- runs
    def next_attempt(self, experiment_id: str, seed: int) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(attempt), 0) + 1 FROM experiment_runs "
            "WHERE experiment_id = ? AND seed = ?",
            (experiment_id, seed),
        )
        return int(row[0]) if row else 1

    def create_run(self, run: ExperimentRun) -> ExperimentRun:
        _insert(
            self.db,
            "experiment_runs",
            _model_to_row(run, json_fields=self.RUN_JSON, bool_fields=self.RUN_BOOL),
        )
        return run

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        data: dict[str, Any] = {}
        for field, value in fields.items():
            column = self.RUN_JSON.get(field, (field, None))[0]
            if field in self.RUN_JSON:
                data[column] = _dump_json(value)
            elif field in self.RUN_BOOL:
                data[field] = 1 if value else 0
            elif isinstance(value, datetime):
                data[field] = to_iso(value)
            else:
                data[field] = value
        assignments = ", ".join(f"{column} = :{column}" for column in data)
        data["run_id"] = run_id
        self.db.execute(
            f"UPDATE experiment_runs SET {assignments} WHERE id = :run_id", data
        )

    def get_run(self, run_id: str) -> ExperimentRun:
        row = self.db.fetchone("SELECT * FROM experiment_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError(f"Experiment run {run_id} does not exist")
        return _row_to_model(
            ExperimentRun, row, json_fields=self.RUN_JSON, bool_fields=self.RUN_BOOL
        )

    def list_runs(self, experiment_id: str) -> list[ExperimentRun]:
        rows = self.db.fetchall(
            "SELECT * FROM experiment_runs WHERE experiment_id = ? "
            "ORDER BY seed, attempt",
            (experiment_id,),
        )
        return [
            _row_to_model(
                ExperimentRun, row, json_fields=self.RUN_JSON, bool_fields=self.RUN_BOOL
            )
            for row in rows
        ]

    def latest_runs_by_seed(self, experiment_id: str) -> dict[int, ExperimentRun]:
        """Latest attempt for every seed. Earlier attempts remain inspectable."""
        rows = self.db.fetchall(
            "SELECT * FROM experiment_runs WHERE experiment_id = ? "
            "ORDER BY seed, attempt DESC",
            (experiment_id,),
        )
        out: dict[int, ExperimentRun] = {}
        for row in rows:
            run = _row_to_model(
                ExperimentRun, row, json_fields=self.RUN_JSON, bool_fields=self.RUN_BOOL
            )
            out.setdefault(run.seed, run)
        return out

    def list_runs_for_task(self, task_id: str) -> list[ExperimentRun]:
        rows = self.db.fetchall(
            "SELECT * FROM experiment_runs WHERE task_id = ? ORDER BY started_at",
            (task_id,),
        )
        return [
            _row_to_model(
                ExperimentRun, row, json_fields=self.RUN_JSON, bool_fields=self.RUN_BOOL
            )
            for row in rows
        ]

    # ----------------------------------------------------------- observations
    def add_observations(
        self, run: ExperimentRun, observations: list[MetricObservation]
    ) -> None:
        """Insert observations and mark the run ingested atomically.

        Deterministic observation ids make a second ingestion of the same run a
        conflict instead of duplicating evidence.
        """
        rows = [
            _model_to_row(item, json_fields=self.OBSERVATION_JSON)
            for item in observations
        ]
        with self.db.transaction():
            _insert_many(self.db, "metric_observations", rows)
            self.db.execute(
                "UPDATE experiment_runs SET metrics_ingested = 1, observation_count = ? "
                "WHERE id = ?",
                (len(observations), run.id),
            )

    def observations_for_run(self, run_id: str) -> list[MetricObservation]:
        rows = self.db.fetchall(
            "SELECT * FROM metric_observations WHERE run_id = ? "
            "ORDER BY metric_key, sample_index",
            (run_id,),
        )
        return [
            _row_to_model(MetricObservation, row, json_fields=self.OBSERVATION_JSON)
            for row in rows
        ]

    def observations_for_experiment(
        self, experiment_id: str, *, metric_key: str | None = None
    ) -> list[MetricObservation]:
        if metric_key:
            rows = self.db.fetchall(
                "SELECT * FROM metric_observations WHERE experiment_id = ? "
                "AND metric_key = ? ORDER BY run_id, sample_index",
                (experiment_id, metric_key),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM metric_observations WHERE experiment_id = ? "
                "ORDER BY run_id, metric_key, sample_index",
                (experiment_id,),
            )
        return [
            _row_to_model(MetricObservation, row, json_fields=self.OBSERVATION_JSON)
            for row in rows
        ]

    def observation_count(self, experiment_id: str) -> int:
        return int(
            self.db.fetchone(
                "SELECT COUNT(*) FROM metric_observations WHERE experiment_id = ?",
                (experiment_id,),
            )[0]
        )

    # ---------------------------------------------------------- comparisons
    def create_comparison(self, comparison: ExperimentComparison) -> ExperimentComparison:
        _insert(
            self.db,
            "experiment_comparisons",
            _model_to_row(
                comparison,
                json_fields=self.COMPARISON_JSON,
                bool_fields=self.COMPARISON_BOOL,
            ),
        )
        return comparison

    def get_comparison(self, comparison_id: str) -> ExperimentComparison:
        row = self.db.fetchone(
            "SELECT * FROM experiment_comparisons WHERE id = ?", (comparison_id,)
        )
        if row is None:
            raise NotFoundError(f"Experiment comparison {comparison_id} does not exist")
        return _row_to_model(
            ExperimentComparison,
            row,
            json_fields=self.COMPARISON_JSON,
            bool_fields=self.COMPARISON_BOOL,
        )

    def list_comparisons(
        self,
        *,
        project_id: str | None = None,
        candidate_experiment_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[ExperimentComparison]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if candidate_experiment_id:
            clauses.append("candidate_experiment_id = ?")
            params.append(candidate_experiment_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM experiment_comparisons {where} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [
            _row_to_model(
                ExperimentComparison,
                row,
                json_fields=self.COMPARISON_JSON,
                bool_fields=self.COMPARISON_BOOL,
            )
            for row in rows
        ]

    def latest_comparison_for_task(self, task_id: str) -> ExperimentComparison | None:
        rows = self.list_comparisons(task_id=task_id, limit=1)
        return rows[0] if rows else None

    def count_definitions(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM experiment_definitions")[0])

    def count_runs(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM experiment_runs")[0])


class MemoryStore:
    """Durable project knowledge with relational provenance links."""

    JSON = {"details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, memory: ProjectMemory) -> ProjectMemory:
        row = _model_to_row(memory, json_fields=self.JSON)
        # Sources are their own table; they are not a column on the record.
        row.pop("sources", None)
        with self.db.transaction():
            _insert(self.db, "project_memories", row)
            self._insert_sources(memory)
        return memory

    def _insert_sources(self, memory: ProjectMemory) -> None:
        rows = []
        for source in memory.sources:
            rows.append(
                {
                    "id": "msrc_"
                    + short_hash(
                        f"{memory.id}:{source.kind}:{source.ref}:{source.sha256}", 32
                    ),
                    "memory_id": memory.id,
                    "kind": source.kind,
                    "ref": source.ref,
                    "sha256": source.sha256,
                    "note": source.note,
                    "created_at": to_iso(utcnow()),
                }
            )
        _insert_many(self.db, "memory_sources", rows)

    def get(self, memory_id: str) -> ProjectMemory:
        row = self.db.fetchone(
            "SELECT * FROM project_memories WHERE id = ?", (memory_id,)
        )
        if row is None:
            raise NotFoundError(f"Memory {memory_id!r} does not exist")
        memory = _row_to_model(ProjectMemory, row, json_fields=self.JSON)
        memory.sources = self.sources_for(memory.id)
        return memory

    def sources_for(self, memory_id: str) -> list[MemorySource]:
        rows = self.db.fetchall(
            "SELECT kind, ref, sha256, note FROM memory_sources WHERE memory_id = ? "
            "ORDER BY created_at, id",
            (memory_id,),
        )
        return [MemorySource.model_validate(dict(row)) for row in rows]

    def list(
        self,
        *,
        project_id: str,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ProjectMemory]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM project_memories WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        )
        memories = [_row_to_model(ProjectMemory, row, json_fields=self.JSON) for row in rows]
        for memory in memories:
            memory.sources = self.sources_for(memory.id)
        return memories

    def set_status(self, memory_id: str, status: str, *, note: str = "") -> None:
        details = self.get(memory_id).details
        if note:
            details = {**details, "status_note": note[:2000]}
        self.db.execute(
            "UPDATE project_memories SET status = ?, updated_at = ?, last_validated_at = ?, "
            "details_json = ? WHERE id = ?",
            (
                status,
                to_iso(utcnow()),
                to_iso(utcnow()),
                _dump_json(details),
                memory_id,
            ),
        )

    def delete(self, memory_id: str) -> None:
        self.db.execute("DELETE FROM project_memories WHERE id = ?", (memory_id,))

    def mark_stale_by_commit(self, *, repository_id: str, current_commit: str) -> list[str]:
        """Mark confirmed commit-scoped memories whose revision is no longer current.

        Only ``CONFIRMED`` items are touched: an unconfirmed hypothesis does not
        become stale by being unconfirmed, and a retracted item stays retracted.
        """
        rows = self.db.fetchall(
            "SELECT id FROM project_memories WHERE repository_id = ? AND commit_sha IS NOT NULL "
            "AND commit_sha <> ? AND status = 'CONFIRMED'",
            (repository_id, current_commit),
        )
        ids = [str(row["id"]) for row in rows]
        for memory_id in ids:
            self.set_status(memory_id, "STALE", note=f"repository is now at {current_commit}")
        return ids

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM project_memories")[0])


class PlanStore:
    JSON = {
        "steps": ("steps_json", []),
        "files": ("files_json", []),
        "checks": ("checks_json", []),
        "risks": ("risks_json", []),
        "unknowns": ("unknowns_json", []),
    }

    def __init__(self, db: Database):
        self.db = db

    def create(self, plan: TaskPlan) -> TaskPlan:
        _insert(self.db, "task_plans", _model_to_row(plan, json_fields=self.JSON))
        return plan

    def get(self, plan_id: str) -> TaskPlan:
        row = self.db.fetchone("SELECT * FROM task_plans WHERE id = ?", (plan_id,))
        if row is None:
            raise NotFoundError(f"Task plan not found: {plan_id}")
        return _row_to_model(TaskPlan, row, json_fields=self.JSON)

    def latest_for_run(self, run_id: str) -> TaskPlan:
        row = self.db.fetchone(
            "SELECT * FROM task_plans WHERE run_id = ? ORDER BY version DESC LIMIT 1",
            (run_id,),
        )
        if row is None:
            raise NotFoundError(f"Run has no plan: {run_id}")
        return _row_to_model(TaskPlan, row, json_fields=self.JSON)

    def list_by_run(self, run_id: str) -> list[TaskPlan]:
        rows = self.db.fetchall(
            "SELECT * FROM task_plans WHERE run_id = ? ORDER BY version", (run_id,)
        )
        return [_row_to_model(TaskPlan, row, json_fields=self.JSON) for row in rows]

    def next_version(self, run_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(version), 0) AS n FROM task_plans WHERE run_id = ?",
            (run_id,),
        )
        return int(row["n"]) + 1


class RunStore:
    JSON = {"limitations": ("limitations_json", []), "details": ("details_json", {})}
    BOOL = ("pause_requested", "cancel_requested")

    def __init__(self, db: Database):
        self.db = db

    def create(self, run: WorkflowRun) -> WorkflowRun:
        _insert(
            self.db,
            "workflow_runs",
            _model_to_row(run, json_fields=self.JSON, bool_fields=self.BOOL),
        )
        return run

    def get(self, run_id: str) -> WorkflowRun:
        row = self.db.fetchone("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError(f"Workflow run not found: {run_id}")
        return _row_to_model(
            WorkflowRun, row, json_fields=self.JSON, bool_fields=self.BOOL
        )

    def list(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        state: WorkflowState | None = None,
        limit: int = 50,
    ) -> list[WorkflowRun]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("task_id", task_id),
            ("project_id", project_id),
            ("state", state.value if state else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"SELECT * FROM workflow_runs {where} ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        )
        return [
            _row_to_model(WorkflowRun, row, json_fields=self.JSON, bool_fields=self.BOOL)
            for row in rows
        ]

    def latest_for_task(self, task_id: str) -> WorkflowRun | None:
        row = self.db.fetchone(
            "SELECT * FROM workflow_runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        if row is None:
            return None
        return _row_to_model(
            WorkflowRun, row, json_fields=self.JSON, bool_fields=self.BOOL
        )

    def compare_and_set_state(
        self,
        run_id: str,
        *,
        expected: Sequence[WorkflowState],
        new_state: WorkflowState,
        started: bool = False,
        finished: bool = False,
        error: str | None = None,
    ) -> bool:
        if not expected:
            return False
        placeholders = ", ".join("?" for _ in expected)
        assignments = [
            "previous_state = state",
            "state = ?",
            "updated_at = ?",
        ]
        now = to_iso(utcnow())
        params: list[Any] = [new_state.value, now]
        if started:
            assignments.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if finished:
            assignments.append("finished_at = ?")
            params.append(now)
        if error is not None:
            assignments.append("error = ?")
            params.append(error[:4000])
        params.extend([run_id, *(state.value for state in expected)])
        cursor = self.db.execute(
            f"UPDATE workflow_runs SET {', '.join(assignments)} WHERE id = ? AND state IN ({placeholders})",
            params,
        )
        return cursor.rowcount == 1

    def update_progress(
        self,
        run_id: str,
        *,
        step_count: int | None = None,
        tool_call_count: int | None = None,
        plan_id: str | None = None,
        workspace_id: str | None = None,
        model_tokens: int | None = None,
        model_cost_usd: float | None = None,
        finish_summary: str | None = None,
        limitations: list[str] | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: list[Any] = [to_iso(utcnow())]
        for column, value in (
            ("step_count", step_count),
            ("tool_call_count", tool_call_count),
            ("plan_id", plan_id),
            ("workspace_id", workspace_id),
            ("model_tokens", model_tokens),
            ("model_cost_usd", model_cost_usd),
            ("finish_summary", finish_summary),
            ("error", error),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if limitations is not None:
            assignments.append("limitations_json = ?")
            params.append(_dump_json(limitations))
        if details is not None:
            assignments.append("details_json = ?")
            params.append(_dump_json(details))
        params.append(run_id)
        self.db.execute(
            f"UPDATE workflow_runs SET {', '.join(assignments)} WHERE id = ?", params
        )

    def set_flags(
        self,
        run_id: str,
        *,
        pause_requested: bool | None = None,
        cancel_requested: bool | None = None,
        add_cost_usd: float | None = None,
        add_tokens: int | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: list[Any] = [to_iso(utcnow())]
        if pause_requested is not None:
            assignments.append("pause_requested = ?")
            params.append(1 if pause_requested else 0)
        if cancel_requested is not None:
            assignments.append("cancel_requested = ?")
            params.append(1 if cancel_requested else 0)
        if add_cost_usd is not None:
            assignments.append("model_cost_usd = model_cost_usd + ?")
            params.append(add_cost_usd)
        if add_tokens is not None:
            assignments.append("model_tokens = model_tokens + ?")
            params.append(add_tokens)
        params.append(run_id)
        self.db.execute(
            f"UPDATE workflow_runs SET {', '.join(assignments)} WHERE id = ?", params
        )

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM workflow_runs")[0])


class ApprovalStore:
    JSON = {"resource_scope": ("resource_scope_json", {}), "details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, approval: ApprovalRequest) -> ApprovalRequest:
        _insert(
            self.db, "approval_requests", _model_to_row(approval, json_fields=self.JSON)
        )
        return approval

    def get(self, approval_id: str) -> ApprovalRequest:
        row = self.db.fetchone("SELECT * FROM approval_requests WHERE id = ?", (approval_id,))
        if row is None:
            raise NotFoundError(f"Approval not found: {approval_id}")
        return _row_to_model(ApprovalRequest, row, json_fields=self.JSON)

    def find_pending(self, run_id: str, action: str | None = None) -> list[ApprovalRequest]:
        clauses = ["run_id = ?", "status = 'PENDING'"]
        params: list[Any] = [run_id]
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        rows = self.db.fetchall(
            f"SELECT * FROM approval_requests WHERE {' AND '.join(clauses)} "
            "ORDER BY requested_at",
            params,
        )
        return [_row_to_model(ApprovalRequest, row, json_fields=self.JSON) for row in rows]

    def list(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        status: ApprovalStatus | None = None,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("run_id", run_id),
            ("task_id", task_id),
            ("status", status.value if status else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"SELECT * FROM approval_requests {where} ORDER BY requested_at DESC LIMIT ?",
            [*params, limit],
        )
        return [_row_to_model(ApprovalRequest, row, json_fields=self.JSON) for row in rows]

    def decide(
        self,
        approval_id: str,
        *,
        approve: bool,
        actor: str,
        reason: str | None,
    ) -> bool:
        cursor = self.db.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ?, decided_by = ?, "
            "reason = ? WHERE id = ? AND status = 'PENDING'",
            (
                ApprovalStatus.APPROVED.value if approve else ApprovalStatus.DENIED.value,
                to_iso(utcnow()),
                actor,
                reason,
                approval_id,
            ),
        )
        return cursor.rowcount == 1

    def consume(
        self, approval_id: str, *, spec_sha256: str, nonce: str, now_iso: str
    ) -> ApprovalRequest | None:
        cursor = self.db.execute(
            "UPDATE approval_requests SET status = 'CONSUMED', consumed_at = ? "
            "WHERE id = ? AND status = 'APPROVED' AND spec_sha256 = ? AND nonce = ? "
            "AND datetime(expires_at) > datetime(?)",
            (now_iso, approval_id, spec_sha256, nonce, now_iso),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(approval_id)

    def expire(self, *, now_iso: str) -> int:
        cursor = self.db.execute(
            "UPDATE approval_requests SET status = 'EXPIRED' "
            "WHERE status = 'PENDING' AND datetime(expires_at) <= datetime(?)",
            (now_iso,),
        )
        return cursor.rowcount

    def expire_one(self, approval_id: str, *, now_iso: str) -> bool:
        cursor = self.db.execute(
            "UPDATE approval_requests SET status = 'EXPIRED' "
            "WHERE id = ? AND status = 'PENDING' AND datetime(expires_at) <= datetime(?)",
            (approval_id, now_iso),
        )
        return cursor.rowcount == 1

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM approval_requests")[0])


class WorkspaceStore:
    JSON = {"details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, workspace: ExecutionWorkspace) -> ExecutionWorkspace:
        _insert(
            self.db,
            "execution_workspaces",
            _model_to_row(workspace, json_fields=self.JSON),
        )
        return workspace

    def get(self, workspace_id: str) -> ExecutionWorkspace:
        row = self.db.fetchone(
            "SELECT * FROM execution_workspaces WHERE id = ?", (workspace_id,)
        )
        if row is None:
            raise NotFoundError(f"Workspace not found: {workspace_id}")
        return _row_to_model(ExecutionWorkspace, row, json_fields=self.JSON)

    def get_by_run(self, run_id: str) -> ExecutionWorkspace | None:
        row = self.db.fetchone(
            "SELECT * FROM execution_workspaces WHERE run_id = ?", (run_id,)
        )
        return None if row is None else _row_to_model(
            ExecutionWorkspace, row, json_fields=self.JSON
        )

    def update(
        self,
        workspace_id: str,
        *,
        status: Any | None = None,
        patch_artifact_id: str | None = None,
        diff_sha256: str | None = None,
        file_count: int | None = None,
        details: dict[str, Any] | None = None,
        finalized: bool = False,
        removed: bool = False,
    ) -> None:
        assignments = []
        params: list[Any] = []
        for column, value in (
            ("status", status.value if status is not None else None),
            ("patch_artifact_id", patch_artifact_id),
            ("diff_sha256", diff_sha256),
            ("file_count", file_count),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if details is not None:
            assignments.append("details_json = ?")
            params.append(_dump_json(details))
        if finalized:
            assignments.append("finalized_at = ?")
            params.append(to_iso(utcnow()))
        if removed:
            assignments.append("removed_at = ?")
            params.append(to_iso(utcnow()))
        if not assignments:
            return
        params.append(workspace_id)
        self.db.execute(
            f"UPDATE execution_workspaces SET {', '.join(assignments)} WHERE id = ?",
            params,
        )

    def count(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) FROM execution_workspaces")[0])


class ToolCallStore:
    JSON = {"arguments": ("arguments_json", {}), "details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create(self, call: ToolCall) -> ToolCall:
        _insert(self.db, "tool_invocations", _model_to_row(call, json_fields=self.JSON))
        return call

    def get(self, call_id: str) -> ToolCall:
        row = self.db.fetchone("SELECT * FROM tool_invocations WHERE id = ?", (call_id,))
        if row is None:
            raise NotFoundError(f"Tool call not found: {call_id}")
        return _row_to_model(ToolCall, row, json_fields=self.JSON)

    def complete(
        self,
        call_id: str,
        *,
        status: Any,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        output_artifact_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        assignments = ["status = ?", "completed_at = ?"]
        params: list[Any] = [status.value, to_iso(utcnow())]
        for column, value in (
            ("exit_code", exit_code),
            ("duration_ms", duration_ms),
            ("error", error),
            ("output_artifact_id", output_artifact_id),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if details is not None:
            assignments.append("details_json = ?")
            params.append(_dump_json(details))
        params.append(call_id)
        self.db.execute(
            f"UPDATE tool_invocations SET {', '.join(assignments)} WHERE id = ?", params
        )

    def note(self, call_id: str, *, details: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE tool_invocations SET details_json = ? WHERE id = ?",
            (_dump_json(details), call_id),
        )

    def list_by_run(self, run_id: str, *, limit: int = 500) -> list[ToolCall]:
        rows = self.db.fetchall(
            "SELECT * FROM tool_invocations WHERE run_id = ? ORDER BY started_at LIMIT ?",
            (run_id, limit),
        )
        return [_row_to_model(ToolCall, row, json_fields=self.JSON) for row in rows]

    def count_by_run(self, run_id: str) -> int:
        return int(
            self.db.fetchone(
                "SELECT COUNT(*) FROM tool_invocations WHERE run_id = ?", (run_id,)
            )[0]
        )


class DiffStore:
    JSON = {
        "files": ("files_json", []),
        "scope_violations": ("scope_violations_json", []),
        "warnings": ("warnings_json", []),
    }
    BOOL = ("original_checkout_unchanged",)

    def __init__(self, db: Database):
        self.db = db

    def create(self, diff: TaskDiff) -> TaskDiff:
        """Insert once per run; concurrent callers get the recorded diff.

        ``capture_diff`` checks-then-inserts, and two processes can pass that
        check together. ``task_diffs.run_id`` is UNIQUE, so the constraint is the
        real guard; losing the race must return the existing evidence rather
        than fail the run.
        """
        try:
            _insert(
                self.db,
                "task_diffs",
                _model_to_row(diff, json_fields=self.JSON, bool_fields=self.BOOL),
            )
        except ConflictError:
            existing = self.get_by_run(diff.run_id)
            if existing is None:  # pragma: no cover - constraint without a row
                raise
            return existing
        return diff

    def get_by_run(self, run_id: str) -> TaskDiff | None:
        row = self.db.fetchone("SELECT * FROM task_diffs WHERE run_id = ?", (run_id,))
        return (
            None
            if row is None
            else _row_to_model(
                TaskDiff, row, json_fields=self.JSON, bool_fields=self.BOOL
            )
        )


class ReviewStore:
    JSON = {
        "tests_modified": ("tests_modified_json", []),
        "dependency_changes": ("dependency_changes_json", []),
        "findings": ("findings_json", []),
    }
    BOOL = ("scope_ok", "secrets_found")

    def __init__(self, db: Database):
        self.db = db

    def create(self, review: TaskReview) -> TaskReview:
        try:
            _insert(
                self.db,
                "task_reviews",
                _model_to_row(review, json_fields=self.JSON, bool_fields=self.BOOL),
            )
        except ConflictError:
            existing = self.get_by_run(review.run_id)
            if existing is None:  # pragma: no cover
                raise
            return existing
        return review

    def get_by_run(self, run_id: str) -> TaskReview | None:
        row = self.db.fetchone("SELECT * FROM task_reviews WHERE run_id = ?", (run_id,))
        return (
            None
            if row is None
            else _row_to_model(
                TaskReview, row, json_fields=self.JSON, bool_fields=self.BOOL
            )
        )


class WorkerJobStore:
    JSON = {"details": ("details_json", {})}

    #: A job that crashes the worker this many times is not retried again.
    MAX_ATTEMPTS = 5

    def __init__(self, db: Database):
        self.db = db

    def enqueue(self, job: WorkerJob) -> WorkerJob:
        # Checking for an active job and inserting must be one transaction:
        # otherwise two CLI processes can each see no job and enqueue one.
        with self.db.transaction():
            existing = self.db.fetchone(
                "SELECT * FROM worker_jobs WHERE run_id = ? AND status IN ('QUEUED','LEASED')",
                (job.run_id,),
            )
            if existing is not None:
                return _row_to_model(WorkerJob, existing, json_fields=self.JSON)
            _insert(self.db, "worker_jobs", _model_to_row(job, json_fields=self.JSON))
        return job

    def get(self, job_id: str) -> WorkerJob:
        row = self.db.fetchone("SELECT * FROM worker_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFoundError(f"Worker job not found: {job_id}")
        return _row_to_model(WorkerJob, row, json_fields=self.JSON)

    def get_by_run(self, run_id: str) -> WorkerJob | None:
        row = self.db.fetchone(
            "SELECT * FROM worker_jobs WHERE run_id = ? ORDER BY enqueued_at DESC LIMIT 1",
            (run_id,),
        )
        return None if row is None else _row_to_model(WorkerJob, row, json_fields=self.JSON)

    def claim(
        self, *, owner: str, lease_seconds: int = 120, run_id: str | None = None
    ) -> WorkerJob | None:
        now = utcnow()
        now_iso = to_iso(now)
        expires = to_iso(now + timedelta(seconds=lease_seconds))
        run_clause = " AND j.run_id = ?" if run_id is not None else ""
        with self.db.transaction():
            # A run may have at most one live worker, even if an older build or
            # a racing enqueue left duplicate job rows behind. The NOT EXISTS
            # also prevents a second worker from picking a duplicate job while
            # the first worker's lease is still valid.
            row = self.db.fetchone(
                "SELECT j.* FROM worker_jobs AS j WHERE (j.status = 'QUEUED' OR "
                "(j.status = 'LEASED' AND j.lease_expires_at < ?)) "
                "AND j.attempts < ?"
                + run_clause
                + " AND NOT EXISTS (SELECT 1 FROM worker_jobs AS o "
                "WHERE o.run_id = j.run_id AND o.id <> j.id AND o.status = 'LEASED' "
                "AND o.lease_expires_at >= ?)"
                " ORDER BY j.enqueued_at LIMIT 1",
                (
                    now_iso,
                    self.MAX_ATTEMPTS,
                    *([run_id] if run_id is not None else []),
                    now_iso,
                ),
            )
            if row is None:
                return None
            job = _row_to_model(WorkerJob, row, json_fields=self.JSON)
            cursor = self.db.execute(
                "UPDATE worker_jobs SET status = 'LEASED', lease_owner = ?, "
                "lease_expires_at = ?, attempts = attempts + 1, "
                "started_at = COALESCE(started_at, ?) WHERE id = ? AND attempts = ? AND "
                "(status = 'QUEUED' OR (status = 'LEASED' AND lease_expires_at < ?))",
                (owner, expires, now_iso, job.id, job.attempts, now_iso),
            )
            if cursor.rowcount != 1:
                return None
            claimed = self.get(job.id)
        return claimed

    def reap_exhausted(
        self, *, max_attempts: int | None = None
    ) -> list[WorkerJob]:
        """Fail jobs that crashed the worker too many times.

        Without this, a job that reliably kills its worker is reclaimed after
        every lease expiry forever, and the run it belongs to never settles.
        """
        limit = self.MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
        finished = to_iso(utcnow())
        reaped: list[WorkerJob] = []
        with self.db.transaction():
            rows = self.db.fetchall(
                "SELECT * FROM worker_jobs WHERE status IN ('QUEUED','LEASED') "
                "AND attempts >= ?",
                (limit,),
            )
            for row in rows:
                job_id = str(row["id"])
                self.db.execute(
                    "UPDATE worker_jobs SET status = 'FAILED', finished_at = ?, "
                    "error = ?, lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
                    (
                        finished,
                        f"exceeded {limit} attempts without completing",
                        job_id,
                    ),
                )
                reaped.append(self.get(job_id))
        return reaped

    def heartbeat(self, job_id: str, *, owner: str, lease_seconds: int = 120) -> bool:
        now = utcnow()
        cursor = self.db.execute(
            "UPDATE worker_jobs SET lease_expires_at = ? WHERE id = ? AND "
            "status = 'LEASED' AND lease_owner = ?",
            (
                to_iso(now + timedelta(seconds=lease_seconds)),
                job_id,
                owner,
            ),
        )
        return cursor.rowcount == 1

    def finish(
        self,
        job_id: str,
        *,
        status: WorkerJobStatus,
        error: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE worker_jobs SET status = ?, finished_at = ?, error = ?, "
            "lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            (status.value, to_iso(utcnow()), error, job_id),
        )

    def list(self, *, status: WorkerJobStatus | None = None, limit: int = 100) -> list[WorkerJob]:
        if status is None:
            rows = self.db.fetchall(
                "SELECT * FROM worker_jobs ORDER BY enqueued_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM worker_jobs WHERE status = ? ORDER BY enqueued_at LIMIT ?",
                (status.value, limit),
            )
        return [_row_to_model(WorkerJob, row, json_fields=self.JSON) for row in rows]


class VerificationStore:
    """Attempts, checks, findings, reports and evidence links (Phase 4).

    Attempts are append-only. Re-running verification adds an attempt instead of
    rewriting the evidence a previous report cited.
    """

    CHECK_JSON = {
        "criteria": ("criteria_json", []),
        "command": ("command_json", []),
        "details": ("details_json", {}),
    }
    CHECK_BOOL = ("required",)
    FINDING_JSON = {"details": ("details_json", {})}
    REPORT_JSON = {
        "criteria": ("criteria_json", []),
        "check_ids": ("check_ids_json", []),
        "blocking": ("blocking_json", []),
        "limitations": ("limitations_json", []),
        "details": ("details_json", {}),
    }
    ATTEMPT_JSON = {"details": ("details_json", {})}
    LINK_JSON = {}

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- attempts
    def next_attempt(self, run_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(attempt), 0) + 1 FROM verification_runs WHERE run_id = ?",
            (run_id,),
        )
        return int(row[0]) if row else 1

    def create_attempt(self, attempt: VerificationRun) -> VerificationRun:
        _insert(
            self.db,
            "verification_runs",
            _model_to_row(attempt, json_fields=self.ATTEMPT_JSON),
        )
        return attempt

    def get_attempt(self, verification_id: str) -> VerificationRun:
        row = self.db.fetchone(
            "SELECT * FROM verification_runs WHERE id = ?", (verification_id,)
        )
        if row is None:
            raise NotFoundError(f"Verification attempt {verification_id} does not exist")
        return _row_to_model(VerificationRun, row, json_fields=self.ATTEMPT_JSON)

    def latest_attempt(self, run_id: str) -> VerificationRun | None:
        row = self.db.fetchone(
            "SELECT * FROM verification_runs WHERE run_id = ? ORDER BY attempt DESC LIMIT 1",
            (run_id,),
        )
        return (
            None
            if row is None
            else _row_to_model(VerificationRun, row, json_fields=self.ATTEMPT_JSON)
        )

    def finish_attempt(
        self,
        verification_id: str,
        *,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE verification_runs SET status = ?, completed_at = ?, details_json = ? "
            "WHERE id = ?",
            (
                status,
                to_iso(utcnow()),
                _dump_json(details or {}),
                verification_id,
            ),
        )

    # --------------------------------------------------------------- checks
    def add_check(self, check: VerificationCheck) -> VerificationCheck:
        row = _model_to_row(
            check.model_copy(update={"findings": []}),
            json_fields=self.CHECK_JSON,
            bool_fields=self.CHECK_BOOL,
        )
        row["check_key"] = row.pop("key")
        row.pop("findings", None)
        with self.db.transaction():
            _insert(self.db, "verification_checks", row)
            for finding in check.findings:
                _insert(
                    self.db,
                    "verification_findings",
                    {
                        "id": "vfd_" + short_hash(f"{check.id}:{finding.code}:{finding.path}:{finding.line}:{finding.message}", 26),
                        "verification_id": check.verification_id,
                        "check_id": check.id,
                        "run_id": check.run_id,
                        "severity": finding.severity.value,
                        "code": finding.code,
                        "message": finding.message,
                        "path": finding.path,
                        "line": finding.line,
                        "criterion_ref": finding.criterion_ref,
                        "source": finding.source,
                        "details_json": _dump_json(finding.details),
                        "created_at": to_iso(utcnow()),
                    },
                )
        return check

    def list_checks(self, verification_id: str) -> list[VerificationCheck]:
        rows = self.db.fetchall(
            "SELECT * FROM verification_checks WHERE verification_id = ? "
            "ORDER BY started_at, check_key",
            (verification_id,),
        )
        if not rows:
            return []
        finding_rows = self.db.fetchall(
            "SELECT * FROM verification_findings WHERE verification_id = ? "
            "ORDER BY created_at, code",
            (verification_id,),
        )
        grouped: dict[str, list[VerificationFinding]] = {}
        for row in finding_rows:
            data = dict(row)
            for extra in (
                "id",
                "verification_id",
                "check_id",
                "run_id",
                "created_at",
            ):
                data.pop(extra, None)
            for field, (column, default) in self.FINDING_JSON.items():
                data[field] = _load_json(data.pop(column, None), default)
            grouped.setdefault(str(row["check_id"]), []).append(
                VerificationFinding.model_validate(data)
            )
        checks: list[VerificationCheck] = []
        for row in rows:
            data = dict(row)
            data["key"] = data.pop("check_key")
            for field, (column, default) in self.CHECK_JSON.items():
                data[field] = _load_json(data.pop(column, None), default)
            for field in self.CHECK_BOOL:
                data[field] = bool(data[field])
            data["findings"] = []
            check = VerificationCheck.model_validate(data)
            check.findings = grouped.get(check.id, [])
            checks.append(check)
        return checks

    def get_check(self, verification_id: str, key: str) -> VerificationCheck | None:
        for check in self.list_checks(verification_id):
            if check.key == key:
                return check
        return None

    def list_findings(self, run_id: str, *, severity: str | None = None) -> list[VerificationFinding]:
        if severity is None:
            rows = self.db.fetchall(
                "SELECT * FROM verification_findings WHERE run_id = ? ORDER BY created_at, code",
                (run_id,),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM verification_findings WHERE run_id = ? AND severity = ? "
                "ORDER BY created_at, code",
                (run_id, severity),
            )
        findings: list[VerificationFinding] = []
        for row in rows:
            data = dict(row)
            for extra in (
                "id",
                "verification_id",
                "check_id",
                "run_id",
                "created_at",
            ):
                data.pop(extra, None)
            for field, (column, default) in self.FINDING_JSON.items():
                data[field] = _load_json(data.pop(column, None), default)
            findings.append(VerificationFinding.model_validate(data))
        return findings

    # --------------------------------------------------------------- reports
    def create_report(
        self, report: VerificationReport, links: Sequence[EvidenceLink]
    ) -> VerificationReport:
        with self.db.transaction():
            _insert(
                self.db,
                "verification_reports",
                _model_to_row(report, json_fields=self.REPORT_JSON),
            )
            _insert_many(
                self.db,
                "evidence_links",
                [
                    _model_to_row(link, json_fields=self.LINK_JSON)
                    for link in links
                ],
            )
        return report

    def get_report(self, report_id: str) -> VerificationReport:
        row = self.db.fetchone(
            "SELECT * FROM verification_reports WHERE id = ?", (report_id,)
        )
        if row is None:
            raise NotFoundError(f"Verification report {report_id} does not exist")
        return _row_to_model(
            VerificationReport, row, json_fields=self.REPORT_JSON
        )

    def report_for_attempt(self, verification_id: str) -> VerificationReport | None:
        row = self.db.fetchone(
            "SELECT * FROM verification_reports WHERE verification_id = ?",
            (verification_id,),
        )
        return (
            None
            if row is None
            else _row_to_model(VerificationReport, row, json_fields=self.REPORT_JSON)
        )

    def latest_report(self, run_id: str) -> VerificationReport | None:
        row = self.db.fetchone(
            "SELECT * FROM verification_reports WHERE run_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        return (
            None
            if row is None
            else _row_to_model(VerificationReport, row, json_fields=self.REPORT_JSON)
        )

    def list_reports(self, run_id: str) -> list[VerificationReport]:
        rows = self.db.fetchall(
            "SELECT * FROM verification_reports WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [
            _row_to_model(VerificationReport, row, json_fields=self.REPORT_JSON)
            for row in rows
        ]

    def list_links(self, report_id: str) -> list[EvidenceLink]:
        rows = self.db.fetchall(
            "SELECT * FROM evidence_links WHERE report_id = ? ORDER BY created_at",
            (report_id,),
        )
        return [
            _row_to_model(EvidenceLink, row, json_fields=self.LINK_JSON) for row in rows
        ]


class EngineeringWorkspaceStore:
    """Persistence for the durable, multi-resource Engineering Workspace."""

    WORKSPACE_JSON = {"description": ("description_json", {})}
    RESOURCE_JSON = {"details": ("details_json", {})}
    ENVIRONMENT_JSON = {"toolchain": ("toolchain_json", {})}
    SESSION_JSON = {"command": ("command_json", []), "details": ("details_json", {})}

    def __init__(self, db: Database):
        self.db = db

    def create_workspace(self, workspace: Workspace) -> Workspace:
        _insert(self.db, "workspaces", _model_to_row(workspace, json_fields=self.WORKSPACE_JSON))
        return workspace

    def get_workspace(self, workspace_id: str) -> Workspace:
        row = self.db.fetchone("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        if row is None:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
        return _row_to_model(Workspace, row, json_fields=self.WORKSPACE_JSON)

    def list_workspaces(self, project_id: str | None = None) -> list[Workspace]:
        sql = "SELECT * FROM workspaces"
        params: tuple = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY created_at, id"
        return [
            _row_to_model(Workspace, row, json_fields=self.WORKSPACE_JSON)
            for row in self.db.fetchall(sql, params)
        ]

    def attach_resource(self, resource: ResourceAttachment) -> ResourceAttachment:
        _insert(self.db, "resource_attachments", _model_to_row(resource, json_fields=self.RESOURCE_JSON))
        return resource

    def resources(self, workspace_id: str) -> list[ResourceAttachment]:
        rows = self.db.fetchall(
            "SELECT * FROM resource_attachments WHERE workspace_id = ? ORDER BY attached_at, id",
            (workspace_id,),
        )
        return [
            _row_to_model(ResourceAttachment, row, json_fields=self.RESOURCE_JSON)
            for row in rows
        ]

    def add_environment(self, spec: EnvironmentSpec) -> EnvironmentSpec:
        _insert(self.db, "environment_specs", _model_to_row(spec, json_fields=self.ENVIRONMENT_JSON))
        return spec

    def environments(self, workspace_id: str) -> list[EnvironmentSpec]:
        rows = self.db.fetchall(
            "SELECT * FROM environment_specs WHERE workspace_id = ? ORDER BY created_at, id",
            (workspace_id,),
        )
        return [
            _row_to_model(EnvironmentSpec, row, json_fields=self.ENVIRONMENT_JSON)
            for row in rows
        ]

    def create_session(self, session: WorkspaceSession) -> WorkspaceSession:
        _insert(self.db, "workspace_sessions", _model_to_row(session, json_fields=self.SESSION_JSON))
        return session

    def close_session(self, session: WorkspaceSession) -> None:
        self.db.execute(
            "UPDATE workspace_sessions SET state = ?, details_json = ? WHERE id = ?",
            (session.state.value, _dump_json(session.details), session.id),
        )

    def sessions(self, workspace_id: str) -> list[WorkspaceSession]:
        rows = self.db.fetchall(
            "SELECT * FROM workspace_sessions WHERE workspace_id = ? ORDER BY started_at, id",
            (workspace_id,),
        )
        return [
            _row_to_model(WorkspaceSession, row, json_fields=self.SESSION_JSON)
            for row in rows
        ]

    def add_evidence(self, item: EvidenceItem) -> EvidenceItem:
        _insert(self.db, "evidence_items", _model_to_row(item))
        return item

    def evidence(self, workspace_id: str) -> list[EvidenceItem]:
        rows = self.db.fetchall(
            "SELECT * FROM evidence_items WHERE workspace_id = ? ORDER BY created_at, id",
            (workspace_id,),
        )
        return [_row_to_model(EvidenceItem, row) for row in rows]

    def graph(self, workspace_id: str) -> dict:
        workspace = self.get_workspace(workspace_id)
        return {
            "workspace": workspace.model_dump(mode="json"),
            "resources": [item.model_dump(mode="json") for item in self.resources(workspace_id)],
            "environments": [
                item.model_dump(mode="json") for item in self.environments(workspace_id)
            ],
            "sessions": [item.model_dump(mode="json") for item in self.sessions(workspace_id)],
            "evidence": [item.model_dump(mode="json") for item in self.evidence(workspace_id)],
        }


__all__ = [
    "EngineeringWorkspaceStore",
    "ProjectStore",
    "RepositoryStore",
    "SnapshotStore",
    "FileStore",
    "SymbolStore",
    "IssueStore",
    "EventStore",
    "InvocationStore",
    "QaStore",
    "ResearchDocumentStore",
    "SourcePassageStore",
    "ResearchClaimStore",
    "SourceMetadataStore",
    "ClaimCitationStore",
    "EvidenceDisagreementStore",
    "ClaimMappingStore",
    "ResearchMemoStore",
    "ResearchExtractionStore",
    "TaskStore",
    "ContractStore",
    "PlanStore",
    "RunStore",
    "ApprovalStore",
    "WorkspaceStore",
    "ToolCallStore",
    "DiffStore",
    "ReviewStore",
    "WorkerJobStore",
    "VerificationStore",
    "ExperimentStore",
    "MemoryStore",
    "like_pattern",
]

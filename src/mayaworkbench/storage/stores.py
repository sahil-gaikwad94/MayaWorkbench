"""Storage repositories.

Each store maps one domain record type to relational rows. Column names match
model field names except for ``*_json`` columns, which are declared explicitly
so JSON never silently becomes the only representation of a queryable field.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import IssueKind, IssueSeverity, SnapshotStatus, SymbolKind
from mayaworkbench.domain.models import (
    IndexIssue,
    IndexedFile,
    ModelInvocation,
    Project,
    QaRecord,
    Record,
    Repository,
    RepositorySnapshot,
    Symbol,
    SymbolEdge,
    WorkflowEvent,
)
from mayaworkbench.errors import DatabaseError, NotFoundError
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
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.append(limit)
        rows = self.db.fetchall(
            "SELECT event_id, occurred_at, event_type, project_id, repository_id, snapshot_id, "
            "entity_type, entity_id, actor_kind, actor_name, correlation_id, schema_version, "
            f"payload_json FROM workflow_events WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
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


__all__ = [
    "ProjectStore",
    "RepositoryStore",
    "SnapshotStore",
    "FileStore",
    "SymbolStore",
    "IssueStore",
    "EventStore",
    "InvocationStore",
    "QaStore",
    "like_pattern",
]

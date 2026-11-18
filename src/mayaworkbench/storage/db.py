"""SQLite persistence primitives.

Local-first storage: SQLite with WAL (same-host concurrency, not a network
filesystem) plus FTS5 for lexical search. All schema changes go through
checksummed migrations; application code never issues DDL.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.errors import ConflictError, DatabaseError
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.storage.migrations import Migration, load_migrations

log = get_logger("storage.db")


@dataclass(frozen=True)
class MigrationResult:
    version: int
    name: str
    applied: bool


class Database:
    """One local SQLite database.

    A ``Database`` owns a single connection. SQLite serializes writes anyway, so
    the connection is guarded by one re-entrant lock: callers may use the same
    ``Database`` from several threads (a future HTTP layer, a thread pool) and
    every statement still sees a consistent connection. Transactions hold the
    lock for their whole scope, which matches SQLite's own writer semantics.

    The lock is per ``Database`` and does not coordinate processes; WAL plus
    ``BEGIN IMMEDIATE`` handle cross-process serialization.
    """

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 10_000,
        allow_shared_filesystem: bool = False,
    ):
        self.path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._allow_shared_filesystem = allow_shared_filesystem
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ conn
    @property
    def conn(self) -> sqlite3.Connection:
        with self._lock:
            return self._connection_locked()

    def _connection_locked(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self._allow_shared_filesystem:
                _assert_local_filesystem(self.path)
            try:
                # isolation_level=None disables the stdlib's implicit BEGIN/COMMIT
                # management. Transaction boundaries must be explicit: otherwise a
                # write performed just before ``transaction()`` leaves an implicit
                # transaction open, our BEGIN is skipped, and the process can exit
                # without committing durable state.
                conn = sqlite3.connect(
                    str(self.path),
                    timeout=self._busy_timeout_ms / 1000,
                    isolation_level=None,
                    check_same_thread=False,
                )
            except sqlite3.Error as exc:  # pragma: no cover - fs failure
                raise DatabaseError(f"Cannot open database at {self.path}: {exc}") from exc
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
                conn.execute("PRAGMA temp_store = MEMORY")
                conn.create_function("regexp", 2, _regexp, deterministic=True)
            except sqlite3.Error as exc:
                conn.close()
                raise DatabaseError(f"Cannot configure database at {self.path}: {exc}") from exc
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- migrations
    def migrate(self) -> list[MigrationResult]:
        with self._lock:
            return self._migrate_locked()

    def _migrate_locked(self) -> list[MigrationResult]:
        migrations = load_migrations()
        if not migrations:
            raise DatabaseError("No migrations were packaged with this installation")

        conn = self.conn
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                checksum   TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            int(row["version"]): (str(row["checksum"]), str(row["name"]))
            for row in conn.execute("SELECT version, name, checksum FROM schema_migrations")
        }

        known = {migration.version: migration for migration in migrations}
        for version, (checksum, name) in applied.items():
            expected = known.get(version)
            if expected is None:
                raise DatabaseError(
                    f"Database at {self.path} has migration {version} ({name}) that this "
                    "installation does not know about",
                    hint="Upgrade MayaWorkbench; do not edit or delete migrations.",
                )
            if expected.checksum != checksum:
                raise DatabaseError(
                    f"Migration {version} ({name}) checksum mismatch",
                    hint=(
                        "The applied migration was modified. Restore the original migration "
                        "file or start from a fresh data directory."
                    ),
                )

        results: list[MigrationResult] = []
        for migration in migrations:
            if migration.version in applied:
                results.append(MigrationResult(migration.version, migration.name, False))
                continue
            self._apply(migration)
            results.append(MigrationResult(migration.version, migration.name, True))
            log.info(
                "applied migration",
                extra={"migration_version": migration.version, "migration_name": migration.name},
            )
        return results

    def _apply(self, migration: Migration) -> None:
        conn = self.conn
        # Caller (migrate) already holds the re-entrant lock.
        try:
            self._begin_immediate(conn)
            for statement in _split_statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, to_iso(utcnow())),
            )
            conn.execute(f"PRAGMA user_version = {migration.version}")
            conn.execute("COMMIT")
        except (sqlite3.Error, DatabaseError) as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - already rolled back
                pass
            raise DatabaseError(
                f"Migration {migration.version} ({migration.name}) failed: {exc}",
                hint="The database was left at its previous version.",
            ) from exc

    def schema_version(self) -> int:
        with self._lock:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def applied_migrations(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT version, name, checksum, applied_at FROM schema_migrations "
                    "ORDER BY version"
                )
            )

    # ------------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements atomically.

        Nested use joins the outer transaction (no savepoints): callers must
        treat any exception as aborting the whole unit of work, which is what
        the indexing/event flows require.
        """
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                yield conn
                return
            self._begin_immediate(conn)
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
                raise
            else:
                conn.execute("COMMIT")

    def _begin_immediate(self, conn: sqlite3.Connection) -> None:
        """Start a write transaction, retrying transient busy/locked errors.

        ``busy_timeout`` already waits inside SQLite. This loop covers the case
        where a WAL checkpoint or a long reader repeatedly grabs the lock, and
        turns a temporary contention into a bounded wait instead of a failed
        command. The total wait is bounded so a wedged process cannot hang a
        worker forever.
        """
        deadline = time.monotonic() + max(5.0, self._busy_timeout_ms / 1000)
        delay = 0.02
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if time.monotonic() >= deadline:
                    raise DatabaseError(
                        "The database stayed locked by another process; the operation "
                        "was not started.",
                        hint=(
                            "SQLite is single-writer and same-host only. Check for a stuck "
                            "worker or move $MAYA_HOME off a network filesystem."
                        ),
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 0.5)

    # ----------------------------------------------------------------- queries
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                return self.conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Database constraint violated: {exc}") from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"Database error: {exc}") from exc

    def executemany(
        self, sql: str, params: Sequence[Sequence[Any]] | Sequence[dict[str, Any]]
    ) -> sqlite3.Cursor:
        try:
            with self._lock:
                return self.conn.executemany(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Database constraint violated: {exc}") from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"Database error: {exc}") from exc

    def fetchall(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        try:
            with self._lock:
                return list(self.conn.execute(sql, params).fetchall())
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Database constraint violated: {exc}") from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"Database error: {exc}") from exc

    def fetchone(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        try:
            with self._lock:
                return self.conn.execute(sql, params).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Database constraint violated: {exc}") from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"Database error: {exc}") from exc

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {"path": str(self.path), "exists": self.path.exists()}
        try:
            with self._lock:
                conn = self.conn
                info["sqlite_version"] = conn.execute("SELECT sqlite_version()").fetchone()[0]
                info["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
                info["foreign_keys"] = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
                info["fts5"] = True
        except sqlite3.Error as exc:
            info["fts5"] = False
            info["error"] = str(exc)
        return info


# Filesystems where SQLite WAL and POSIX locking are not safe for a live
# database. A workbench database accidentally placed on one of these must fail
# loudly instead of corrupting evidence.
_SHARED_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "smbfs",
    "9p",
    "ceph",
    "afs",
    "glusterfs",
    "lustre",
    "fuse.sshfs",
}


def _mount_entries() -> list[tuple[str, str]]:
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:  # pragma: no cover - non-Linux or restricted /proc
        return []
    entries: list[tuple[str, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = fields[1].replace("\\040", " ").replace("\\011", "\t")
        entries.append((mount_point, fields[2].lower()))
    return entries


def _filesystem_type(path: Path) -> str | None:
    resolved = os.path.realpath(str(path))
    best: tuple[int, str] | None = None
    for mount_point, fs_type in _mount_entries():
        prefix = mount_point.rstrip("/") or "/"
        if resolved == prefix or resolved.startswith(prefix.rstrip("/") + "/") or prefix == "/":
            length = len(prefix)
            if best is None or length > best[0]:
                best = (length, fs_type)
    return best[1] if best else None


def _assert_local_filesystem(path: Path) -> None:
    fs_type = _filesystem_type(path)
    if fs_type is None:
        return
    if fs_type in _SHARED_FILESYSTEMS:
        raise DatabaseError(
            f"$MAYA_HOME is on a shared filesystem ({fs_type}) at {path.parent}",
            hint=(
                "SQLite WAL and POSIX locking are only safe on a local filesystem, and "
                "MayaWorkbench is single-host by design. Move the data directory to local "
                "disk. If this filesystem really is local, open Database(..., "
                "allow_shared_filesystem=True) deliberately."
            ),
        )


def _regexp(pattern: str, value: str | None) -> bool:
    if value is None:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into statements, respecting triggers.

    ``sqlite3.Cursor.execute`` accepts one statement at a time, and
    ``executescript`` would implicitly commit the surrounding transaction. A
    small splitter is safer than either extreme: it handles ``BEGIN ... END``
    trigger bodies and ignores ``--`` comments.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger_body = False

    for raw_line in sql.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(raw_line)
        lowered = stripped.lower()
        if lowered.startswith("create trigger"):
            in_trigger_body = True
        if in_trigger_body:
            if lowered.endswith("end;"):
                statements.append("\n".join(buffer).strip())
                buffer = []
                in_trigger_body = False
            continue
        if stripped.endswith(";"):
            statements.append("\n".join(buffer).strip())
            buffer = []
    remainder = "\n".join(buffer).strip()
    if remainder:
        statements.append(remainder)
    # Normalize: ensure each statement ends without a trailing semicolon.
    return [statement[:-1].rstrip() if statement.endswith(";") else statement for statement in statements]


__all__ = ["Database", "MigrationResult", "Migration"]

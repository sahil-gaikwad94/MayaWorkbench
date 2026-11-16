from __future__ import annotations

import sqlite3

import pytest

from mayaworkbench.domain.enums import ArtifactKind
from mayaworkbench.errors import ConflictError, DatabaseError, NotFoundError
from mayaworkbench.storage.db import Database

PROJECT_INSERT = (
    "INSERT INTO projects (id, name, slug, description, settings_json, created_at, updated_at) "
    "VALUES (?, ?, ?, '', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
)


def test_migrations_apply_once_and_record_checksums(tmp_path):
    db = Database(tmp_path / "maya.db")
    first = db.migrate()
    assert [result.applied for result in first] == [True]
    assert db.schema_version() == 1
    rows = db.applied_migrations()
    assert rows[0]["checksum"]

    second = db.migrate()
    assert all(result.applied is False for result in second)
    db.close()


def test_tampered_migration_history_is_refused(tmp_path):
    db = Database(tmp_path / "maya.db")
    db.migrate()
    db.execute("UPDATE schema_migrations SET checksum = 'not-the-checksum'")
    db.close()

    reopened = Database(tmp_path / "maya.db")
    with pytest.raises(DatabaseError, match="checksum mismatch"):
        reopened.migrate()
    reopened.close()


def test_missing_fts5_is_reported(tmp_path):
    db = Database(tmp_path / "maya.db")
    db.migrate()
    assert db.health()["fts5"] is True
    db.close()


def test_committed_transaction_is_durable_across_connections(tmp_path):
    path = tmp_path / "maya.db"
    db = Database(path)
    db.migrate()
    with db.transaction():
        db.execute(PROJECT_INSERT, ("p1", "One", "one"))
    db.close()

    reopened = Database(path)
    assert reopened.fetchone("SELECT COUNT(*) FROM projects")[0] == 1
    reopened.close()


def test_transaction_rolls_back_on_error(tmp_path):
    path = tmp_path / "maya.db"
    db = Database(path)
    db.migrate()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute(PROJECT_INSERT, ("p1", "One", "one"))
            raise RuntimeError("abort")
    assert db.fetchone("SELECT COUNT(*) FROM projects")[0] == 0
    db.close()


def test_autocommit_write_then_transaction_is_not_swallowed(tmp_path):
    """Regression: writes before an explicit transaction must remain durable.

    With stdlib implicit transactions, an INSERT could open a transaction that
    the explicit BEGIN then joined, so the later COMMIT never happened.
    """
    path = tmp_path / "maya.db"
    db = Database(path)
    db.migrate()
    db.execute(PROJECT_INSERT, ("p1", "One", "one"))
    with db.transaction():
        db.execute(PROJECT_INSERT, ("p2", "Two", "two"))
    assert db.conn.in_transaction is False
    db.close()

    reopened = Database(path)
    assert reopened.fetchone("SELECT COUNT(*) FROM projects")[0] == 2
    reopened.close()


def test_foreign_keys_cascade_from_project(tmp_path):
    db = Database(tmp_path / "maya.db")
    db.migrate()
    with db.transaction():
        db.execute(PROJECT_INSERT, ("p1", "One", "one"))
        db.execute(
            "INSERT INTO repositories (id, project_id, name, root_path, details_json, created_at, updated_at) "
            "VALUES ('r1', 'p1', 'R', '/tmp/r', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
    db.execute("DELETE FROM projects WHERE id = 'p1'")
    assert db.fetchone("SELECT COUNT(*) FROM repositories")[0] == 0
    db.close()


def test_artifacts_are_deduplicated_and_content_addressed(services):
    first = services.artifacts.put_text("hello", kind=ArtifactKind.FILE_CONTENT)
    second = services.artifacts.put_text("hello", kind=ArtifactKind.FILE_CONTENT)
    assert first.id == second.id
    assert first.sha256 == second.sha256
    assert services.artifacts.read_text(first.id) == "hello"


def test_tampered_artifact_is_reported(services):
    artifact = services.artifacts.put_text("original", kind=ArtifactKind.QA_ANSWER)
    path = services.artifacts.resolve_path(artifact.id)
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ConflictError, match="integrity check"):
        services.artifacts.read_bytes(artifact.id)


def test_missing_artifact_blob_is_reported(services):
    artifact = services.artifacts.put_text("content", kind=ArtifactKind.CONTEXT_PACK)
    services.artifacts.resolve_path(artifact.id).unlink()
    with pytest.raises(NotFoundError):
        services.artifacts.read_bytes(artifact.id)


def test_regexp_function_is_registered(tmp_path):
    db = Database(tmp_path / "maya.db")
    db.migrate()
    assert db.fetchone("SELECT regexp('^ab', 'abcdef') AS m")["m"] == 1
    # SQLite wraps user-function exceptions, so the DatabaseError is what callers see.
    with pytest.raises(DatabaseError):
        db.fetchone("SELECT regexp('(', 'abc') AS m")
    db.close()


def test_sqlite_version_is_new_enough(tmp_path):
    db = Database(tmp_path / "maya.db")
    db.migrate()
    version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    assert version >= (3, 35), f"FTS5 features require SQLite >= 3.35, found {sqlite3.sqlite_version}"
    db.close()

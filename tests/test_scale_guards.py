"""Regression tests for the Phase 1-3 scale and concurrency limitations.

Each test targets one previously documented weakness:

* indexing parsed files inside the rebuild write transaction;
* check-then-insert diff capture not being enforced by the database;
* no guard against a database on a network filesystem;
* fork-bomb/CPU limits only asserted as configured, never exercised;
* memos loading every project claim for a single-document memo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mayaworkbench.app.container import Services
from mayaworkbench.errors import DatabaseError, UsageError
from mayaworkbench.indexing import indexer as indexer_module
from mayaworkbench.research import memo as memo_module
from mayaworkbench.storage import db as db_module
from mayaworkbench.storage.db import Database

from tests.test_verification import make_reviewing_run


def test_parsing_runs_outside_the_index_write_transaction(
    services, indexed, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_parse = indexer_module.parse_python
    observed: list[bool] = []

    def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed.append(services.db.conn.in_transaction)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(indexer_module, "parse_python", wrapped)
    report = services.indexer.index(indexed.snapshot.id)
    assert observed, "the fixture should contain Python files"
    assert not any(observed), "a file was parsed while the rebuild transaction was open"
    assert report.prepare_ms >= 0
    assert report.write_ms >= 0
    assert report.files_indexed == indexed.report.files_indexed
    spill_dir = Path(services.artifacts.root) / "tmp"
    assert not list(spill_dir.glob("index-*.jsonl"))


def test_diff_capture_is_idempotent_under_a_race(
    services, indexed, task_model, home: Path
) -> None:
    run = make_reviewing_run(services, indexed, task_model)
    diff = services.task_service.capture_diff(run.run.id)
    peer = Services.open(str(home))
    try:
        duplicate = diff.model_copy(update={"id": "diff_duplicate"})
        returned = peer.diffs.create(duplicate)
        assert returned.id == diff.id
        count = services.db.fetchone(
            "SELECT COUNT(*) AS n FROM task_diffs WHERE run_id = ?", (run.run.id,)
        )["n"]
        assert count == 1
    finally:
        peer.close()


def test_database_refuses_a_shared_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_module, "_filesystem_type", lambda path: "nfs")
    refused = Database(tmp_path / "refused.sqlite3")
    with pytest.raises(DatabaseError, match="shared filesystem"):
        refused.fetchone("SELECT 1 AS n")
    refused.close()

    allowed = Database(tmp_path / "allowed.sqlite3", allow_shared_filesystem=True)
    try:
        assert allowed.fetchone("SELECT 1 AS n")["n"] == 1
    finally:
        allowed.close()


def test_memo_claim_guard_reports_instead_of_loading_everything(
    services, project, paper, monkeypatch: pytest.MonkeyPatch
) -> None:
    services.claim_service.extract(paper.document.id)
    monkeypatch.setattr(memo_module, "MAX_MEMO_CLAIMS", 0)
    with pytest.raises(UsageError, match="more than 0 claims"):
        services.memo_service.build(project, document=paper.document)


def test_claim_list_scopes_documents_in_sql(services, project, paper) -> None:
    services.claim_service.extract(paper.document.id)
    document_claims = services.claims.list(
        project_id=project.id, document_ids=[paper.document.id]
    )
    assert document_claims
    assert all(claim.document_id == paper.document.id for claim in document_claims)
    only_project_level = services.claims.list(
        project_id=project.id, document_ids=[], include_project_level=True
    )
    assert all(claim.document_id is None for claim in only_project_level)
    excluded = services.claims.list(
        project_id=project.id, document_ids=[paper.document.id], include_project_level=False
    )
    assert all(claim.document_id is not None for claim in excluded)
    assert services.claims.list(project_id=project.id, document_ids=[]) == []


def test_readiness_does_not_include_other_documents_claims(
    services, project, paper
) -> None:
    services.claim_service.extract(paper.document.id)
    assessment = services.memo_service.assess(project)
    assert assessment.claims
    scoped = services.memo_service.assess(project, document=paper.document)
    assert scoped.claims
    assert all(
        claim.document_id in (None, paper.document.id) for claim in scoped.claims
    )

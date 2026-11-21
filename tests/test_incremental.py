"""WP5 end-to-end: content-hash incremental indexing across process restarts."""

from __future__ import annotations

from pathlib import Path

from mayaworkbench.domain.enums import SymbolKind

from .conftest import git, write


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "incremental-repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    write(root / "alpha.py", "def alpha():\n    return 1\n")
    write(root / "beta.py", "def beta():\n    return 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    return root


def _snapshot(services, project, repo):
    attached = services.repository_service.attach(project, repo)
    return services.repository_service.create_snapshot(attached.repository).snapshot


def test_second_index_reuses_every_unchanged_file(services, project, tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(services, project, repo)
    first = services.indexer.index(snapshot.id)
    assert first.files_indexed == 2
    assert first.files_reused == 0

    second = services.indexer.index(snapshot.id)
    assert second.files_indexed == 2
    assert second.files_reused == 2
    assert second.symbols == first.symbols


def test_changed_file_is_reindexed_and_stale_symbols_disappear(services, project, tmp_path):
    repo = _repo(tmp_path)
    first_snapshot = _snapshot(services, project, repo)
    first = services.indexer.index(first_snapshot.id)
    assert services.symbols.find_by_qualified(first_snapshot.id, "beta")

    write(repo / "beta.py", "def renamed():\n    return 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "rename")
    second_snapshot = _snapshot(services, project, repo)
    report = services.indexer.index(second_snapshot.id)

    assert report.files_indexed == 2
    # Cache rows are snapshot-scoped, so a new commit rebuilds from source;
    # correctness after rename/delete is the guarantee here.
    assert report.files_reused == 0
    assert not any(
        item.kind is SymbolKind.FUNCTION
        for item in services.symbols.find_by_qualified(second_snapshot.id, "beta")
    )
    renamed = services.symbols.find_by_qualified(second_snapshot.id, "renamed")
    assert renamed and renamed[0].kind is SymbolKind.FUNCTION
    assert any(
        item.kind is SymbolKind.FUNCTION
        for item in services.symbols.find_by_qualified(second_snapshot.id, "alpha")
    )


def test_cache_key_includes_indexer_version(services, project, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    snapshot = _snapshot(services, project, repo)
    services.indexer.index(snapshot.id)

    monkeypatch.setattr("mayaworkbench.indexing.indexer.INDEXER_VERSION", "2.0.0")
    report = services.indexer.index(snapshot.id)
    assert report.files_reused == 0, "a parser change must invalidate all cached records"

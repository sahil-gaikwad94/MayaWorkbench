from __future__ import annotations

import json
from pathlib import Path

import pytest

from mayaworkbench.domain.enums import SnapshotKind, SnapshotStatus
from mayaworkbench.errors import IndexingError, RepositoryError

from .conftest import ATTENTION_PY, git, write


def attach(services, project, repo: Path):
    return services.repository_service.attach(project, repo).repository


def test_committed_snapshot_records_identity_and_dirty_state(services, project, repo):
    write(repo / "attention.py", ATTENTION_PY + "\n# uncommitted note\n")
    repository = attach(services, project, repo)
    outcome = services.repository_service.create_snapshot(repository)
    snapshot = outcome.snapshot

    assert snapshot.kind == SnapshotKind.COMMITTED
    assert snapshot.status == SnapshotStatus.CREATED
    assert snapshot.dirty is True
    assert snapshot.includes_worktree_patch is False
    assert snapshot.patch_hash is None
    assert len(snapshot.commit_sha or "") == 40
    assert snapshot.tree_sha and len(snapshot.tree_sha) == 40
    assert any("part of this snapshot" in warning.lower() for warning in outcome.warnings)
    assert snapshot.index_metadata["commit_subject"] == "Initial fixture"


def test_committed_snapshot_indexes_only_committed_content(services, project, repo):
    write(repo / "attention.py", ATTENTION_PY + "\n# uncommitted note\n")
    repository = attach(services, project, repo)
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    services.indexer.index(snapshot.id)

    hits, _ = services.search.search_text(snapshot.id, "uncommitted note", mode="exact")
    assert hits == []
    hits, _ = services.search.search_text(snapshot.id, "CausalAttention", mode="lexical")
    assert hits, "committed content must still be indexed"


def test_worktree_snapshot_captures_patch_and_untracked_files(services, project, repo):
    write(repo / "attention.py", ATTENTION_PY + "\n# working tree change\n")
    write(repo / "experimental.py", "VALUE = 7\n")
    repository = attach(services, project, repo)
    outcome = services.repository_service.create_snapshot(repository, working_tree="include")
    snapshot = outcome.snapshot

    assert snapshot.kind == SnapshotKind.WORKTREE
    assert snapshot.includes_worktree_patch is True
    assert snapshot.dirty is True
    assert snapshot.patch_hash and len(snapshot.patch_hash) == 64
    assert snapshot.patch_artifact_id and snapshot.untracked_manifest_artifact_id
    assert "experimental.py" in snapshot.included_paths
    assert "attention.py" in snapshot.included_paths

    patch_text = services.artifacts.read_text(snapshot.patch_artifact_id)
    assert "working tree change" in patch_text
    manifest = json.loads(services.artifacts.read_text(snapshot.untracked_manifest_artifact_id))
    assert "experimental.py" in manifest["files"]
    assert len(manifest["files"]["experimental.py"]["sha256"]) == 64


def test_worktree_snapshot_indexes_uncommitted_content(services, project, repo):
    write(repo / "attention.py", ATTENTION_PY + "\n# working tree change\n")
    repository = attach(services, project, repo)
    snapshot = services.repository_service.create_snapshot(repository, working_tree="include").snapshot
    services.indexer.index(snapshot.id)

    hits, _ = services.search.search_text(snapshot.id, "working tree change", mode="exact")
    assert hits, "worktree snapshot must include uncommitted content"


def test_lexical_index_is_scoped_to_its_snapshot(services, project, repo):
    repository = attach(services, project, repo)
    committed = services.repository_service.create_snapshot(repository).snapshot
    services.indexer.index(committed.id)

    write(repo / "attention.py", ATTENTION_PY + "\n# uniquemarkerword\n")
    worktree = services.repository_service.create_snapshot(
        repository, working_tree="include"
    ).snapshot
    services.indexer.index(worktree.id)

    committed_hits, _ = services.search.search_text(
        committed.id, "uniquemarkerword", mode="lexical"
    )
    worktree_hits, _ = services.search.search_text(
        worktree.id, "uniquemarkerword", mode="lexical"
    )
    assert committed_hits == []
    assert worktree_hits


def test_snapshot_creation_never_modifies_the_user_checkout(services, project, repo):
    write(repo / "utils.py", "def extra() -> int:\n    return 1\n")
    before_head = git(repo, "rev-parse", "HEAD").strip()
    before_status = git(repo, "status", "--porcelain")
    repository = attach(services, project, repo)
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    services.indexer.index(snapshot.id)

    assert git(repo, "rev-parse", "HEAD").strip() == before_head
    assert git(repo, "status", "--porcelain") == before_status


def test_equivalent_snapshot_is_reused(services, project, repo):
    repository = attach(services, project, repo)
    first = services.repository_service.create_snapshot(repository)
    second = services.repository_service.create_snapshot(repository)
    assert second.reused is True
    assert second.snapshot.id == first.snapshot.id


def test_stale_worktree_snapshot_is_refused_at_index_time(services, project, repo):
    write(repo / "attention.py", ATTENTION_PY + "\n# first change\n")
    repository = attach(services, project, repo)
    snapshot = services.repository_service.create_snapshot(repository, working_tree="include").snapshot

    write(repo / "attention.py", ATTENTION_PY + "\n# second change\n")
    with pytest.raises(IndexingError, match="working tree changed"):
        services.indexer.index(snapshot.id)

    refreshed = services.snapshots.get(snapshot.id)
    assert refreshed.status == SnapshotStatus.FAILED
    assert refreshed.error and "working tree changed" in refreshed.error.lower()


def test_attach_rejects_non_repository(tmp_path, services, project):
    directory = tmp_path / "not-a-repo"
    directory.mkdir()
    with pytest.raises(RepositoryError):
        services.repository_service.attach(project, directory)


def test_attach_is_idempotent_for_the_same_path(services, project, repo):
    first = services.repository_service.attach(project, repo)
    second = services.repository_service.attach(project, repo)
    assert first.created is True
    assert second.created is False
    assert first.repository.id == second.repository.id


def test_snapshot_of_empty_repository_is_refused(tmp_path, services, project):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    repository = attach(services, project, empty)
    with pytest.raises(Exception, match="no commits"):
        services.repository_service.create_snapshot(repository)


def test_repository_controlled_helpers_are_not_executed(services, project, repo, tmp_path):
    """A hostile .gitattributes/.git/config must not run code during capture."""
    marker = tmp_path / "executed"
    write(repo / ".gitattributes", "*.py diff=hostile\n")
    git(repo, "config", "diff.hostile.textconv", f"touch {marker} && cat")
    git(repo, "config", "core.fsmonitor", f"touch {marker} && echo")
    write(repo / "attention.py", ATTENTION_PY + "\n# change for diff\n")
    repository = attach(services, project, repo)
    outcome = services.repository_service.create_snapshot(repository, working_tree="include")
    assert outcome.snapshot.patch_artifact_id
    assert not marker.exists(), "repository-controlled helper was executed"


def test_snapshot_records_submodules(services, project, repo):
    git(repo, "update-index", "--add", "--cacheinfo", "160000,1111111111111111111111111111111111111111,vendor/lib")
    git(repo, "commit", "-qm", "add submodule entry")
    repository = attach(services, project, repo)
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    assert snapshot.submodules.get("vendor/lib") == "1" * 40

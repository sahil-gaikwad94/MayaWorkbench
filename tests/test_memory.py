"""Durable project memory tests (Phase 5 §15).

The rules are epistemic, not cosmetic: model-authored items start UNCONFIRMED,
only a user decision confirms, secrets are refused, and commit-scoped knowledge
goes STALE instead of silently pretending to still describe the repository.
"""

from __future__ import annotations

import json

import pytest

from mayaworkbench.domain.enums import MemoryAuthor, MemoryKind, MemoryStatus
from mayaworkbench.domain.models import MemorySource
from mayaworkbench.errors import ProjectMemoryError
from tests.conftest import git, write


def add(services, project, **overrides):  # type: ignore[no-untyped-def]
    payload = dict(
        project_id=project.id,
        kind=MemoryKind.FACT,
        title="Tokenizer",
        statement="The tokenizer splits on whitespace.",
    )
    payload.update(overrides)
    return services.memory_service.add(**payload)


def test_user_memory_is_confirmed_and_exported(services, project, tmp_path):
    memory = add(
        services,
        project,
        sources=[MemorySource(kind="USER_NOTE", ref="design discussion")],
    )
    assert memory.status == MemoryStatus.CONFIRMED
    assert memory.author_kind == MemoryAuthor.USER
    assert services.memory_service.get(memory.id).id == memory.id
    assert [item.id for item in services.memory_service.list(project.id)] == [memory.id]
    assert (
        services.memory_service.list(project.id, kind="FACT", status="CONFIRMED")
        == [memory]
    )
    assert services.memory_service.list(project.id, kind="DECISION") == []

    paths = services.memory_service.export(project.id, tmp_path / "memory-export")
    payload = json.loads((tmp_path / "memory-export" / "memories.json").read_text())
    assert payload["memories"][0]["statement"] == memory.statement
    markdown = (tmp_path / "memory-export" / "memories.md").read_text()
    assert "The tokenizer splits on whitespace." in markdown


def test_model_memory_starts_unconfirmed_and_only_a_user_confirms(services, project):
    memory = add(
        services,
        project,
        author_kind=MemoryAuthor.MODEL,
        title="Inferred convention",
        statement="Tests appear to use pytest.",
    )
    assert memory.status == MemoryStatus.UNCONFIRMED
    with pytest.raises(ProjectMemoryError, match="cannot be created as confirmed"):
        add(
            services,
            project,
            author_kind=MemoryAuthor.MODEL,
            status=MemoryStatus.CONFIRMED,
            title="Inferred decision",
            statement="The model says so.",
        )
    confirmed = services.memory_service.confirm(memory.id, note="checked the test config")
    assert confirmed.status == MemoryStatus.CONFIRMED
    assert services.memory_service.confirm(memory.id).status == MemoryStatus.CONFIRMED

    retracted = services.memory_service.retract(memory.id, reason="no longer true")
    assert retracted.status == MemoryStatus.RETRACTED
    with pytest.raises(ProjectMemoryError, match="retracted"):
        services.memory_service.confirm(memory.id)
    with pytest.raises(ProjectMemoryError, match="reason"):
        services.memory_service.retract(memory.id, reason="  ")


def test_memory_refuses_secret_shaped_content(services, project):
    with pytest.raises(ProjectMemoryError, match="secret"):
        add(
            services,
            project,
            statement="The deploy key is -----BEGIN OPENSSH PRIVATE KEY-----",
        )
    with pytest.raises(ProjectMemoryError, match="secret"):
        add(services, project, statement="api_key = sk-abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(ProjectMemoryError, match="secret"):
        add(services, project, title="password = hunter2hunter2")


def test_provenance_is_validated_not_just_stored(services, project, indexed):
    memory = add(
        services,
        project,
        sources=[MemorySource(kind="SNAPSHOT", ref=indexed.snapshot.id)],
    )
    assert memory.sources[0].ref == indexed.snapshot.id

    with pytest.raises(ProjectMemoryError, match="does not exist"):
        add(
            services,
            project,
            sources=[MemorySource(kind="EXPERIMENT", ref="exp_missing")],
        )
    with pytest.raises(ProjectMemoryError, match="Unknown memory source"):
        add(services, project, sources=[MemorySource(kind="NOT_A_KIND", ref="x")])


def test_commit_scoped_memory_goes_stale_when_the_repository_moves(
    services, project, indexed
):
    old_commit = indexed.snapshot.commit_sha
    confirmed = add(
        services,
        project,
        title="Old API",
        statement="The old API accepted a str path.",
        repository_id=indexed.repository.id,
        commit_sha=old_commit,
    )
    unconfirmed = add(
        services,
        project,
        author_kind=MemoryAuthor.MODEL,
        title="Maybe old API",
        statement="Possibly the same.",
        repository_id=indexed.repository.id,
        commit_sha=old_commit,
    )
    retracted = add(
        services,
        project,
        title="Retracted",
        statement="Already wrong.",
        repository_id=indexed.repository.id,
        commit_sha=old_commit,
    )
    services.memory_service.retract(retracted.id, reason="obsolete")

    write(indexed.repo / "utils.py", '"""Utility helpers."""\n\n\ndef count_tokens(text):\n    return len(text.split())\n')
    git(indexed.repo, "add", "-A")
    git(indexed.repo, "commit", "-qm", "change API")

    marked = services.memory_service.mark_stale(
        project_id=project.id, repository_id=indexed.repository.id
    )
    assert marked == [confirmed.id]
    assert services.memory_service.get(confirmed.id).status == MemoryStatus.STALE
    assert services.memory_service.get(unconfirmed.id).status == MemoryStatus.UNCONFIRMED
    assert services.memory_service.get(retracted.id).status == MemoryStatus.RETRACTED


def test_delete_removes_one_item(services, project):
    memory = add(services, project)
    services.memory_service.delete(memory.id)
    with pytest.raises(Exception):
        services.memory_service.get(memory.id)
    assert services.memory_service.list(project.id) == []

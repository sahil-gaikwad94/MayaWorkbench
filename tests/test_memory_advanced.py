"""Advanced memory: conflicts, retention and portable bundles are advisory-only."""

from __future__ import annotations

import json

from mayaworkbench.domain.enums import MemoryKind, MemoryStatus
from mayaworkbench.memory.advanced import (
    apply_retention,
    export_bundle,
    find_conflicts,
    fingerprint,
    import_bundle,
)


def _confirmed(service, project, title, statement):
    item = service.memory_service.add(
        project_id=project.id, kind=MemoryKind.FACT, title=title, statement=statement
    )
    return service.memory_service.confirm(item.id)


def test_conflict_detection_is_advisory(services, project):
    _confirmed(services, project, "a", "Always use the causal mask before normalization")
    _confirmed(services, project, "b", "Never use the causal mask before normalization")
    conflicts = find_conflicts(services.memory_service, project.id)
    assert conflicts
    assert conflicts[0]["advisory"].startswith("Reported only")


def test_fingerprint_ignores_word_order():
    assert fingerprint("alpha beta gamma") == fingerprint("gamma alpha beta")


def test_retention_retracts_expired_confirmed_memories(services, project):
    item = services.memory_service.add(
        project_id=project.id,
        kind=MemoryKind.FACT,
        title="old",
        statement="An old confirmed convention",
        details={"expires_at": "2000-01-01T00:00:00+00:00"},
    )
    services.memory_service.confirm(item.id)
    retracted = apply_retention(services.memory_service, project.id)
    assert any(entry["id"] == item.id for entry in retracted)
    assert services.memory_service.get(item.id).status is MemoryStatus.RETRACTED


def test_bundle_round_trip_imports_unconfirmed(services, project):
    _confirmed(services, project, "a", "The default batch size is thirty two")
    bundle = export_bundle(services.memory_service, project.id)
    assert bundle["schema"] == "maya-memory-bundle/v1"
    result = import_bundle(services.memory_service, project.id, bundle)
    assert not result["created"]
    bundle["memories"][0]["statement"] = "A new statement from an external bundle"
    result = import_bundle(services.memory_service, project.id, bundle)
    assert len(result["created"]) == 1
    imported = services.memory_service.get(result["created"][0])
    assert imported.status is MemoryStatus.UNCONFIRMED
    assert imported.details["imported_from_bundle"] is True


def test_memory_advanced_cli(tmp_path, repo, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    from mayaworkbench.cli.main import main

    run(["init", "--project", "advanced-memory"])
    code, payload = run(
        [
            "memory",
            "add",
            "--project",
            "advanced-memory",
            "--kind",
            "FACT",
            "--title",
            "m",
            "--statement",
            "The cache uses an LRU policy",
        ]
    )
    assert code == 0
    out = tmp_path / "bundle.json"
    code, _ = run(["memory", "export-bundle", "--project", "advanced-memory", str(out)])
    assert code == 0
    code, payload = run(["memory", "audit", "--project", "advanced-memory"])
    assert code == 0
    assert "conflicts" in payload

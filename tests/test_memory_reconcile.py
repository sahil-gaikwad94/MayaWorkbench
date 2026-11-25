"""Memory semantic reconciliation: near-duplicate detection and advisory plans."""

from __future__ import annotations

import json

from mayaworkbench.domain.enums import MemoryKind
from mayaworkbench.memory.advanced import (
    find_near_duplicates,
    reconcile,
)


def _confirmed(service, project, title, statement):
    item = service.memory_service.add(
        project_id=project.id, kind=MemoryKind.FACT, title=title, statement=statement
    )
    return service.memory_service.confirm(item.id)


def test_find_near_duplicates_detects_paraphrase(services, project):
    _confirmed(services, project, "a", "Always use the causal mask before normalization")
    _confirmed(services, project, "b", "Always use causal mask before normalization step")
    duplicates = find_near_duplicates(services.memory_service, project.id)
    assert duplicates, "expected at least one near-duplicate pair"
    assert duplicates[0]["similarity"] >= 0.78
    assert duplicates[0]["advisory"].startswith("Reported only")


def test_find_near_duplicates_threshold_validation(services, project):
    import pytest

    with pytest.raises(Exception):
        find_near_duplicates(services.memory_service, project.id, threshold=0.0)
    with pytest.raises(Exception):
        find_near_duplicates(services.memory_service, project.id, threshold=1.5)


def test_reconcile_is_advisory_and_does_not_mutate(services, project):
    first = _confirmed(services, project, "a", "The default batch size is thirty two")
    _confirmed(services, project, "b", "The default batch size is thirty two items")
    before = services.memory_service.list(project.id)
    plan = reconcile(services.memory_service, project.id)
    after = services.memory_service.list(project.id)
    assert len(before) == len(after)
    assert plan["advisory"].startswith("Reconciliation is advisory")
    assert plan["plan"], "expected at least one suggested action"
    # statuses untouched
    assert services.memory_service.get(first.id).status.value == "CONFIRMED"


def test_reconcile_cli(tmp_path, repo, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        from mayaworkbench.cli.main import main

        code = main(base + args)
        captured = capsys.readouterr()
        text = captured.out.strip()
        try:
            return code, json.loads(text) if text else None
        except json.JSONDecodeError:
            return code, None

    run(["init", "--project", "recon"])
    run(["memory", "add", "--project", "recon", "--kind", "FACT", "--title", "m1", "--statement", "Use LRU cache eviction policy always"])
    run(["memory", "add", "--project", "recon", "--kind", "FACT", "--title", "m2", "--statement", "Use LRU cache eviction policy always now"])
    # confirm both
    # list to get ids
    code, payload = run(["memory", "list", "--project", "recon"])
    for item in (payload or {}).get("memories", []):
        run(["memory", "confirm", item["id"]])
    code, payload = run(["memory", "reconcile", "--project", "recon"])
    assert code == 0
    assert "plan" in payload
    assert "near_duplicates" in payload

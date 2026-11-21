"""Skills system end-to-end: versioning, governance, bundles, and bounded execution."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.errors import NotFoundError, UsageError
from mayaworkbench.skills.service import SkillsService


def _service(services):
    return SkillsService(services.db)


def test_seed_and_effective_version(services, project):
    svc = _service(services)
    created = svc.seed_defaults(project.id)
    assert len(created) == 6
    assert svc.effective(project.id, "plan-first") is not None
    assert len(svc.seed_defaults(project.id)) == 0


def test_versioning_enable_disable_and_promote(services, project):
    svc = _service(services)
    first = svc.upsert(project.id, "custom", "Custom", body="v1")
    second = svc.upsert(project.id, "custom", "Custom", body="v2")
    assert second.version == 2
    assert svc.promote(project.id, "custom").id == second.id
    svc.set_enabled(second.id, False)
    assert svc.promote(project.id, "custom").id == first.id
    assert svc.effective(project.id, "custom").version == 1
    assert [s.version for s in svc.list(project.id, enabled=True) if s.key == "custom"] == [1]


def test_invalid_skill_input_is_refused(services, project):
    try:
        _service(services).upsert(project.id, "  ", "Name")
    except UsageError:
        pass
    else:
        raise AssertionError("empty key must be refused")
    try:
        _service(services).get("skill_does_not_exist")
    except NotFoundError:
        pass
    else:
        raise AssertionError("unknown skill must be refused")


def test_export_import_is_idempotent_and_reports_conflicts(services, project):
    svc = _service(services)
    a = svc.upsert(project.id, "bundle-skill", "Bundle", body="text")
    bundle = svc.export_bundle(project.id)
    result = svc.import_bundle(project.id, bundle)
    assert result["skipped"] and not result["created"]
    other = svc.upsert(project.id, "bundle-skill", "Bundle", body="newer")
    stale = [{**a.model_dump(mode="json"), "version": 1, "key": "bundle-skill"}]
    conflict = svc.import_bundle(project.id, stale)
    assert "bundle-skill@v1" in conflict["conflicts"]
    assert other.version == 2


def test_executor_is_bounded_and_honest(services, project):
    from mayaworkbench.skills.executor import execute_skill

    svc = _service(services)
    procedure = svc.upsert(project.id, "plan", "Plan", body="1. Read the contract.")
    result = execute_skill(procedure, goal="fix the bug")
    assert result.status == "PROCESSED"
    assert "fix the bug" in result.output
    assert any("no side effects" in item for item in result.validation)

    dangerous = svc.upsert(project.id, "bad", "Bad", body="run rm -rf / now")
    refused = execute_skill(dangerous)
    assert refused.status == "FAILED"
    assert any("forbidden" in item for item in refused.validation)
    disabled = svc.set_enabled(procedure.id, False)
    assert execute_skill(disabled).status == "SKIPPED"


def test_skills_cli_round_trip(tmp_path, repo, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    run(["init", "--project", "skills-project"])
    code, _ = run(["skill", "seed", "--project", "skills-project"])
    assert code == 0
    code, payload = run(["skill", "list", "--project", "skills-project"])
    assert code == 0
    assert any(item["key"] == "plan-first" for item in payload["skills"])
    code, payload = run(
        [
            "skill",
            "add",
            "--project",
            "skills-project",
            "--key",
            "cli-skill",
            "--name",
            "CLI skill",
            "--body",
            "1. Read only.",
        ]
    )
    assert code == 0
    skill_id = payload["skill"]["id"]
    code, payload = run(["skill", "run", skill_id, "--context", "goal=demo"])
    assert code == 0
    assert payload["status"] == "PROCESSED"
    assert "demo" in payload["output"]

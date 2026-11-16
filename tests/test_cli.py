from __future__ import annotations

import json
from pathlib import Path

import pytest

from mayaworkbench.cli.main import main


def run(capsys, args: list[str]) -> tuple[int, dict | None]:
    code = main(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else None
    return code, payload


def test_full_workflow_via_cli(tmp_path, repo: Path, capsys):
    home = tmp_path / "cli-home"
    base = ["--home", str(home), "--json"]

    code, payload = run(capsys, base + ["init", "--project", "cli-project"])
    assert code == 0
    assert payload["project"]["slug"] == "cli-project"
    assert Path(payload["database"]).exists()

    code, payload = run(capsys, base + ["repo", "add", str(repo)])
    assert code == 0
    assert payload["created"] is True
    repository_id = payload["repository"]["id"]

    code, payload = run(capsys, base + ["snapshot", "create", "--repo", repository_id])
    assert code == 0
    snapshot = payload["snapshot"]
    assert payload["reused"] is False
    assert len(snapshot["commit_sha"]) == 40
    snapshot_id = snapshot["id"]

    code, payload = run(capsys, base + ["index", "--snapshot", snapshot_id])
    assert code == 0
    assert payload["files_indexed"] > 0
    assert payload["symbols"] > 0
    assert payload["manifest_artifact_id"]

    code, payload = run(capsys, base + ["inspect", "--snapshot", snapshot_id, "--save"])
    assert code == 0
    repository_map = payload["map"]
    assert repository_map["files"] > 0
    assert repository_map["parse_coverage"]["FULL"] > 0
    assert payload["saved_artifact_id"]

    code, payload = run(
        capsys, base + ["search", "CausalAttention", "--snapshot", snapshot_id, "--symbols"]
    )
    assert code == 0
    assert payload["symbols"][0]["qualified_name"] == "CausalAttention"

    code, payload = run(capsys, base + ["ask", "Where is CausalAttention defined?", "--snapshot", snapshot_id, "--no-model"])
    assert code == 0
    assert payload["record"]["status"] == "NO_MODEL"
    assert payload["record"]["citations"]
    assert payload["context"]["token_estimate"] > 0

    code, payload = run(capsys, base + ["qa", "list"])
    assert code == 0
    assert len(payload["records"]) == 1

    code, payload = run(capsys, base + ["events", "--limit", "50"])
    assert code == 0
    event_types = [event["event_type"] for event in payload["events"]]
    for expected in (
        "PROJECT_CREATED",
        "REPOSITORY_ATTACHED",
        "SNAPSHOT_PINNED",
        "INDEX_COMPLETED",
        "QA_EVIDENCE_ONLY",
    ):
        assert expected in event_types

    code, payload = run(capsys, base + ["db", "info"])
    assert code == 0
    assert payload["counts"]["indexed_files"] > 0

    code, payload = run(capsys, base + ["issues", "--snapshot", snapshot_id])
    assert code == 0
    assert "SKIPPED_SECRET" in payload["counts"]


def test_explicit_snapshot_disambiguates_multiple_repositories(tmp_path, repo: Path, capsys):
    import shutil

    home = tmp_path / "multi-home"
    base = ["--home", str(home), "--json"]
    second = tmp_path / "second-repo"
    shutil.copytree(repo, second)

    code, _ = run(capsys, base + ["init", "--project", "multi"])
    assert code == 0
    code, first_payload = run(capsys, base + ["repo", "add", str(repo)])
    assert code == 0
    code, second_payload = run(capsys, base + ["repo", "add", str(second)])
    assert code == 0
    first_id = first_payload["repository"]["id"]
    assert second_payload["repository"]["id"] != first_id

    code, payload = run(capsys, base + ["snapshot", "create", "--repo", first_id])
    assert code == 0
    snapshot_id = payload["snapshot"]["id"]

    # Both of these previously failed with "Several repositories are attached".
    code, payload = run(capsys, base + ["index", "--snapshot", snapshot_id])
    assert code == 0
    assert payload["files_indexed"] > 0
    code, payload = run(
        capsys, base + ["ask", "Where is CausalAttention defined?", "--snapshot", snapshot_id, "--no-model"]
    )
    assert code == 0
    assert payload["record"]["status"] == "NO_MODEL"


def test_doctor_reports_environment(tmp_path, capsys):
    home = tmp_path / "doctor-home"
    code, payload = run(capsys, ["--home", str(home), "--json", "init", "--project", "doc"])
    assert code == 0
    code, payload = run(capsys, ["--home", str(home), "--json", "doctor"])
    assert code == 0
    checks = {check["check"]: check["status"] for check in payload["checks"]}
    assert checks["python"] == "ok"
    assert checks["fts5"] == "ok"
    assert checks["git"] == "ok"
    assert checks["model"] in {"ok", "warn"}


def test_missing_project_returns_usage_error(tmp_path, capsys):
    home = tmp_path / "err-home"
    code = main(["--home", str(home), "--json", "init", "--project", "p"])
    capsys.readouterr()
    assert code == 0
    code = main(["--home", str(home), "--json", "project", "show", "missing"])
    captured = capsys.readouterr()
    assert code != 0
    assert "error" in json.loads(captured.err)


def test_config_set_round_trip(tmp_path, capsys):
    home = tmp_path / "cfg-home"
    code = main(["--home", str(home), "--json", "init", "--project", "p"])
    capsys.readouterr()
    assert code == 0

    code, payload = run(
        capsys,
        ["--home", str(home), "--json", "config", "set", "retrieval.default_token_budget=1234"],
    )
    assert code == 0
    assert payload["value"] == 1234

    code, payload = run(capsys, ["--home", str(home), "--json", "config", "show"])
    assert code == 0
    assert payload["retrieval"]["default_token_budget"] == 1234

    code = main(["--home", str(home), "--json", "config", "set", "model.api_key=leak"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Secrets are never stored" in captured.err


def test_schemas_export(tmp_path, capsys):
    target = tmp_path / "schemas"
    code, payload = run(capsys, ["--json", "schemas", "export", str(target)])
    assert code == 0
    assert (target / "RepositoryMap.schema.json").exists()
    assert (target / "schemas.json").exists()
    manifest = json.loads((target / "schemas.json").read_text())
    assert manifest["bundle_version"]
    assert "RepositorySnapshot" in manifest["models"]


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "MayaWorkbench" in capsys.readouterr().out

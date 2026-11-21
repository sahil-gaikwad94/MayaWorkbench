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


def test_experiment_and_memory_commands_are_registered():
    from mayaworkbench.cli.main import build_parser

    parser = build_parser()
    experiment = next(
        action for action in parser._actions if action.dest == "command"
    ).choices["experiment"]
    memory = next(action for action in parser._actions if action.dest == "command").choices[
        "memory"
    ]
    experiment_actions = next(
        action for action in experiment._actions if action.dest == "experiment_command"
    ).choices
    memory_actions = next(
        action for action in memory._actions if action.dest == "memory_command"
    ).choices
    assert {"create", "run", "compare", "bundle", "verify-bundle"} <= set(
        experiment_actions
    )
    assert {"add", "list", "confirm", "stale", "export"} <= set(memory_actions)


def test_metric_spec_parsing_uses_explicit_directions():
    from mayaworkbench.cli.experiment_commands import parse_metric_spec
    from mayaworkbench.errors import UsageError

    metric = parse_metric_spec("val_loss,direction=LOWER,aggregation=MEAN,primary=true,unit=nats")
    assert metric.key == "val_loss"
    assert metric.direction.value == "LOWER_IS_BETTER"
    assert metric.aggregation.value == "MEAN"
    assert metric.primary is True
    assert metric.unit == "nats"
    higher = parse_metric_spec("acc,direction=HIGHER,primary=true")
    assert higher.direction.value == "HIGHER_IS_BETTER"
    with pytest.raises(UsageError, match="direction"):
        parse_metric_spec("loss,primary=true")


def test_experiment_cli_static_flow(tmp_path, repo: Path, capsys):
    home = tmp_path / "experiment-cli-home"
    base = ["--home", str(home), "--json"]
    assert run(capsys, base + ["init", "--project", "experiments"])[0] == 0
    _code, attached = run(capsys, base + ["repo", "add", str(repo)])
    _code, pinned = run(
        capsys, base + ["snapshot", "create", "--repo", attached["repository"]["id"]]
    )
    snapshot_id = pinned["snapshot"]["id"]
    assert run(capsys, base + ["index", "--snapshot", snapshot_id])[0] == 0

    def create(name, role):
        return run(
            capsys,
            base
            + [
                "experiment",
                "create",
                "--snapshot",
                snapshot_id,
                "--name",
                name,
                "--hypothesis",
                "the candidate reduces loss",
                "--role",
                role,
                "--profile",
                "STATIC",
                "--metrics-source",
                "IMPORTED",
                "--metric",
                "loss,direction=LOWER,aggregation=LAST,primary=true",
                "--seed",
                "1",
                "--seed",
                "2",
                "--seed",
                "3",
            ],
        )

    code, baseline = create("cli baseline", "BASELINE")
    assert code == 0, baseline
    code, candidate = create("cli candidate", "CANDIDATE")
    assert code == 0, candidate
    baseline_id = baseline["definition"]["id"]
    candidate_id = candidate["definition"]["id"]
    assert run(capsys, base + ["experiment", "approve", baseline_id])[0] == 0
    assert run(capsys, base + ["experiment", "approve", candidate_id])[0] == 0

    def record(experiment_id, values):
        for seed, value in values.items():
            metrics = tmp_path / f"metrics-{experiment_id}-{seed}.json"
            metrics.write_text(json.dumps({"metrics": {"loss": value}}), encoding="utf-8")
            code, payload = run(
                capsys,
                base
                + [
                    "experiment",
                    "record",
                    experiment_id,
                    "--seed",
                    str(seed),
                    "--metrics",
                    str(metrics),
                    "--note",
                    "external lab run",
                ],
            )
            assert code == 0, payload

    record(baseline_id, {1: 1.00, 2: 1.01, 3: 0.99})
    record(candidate_id, {1: 0.90, 2: 0.92, 3: 0.89})

    code, plan = run(capsys, base + ["experiment", "plan", candidate_id])
    assert code == 0
    assert plan["blockers"] == []

    code, compared = run(
        capsys,
        base
        + [
            "experiment",
            "compare",
            candidate_id,
            "--baseline",
            baseline_id,
            "--remember",
        ],
    )
    assert code == 0, compared
    comparison_id = compared["comparison"]["id"]
    assert compared["comparison"]["verdict"] == "CANDIDATE_ADVANTAGE"

    destination = tmp_path / "cli-experiment-bundle"
    code, bundled = run(
        capsys, base + ["experiment", "bundle", comparison_id, str(destination)]
    )
    assert code == 0, bundled
    assert bundled["manifest"]["kind"] == "COMPARISON"
    code, verified = run(capsys, base + ["experiment", "verify-bundle", str(destination)])
    assert code == 0, verified
    assert verified["ok"] is True

    code, memories = run(capsys, base + ["memory", "list"])
    assert code == 0
    assert any(item["kind"] == "EXPERIMENT_RESULT" for item in memories["memories"])

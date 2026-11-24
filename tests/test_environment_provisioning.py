"""P1-05: an environment spec is replayable and provisioning is isolated.

These tests use a fake provisioning runtime, so they exercise the provisioner's
own rules: host execution is refused, network steps are blocked without approval,
a failure stops the run, and an inventory that cannot be read is reported as
unavailable instead of an empty list. Real installs are covered only by the
opt-in OCI harness, which needs a rootless host.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.environments import ServiceSpec, resolve_spec
from mayaworkbench.workspace.provisioning import EnvironmentProvisioner
from mayaworkbench.workspace.runner import CommandResult


def _project(tmp_path: Path, *, lockfile: str | None = None) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n\n"
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    if lockfile is not None:
        (root / "requirements.txt").write_text(lockfile, encoding="utf-8")
    return root


def _result(
    argv: list[str], *, exit_code: int = 0, stdout: str = "", stderr: str = ""
) -> CommandResult:
    return CommandResult(
        argv=list(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=3,
        timed_out=False,
        output_truncated=False,
    )


class FakeRuntime:
    """An isolated runtime that records argv and answers from a handler."""

    trust_profile = "ROOTLESS_CONTAINER"

    def __init__(self, handler=None):
        self.calls: list[tuple[list[str], int]] = []
        self.handler = handler or (lambda argv: _result(argv, stdout="ok"))

    def exec(self, argv, *, timeout_seconds, run_home=None):
        self.calls.append((list(argv), timeout_seconds))
        return self.handler(list(argv))

    def describe(self):
        return {"trust_profile": self.trust_profile, "content_diff": []}


# ------------------------------------------------------------------ the spec
def test_spec_is_deterministic_and_never_invents_a_lockfile(tmp_path):
    root = _project(tmp_path)
    first = resolve_spec(root)
    second = resolve_spec(root)

    assert first.spec_sha256 == second.spec_sha256
    assert first.lockfiles == {}
    assert "python" in first.lockfile_absent
    assert first.package_managers["python"] == "pip"
    assert first.provenance["detection_confidence"] in {"MEDIUM", "HIGH"}
    assert "python-install" in first.network_gated_steps
    assert "verify-runtime" not in first.commands
    assert any("No lockfile was detected" in note for note in first.notes)


def test_lockfile_bytes_pin_the_spec(tmp_path):
    root = _project(tmp_path, lockfile="pytest==9.2.0\n")
    spec = resolve_spec(root)
    assert len(spec.lockfiles["requirements.txt"]) == 64
    assert spec.lockfile_absent == ()

    (root / "requirements.txt").write_text("pytest==9.2.1\n", encoding="utf-8")
    changed = resolve_spec(root)
    assert changed.spec_sha256 != spec.spec_sha256
    assert changed.lockfiles["requirements.txt"] != spec.lockfiles["requirements.txt"]


def test_secret_references_never_carry_a_value(tmp_path):
    spec = resolve_spec(_project(tmp_path), secret_refs=["MAYA_MODEL_API_KEY"])
    assert spec.env == {"MAYA_MODEL_API_KEY": "secret:MAYA_MODEL_API_KEY"}
    assert "sk-" not in json.dumps(spec.as_dict())


def test_services_are_declared_with_their_health_check(tmp_path):
    spec = resolve_spec(
        _project(tmp_path),
        services=(ServiceSpec(name="db", port=5432, health_check=("pg_isready",)),),
    )
    declared = spec.as_dict()["services"][0]
    assert declared == {
        "name": "db",
        "port": 5432,
        "health_check": ["pg_isready"],
        "required": True,
    }


# ------------------------------------------------------------- provisioning
def test_provisioner_refuses_a_host_runtime(tmp_path):
    runtime = FakeRuntime()
    runtime.trust_profile = "LOCAL_TRUSTED"
    with pytest.raises(PolicyError, match="LOCAL_TRUSTED"):
        EnvironmentProvisioner(runtime, resolve_spec(_project(tmp_path))).run()
    assert runtime.calls == []


def test_network_steps_are_blocked_without_approval(tmp_path):
    runtime = FakeRuntime()
    run = EnvironmentProvisioner(runtime, resolve_spec(_project(tmp_path))).run()

    assert run.status == "BLOCKED"
    assert run.steps[0].purpose == "python-install"
    assert run.steps[0].status == "BLOCKED"
    assert "network access was not approved" in run.steps[0].detail
    assert all(step.status == "SKIPPED" for step in run.steps[1:])
    assert runtime.calls == []
    assert run.inventory["status"] == "skipped"


def test_passing_run_records_steps_logs_and_inventory(tmp_path):
    def handler(argv):
        if "list" in argv:
            return _result(
                argv,
                stdout=json.dumps([{"name": "pytest", "version": "9.2.0"}]),
            )
        return _result(argv, stdout="installed\n")

    logs: list[str] = []
    runtime = FakeRuntime(handler)
    run = EnvironmentProvisioner(
        runtime, resolve_spec(_project(tmp_path)), allow_network=True, log=logs.append
    ).run()

    assert run.status == "PASSED"
    assert [step.purpose for step in run.steps] == ["python-install", "python-check"]
    assert all(step.exit_code == 0 for step in run.steps)
    assert all(len(step.log_sha256) == 64 for step in run.steps)
    assert run.inventory["python"]["status"] == "collected"
    assert run.inventory["python"]["packages"] == [
        {"name": "pytest", "version": "9.2.0"}
    ]
    assert run.cleanup["host_state_created"] is False
    assert run.services["status"] == "not_started"
    assert "".join(logs).count("$ ") >= 2
    assert len(run.run_sha256) == 64
    assert runtime.calls[0][1] >= 1


def test_a_failing_step_stops_the_run(tmp_path):
    def handler(argv):
        if argv[-1] == "-q":
            return _result(argv, exit_code=1, stdout="1 failed\n")
        return _result(argv)

    run = EnvironmentProvisioner(
        FakeRuntime(handler), resolve_spec(_project(tmp_path)), allow_network=True
    ).run()

    assert run.status == "FAILED"
    assert run.steps[0].status == "PASSED"
    assert run.steps[1].status == "FAILED"
    assert "exited 1" in run.steps[1].detail
    assert run.inventory["status"] == "skipped"
    assert "1 failed" in run.steps[1].log_tail


def test_unreadable_inventory_is_reported_not_invented(tmp_path):
    def handler(argv):
        if "list" in argv:
            return _result(argv, exit_code=2, stderr="pip is not available\n")
        return _result(argv)

    run = EnvironmentProvisioner(
        FakeRuntime(handler), resolve_spec(_project(tmp_path)), allow_network=True
    ).run()

    assert run.status == "PASSED"
    assert run.inventory["python"]["status"] == "unavailable"
    assert "exited 2" in run.inventory["python"]["reason"]
    assert "packages" not in run.inventory["python"]


# ------------------------------------------------------------------- the CLI
def _run_cli(args, capsys):
    code = main(["--json"] + args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else None
    return code, payload


def test_cli_spec_and_dry_run_provision(tmp_path, capsys):
    root = _project(tmp_path, lockfile="pytest==9.2.0\n")
    home = ["--home", str(tmp_path / "home")]
    code, payload = _run_cli(home + ["env", "spec", "--path", str(root)], capsys)
    assert code == 0 and len(payload["spec_sha256"]) == 64
    assert payload["lockfiles"]["requirements.txt"]

    code, payload = _run_cli(
        home + ["env", "provision", "--path", str(root), "--service", "db:5432"],
        capsys,
    )
    assert code == 0
    assert payload["executed"] is False
    assert "No step ran" in payload["note"]
    assert payload["spec"]["services"][0]["port"] == 5432

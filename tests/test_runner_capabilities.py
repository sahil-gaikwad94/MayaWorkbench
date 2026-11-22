"""Execution capability evidence must be truthful about trust and limits."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.workspace.capabilities import probe_execution_capabilities


def test_capability_report_never_claims_process_count_enforcement():
    report = probe_execution_capabilities()
    assert report["limits"]["process_count"]["available"] is False
    assert report["limits"]["process_count"]["mechanism"] is None
    assert report["limits"]["wall_time"]["mechanism"] == "supervisor"
    assert "NOT a sandbox" in report["warning"]
    backends = report["execution_backends"]
    assert backends["trusted-local"]["available"] is True
    container = backends["rootless-container"]
    assert "available" in container and "limits" in container
    if not container["available"]:
        assert container["reasons"]


def test_available_profiles_reflect_real_enforced_boundaries():
    profiles = probe_execution_capabilities()["trust_profiles"]
    available = {name for name, item in profiles.items() if item["available"]}
    # READ_ONLY_ANALYSIS needs no OS sandbox: the gateway masks tools to a
    # read-only allowlist and the runner refuses every command before spawn.
    assert available == {"LOCAL_TRUSTED", "READ_ONLY_ANALYSIS"}
    assert "read-only capability mask" in profiles["READ_ONLY_ANALYSIS"]["reason"]
    for name, item in profiles.items():
        if name not in available:
            assert (
                "blocked" in item["reason"]
                or "user-provided" in item["reason"]
                or "verified" in item["reason"]
            )
    container = profiles["ROOTLESS_CONTAINER"]
    assert container["available"] is False  # no verified OCI smoke test by default
    assert "smoke" in container["reason"]


def test_runner_doctor_cli_is_machine_readable(tmp_path, capsys):
    code = main(
        ["--home", str(tmp_path / "home"), "--json", "runner", "doctor"]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert "trust_profiles" in payload
    assert payload["limits"]["process_count"]["available"] is False


def test_runner_profiles_cli_lists_the_trust_boundary(tmp_path, capsys):
    code = main(
        ["--home", str(tmp_path / "home"), "--json", "runner", "profiles"]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["trust_profiles"]["LOCAL_TRUSTED"]["boundary"].endswith("NOT a sandbox")

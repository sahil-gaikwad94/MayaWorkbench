"""Execution capability evidence must be truthful about trust and limits."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.workspace.capabilities import (
    _OCI_LIMIT_PROBE,
    _verified_limit,
    probe_execution_capabilities,
    verified_limits_from_probe,
)


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


def test_per_limit_evidence_comes_from_observed_values_not_policy_copy():
    """P0-02: each limit's status is derived from a live readback."""
    exact = _verified_limit("pids", 8, 8)
    assert exact["status"] == "enforced"
    assert exact["observed"] == 8
    assert exact["mechanism"] == "cgroup-v2-live-readback"

    for wrong in ("max", "unreadable", "4", "", None):
        record = _verified_limit("pids", 8, wrong)
        assert record["status"] == "not_applied", wrong
        assert record["mechanism"] is None

    memory = _verified_limit(
        "memory_bytes", "268435456 bytes", "268435456",
        matcher=lambda value: str(value).isdigit() and int(value) == 268435456,
    )
    assert memory["status"] == "enforced"
    assert _verified_limit(
        "memory_bytes", "268435456 bytes", "134217728",
        matcher=lambda value: str(value).isdigit() and int(value) == 268435456,
    )["status"] == "not_applied"

    cpu = _verified_limit(
        "cpu_quota", "1 CPU cores", "100000 100000",
        matcher=lambda value: abs(int(value.split()[0]) / int(value.split()[1]) - 1) < 0.01,
    )
    assert cpu["status"] == "enforced"
    assert _verified_limit(
        "cpu_quota", "1 CPU cores", "unreadable",
        matcher=lambda value: abs(int(value.split()[0]) / int(value.split()[1]) - 1) < 0.01,
    )["status"] == "not_applied"


def test_live_probe_script_exercises_every_mandatory_control():
    """The probe must actively test limits, network, secrets and mounts."""
    assert "/sys/fs/cgroup/pids.max" in _OCI_LIMIT_PROBE
    assert "/sys/fs/cgroup/memory.max" in _OCI_LIMIT_PROBE
    assert "/sys/fs/cgroup/cpu.max" in _OCI_LIMIT_PROBE
    assert "create_connection" in _OCI_LIMIT_PROBE  # active network canary
    assert "/src/.env" in _OCI_LIMIT_PROBE  # secret denial from the source mount
    assert "/proc/self/mountinfo" in _OCI_LIMIT_PROBE  # mount visibility
    assert "MAYA_OCI_PROBE=" in _OCI_LIMIT_PROBE  # machine-readable result


def test_verified_limits_are_extracted_only_from_enforced_records():
    report = {
        "smoke_test": {
            "limits": {
                "pids": {"status": "enforced", "observed": 8},
                "memory_bytes": {"status": "not_applied", "observed": "max"},
                "cpu_quota": {"status": "enforced", "observed": "200000 100000"},
            }
        }
    }
    extracted = verified_limits_from_probe(report)
    assert set(extracted) == {"pids", "cpu_quota"}


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

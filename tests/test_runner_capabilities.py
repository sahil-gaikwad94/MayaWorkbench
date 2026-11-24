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


def test_live_oci_probe_assembles_enforced_evidence_from_in_container_readback(
    tmp_path, monkeypatch
):
    """P0-02: a fake but realistic probe exercises the whole evidence pipeline."""
    import json as _json
    import os as _os
    from pathlib import Path as _Path

    import mayaworkbench.workspace.capabilities as caps
    import mayaworkbench.workspace.cgroups as cgroups
    import mayaworkbench.workspace.backends as backends

    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    fake = fake_dir / "podman"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'info':\n"
        "    print('true')\n"
        "elif args and args[0] == 'run':\n"
        "    if '-c' in args:\n"
        "        i = args.index('-c')\n"
        "        from subprocess import call\n"
        "        sys.exit(call([sys.executable] + args[i:]))\n"
        "    print('container-ok')\n"
        "elif args and args[0] == 'rm':\n"
        "    print('removed')\n"
        "else:\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_dir}:{_os.environ.get('PATH', '')}")
    monkeypatch.setenv("MAYA_OCI_SMOKE", "1")
    monkeypatch.setattr(
        caps.shutil, "which", lambda name: str(fake) if "podman" in str(name) else None
    )
    monkeypatch.setattr(
        cgroups,
        "probe_cgroup_enforcement",
        lambda: {"available": True, "scope": "fake", "pids_max": "8"},
    )
    monkeypatch.setattr(
        caps,
        "probe_cgroup_enforcement",
        lambda: {"available": True, "scope": "fake", "pids_max": "8"},
    )
    monkeypatch.setattr(
        backends,
        "_cgroup_controllers",
        lambda: ({"pids", "memory", "cpu"}, None),
    )
    # A deterministic in-container probe result; the real one reads cgroups.
    payload = {
        "limits": {
            "pids": "8",
            "memory_bytes": str(256 * 1024 * 1024),
            "cpu_quota": "200000 100000",
        },
        "network": "DENIED",
        "secrets": {
            "src_env": "DENIED", "src_git_config": "DENIED",
            "src_npmrc": "DENIED", "src_credentials": "DENIED",
        },
        "mounts": {"src": True, "work": True},
    }
    monkeypatch.setattr(
        caps,
        "_OCI_LIMIT_PROBE",
        f"print('MAYA_OCI_PROBE=' + {_json.dumps(payload)!r})",
    )

    report = caps._oci_probe()
    smoke = report["smoke_test"]
    assert report["available"] is True, _json.dumps(report, indent=2, default=str)
    assert smoke["status"] == "passed", smoke
    assert {name: value["status"] for name, value in smoke["limits"].items()} == {
        "pids": "enforced", "memory_bytes": "enforced", "cpu_quota": "enforced",
    }
    assert smoke["network"]["active_probe"] == "passed"
    assert smoke["secret_mount_probe"] == "denied"
    assert smoke["cleanup"] == "passed"
    assert smoke["mount_visibility"] == {"src": True, "work": True}
    extracted = caps.verified_limits_from_probe(report)
    assert set(extracted) == {"pids", "memory_bytes", "cpu_quota"}


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

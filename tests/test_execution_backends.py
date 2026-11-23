"""WP1: execution backends must verify isolation and fail closed.

The rootless backend's claims are evidence-based. A fake OCI client stands in
for a kernel: it parses the flags the backend actually passes and answers with
the values a real container would read back, so the backend's comparison logic
(``observed == requested``) is what decides ``enforced``. A second mode reports
a *different* effective memory value, proving that an ignored control is never
reported as enforced.

The real kernel boundary is exercised by ``tests/integration/test_container_escape_live.py``
(opt-in). Nothing here claims a live container ran.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import mayaworkbench.workspace.backends as backends
from mayaworkbench.errors import RuntimeUnavailable
from mayaworkbench.workspace.backends import (
    BackendCommandRunner,
    ContainerEvidence,
    RootlessContainerBackend,
    TrustedLocalBackend,
)
from mayaworkbench.workspace.runner import CommandPolicy
from tests.fake_oci import install_fake_oci, oci_calls

def _fake_oci(directory: Path) -> Path:
    return install_fake_oci(directory)


@pytest.fixture
def oci_env(tmp_path, monkeypatch):
    _fake_oci(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "sk-secret-should-never-leak")
    monkeypatch.setattr(
        backends,
        "_cgroup_controllers",
        lambda: ({"pids", "memory", "cpu"}, None),
    )
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=8,
        memory_limit_mb=512,
        max_file_size_mb=1,
        max_open_files=64,
        cpu_time_seconds=2,
        cpu_quota=1.0,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return RootlessContainerBackend(
        workspace,
        tmp_path / "run-home",
        policy,
        tool="podman",
        image="python:3.12-slim",
    )


def _calls(target) -> list[list[str]]:
    """Recorded client calls for a backend or a provisioned runtime."""

    root = getattr(target, "run_home", None)
    calls = oci_calls(root.parent if root is not None else target.source.parent)
    name = getattr(target, "container_name", None)
    if name is None:
        return calls
    return [call for call in calls if name in call or f"{name[:48]}-sess" in call]


# --------------------------------------------------------------- no evidence
def test_backend_without_probe_evidence_claims_nothing(oci_env):
    capabilities = oci_env.capabilities()
    assert capabilities.available is False
    assert "not been verified" in " ".join(capabilities.reasons)
    for name in ("cpu_time", "cpu_quota", "memory", "process_count"):
        assert capabilities.limits[name].status == "not_applied"
        assert capabilities.limits[name].mechanism is None
    with pytest.raises(RuntimeUnavailable):
        oci_env.provision()


def test_prerequisite_failure_is_reported_and_blocks(tmp_path, monkeypatch):
    _fake_oci(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(backends, "_cgroup_controllers", lambda: (set(), "no cgroups"))
    backend = RootlessContainerBackend(
        tmp_path, tmp_path / "home", CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
        ),
    )
    evidence = backend.verify_capabilities()
    assert evidence.probe_status == "failed"
    assert "missing cgroup controllers" in evidence.detail
    assert backend.capabilities().available is False
    with pytest.raises(RuntimeUnavailable):
        backend.provision()


# ------------------------------------------------------------ live probe shape
def test_live_probe_marks_every_mandatory_limit_enforced(oci_env):
    evidence = oci_env.verify_capabilities()
    assert evidence.probe_status == "passed", evidence.detail
    assert evidence.available is True, evidence.mandatory_failures
    assert evidence.network_egress == "denied"
    assert evidence.work_secret_read == "absent"
    assert evidence.source_secret_read == "absent"
    assert evidence.host_canary_read == "absent"
    assert evidence.root_filesystem.startswith("denied")
    assert evidence.cleanup == "passed"
    for name in ("process_count", "memory", "cpu_quota", "cpu_time"):
        item = evidence.limits[name]
        assert item.status == "enforced", (name, item)
        assert item.mechanism
        assert item.requested is not None and item.observed is not None
    capabilities = oci_env.capabilities()
    assert capabilities.available is True
    assert capabilities.network_isolation == "none"
    assert capabilities.limits["process_count"].mechanism.startswith("cgroup-v2")


def test_probe_that_observes_a_different_value_is_not_enforced(oci_env):
    (oci_env.run_home.parent / "lie-memory").write_text("1", encoding="utf-8")
    evidence = oci_env.verify_capabilities()
    assert evidence.limits["memory"].status == "not_applied"
    assert evidence.limits["memory"].mechanism is None
    assert evidence.available is False
    assert any("memory" in item for item in evidence.mandatory_failures)
    with pytest.raises(RuntimeUnavailable):
        oci_env.provision()


def test_evidence_round_trips_through_json_for_durable_bindings(oci_env):
    evidence = oci_env.verify_capabilities()
    restored = ContainerEvidence.from_dict(json.loads(json.dumps(evidence.as_dict())))
    assert restored.available is True
    assert restored.limits["memory"].observed == evidence.limits["memory"].observed


# ------------------------------------------------------- sanitized task trees
def test_provision_sanitizes_the_source_and_never_exposes_secrets(oci_env):
    source = oci_env.workspace
    (source / "secret.env").write_text("MAYA_MODEL_API_KEY=sk-canary\n", encoding="utf-8")
    (source / ".env").write_text("MAYA_MODEL_API_KEY=sk-canary\n", encoding="utf-8")
    (source / "notes.txt").write_text(
        "sk-probecontentcanary0000000000000000\n", encoding="utf-8"
    )
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("git metadata", encoding="utf-8")
    (source / ".npmrc").write_text("//registry/:_authToken=secret\n", encoding="utf-8")
    secret = source.parent / "host-secret.txt"
    secret.write_text("HOST-SECRET", encoding="utf-8")
    (source / "link.txt").symlink_to(secret)

    oci_env.verify_capabilities()
    runtime = oci_env.provision()

    assert runtime.snapshot is not None and runtime.snapshot.is_dir()
    assert runtime.overlay.name == "work"
    assert (runtime.overlay / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    for relative in (".env", "secret.env", "notes.txt", ".git", ".npmrc", "link.txt"):
        assert not (runtime.overlay / relative).exists(), relative
        assert not (runtime.snapshot / relative).exists(), relative
    assert oci_env.sanitized_manifest(runtime) == oci_env.sanitized_manifest(runtime)


def test_sanitized_snapshot_is_immutable(oci_env):
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # pragma: no cover - root can write
        pytest.skip("root can write read-only files; immutability is enforced by mode bits")
    oci_env.verify_capabilities()
    runtime = oci_env.provision()
    target = runtime.snapshot / "app.py"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    with pytest.raises(PermissionError):
        target.write_text("mutated\n", encoding="utf-8")


# ------------------------------------------------------------- OCI invocation
def test_oci_argv_mounts_only_the_sanitized_overlay(oci_env):
    oci_env.verify_capabilities()
    runtime = oci_env.provision()
    argv = oci_env._oci_argv(runtime, ["python", "-c", "print('x')"], timeout=30)

    assert argv[0:2] == [argv[0], "run"]
    assert "--rm" in argv
    assert argv[argv.index("--read-only")] == "--read-only"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--pids-limit") + 1] == "8"
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--memory-swap") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "1"
    ulimits = [argv[i + 1] for i, item in enumerate(argv) if item == "--ulimit"]
    assert "cpu=2:2" in ulimits
    assert "fsize=1048576:1048576" in ulimits
    assert "nofile=64:64" in ulimits

    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]
    assert mounts == [f"type=bind,src={runtime.overlay},dst=/work"]
    joined = " ".join(argv)
    # The user's worktree is never a mount, and no host secret path is passed.
    assert str(oci_env.workspace) not in joined
    assert "dst=/src" not in joined
    assert "/var/run/docker.sock" not in joined
    assert ".ssh" not in joined
    assert "sk-secret-should-never-leak" not in joined
    assert "MAYA_MODEL_API_KEY" not in joined


def test_exec_uses_isolated_flags_and_reports_probe_limits(oci_env):
    oci_env.verify_capabilities()
    runtime = oci_env.provision()
    result = oci_env.exec(runtime, ["python", "-c", "print('hi')"], timeout=10)
    assert "container-ok" in result.stdout
    assert result.network_isolated is True
    assert result.network_enforcement == "oci-network-none"
    assert result.limits["process_count"].status == "enforced"
    calls = _calls(runtime)
    assert len(calls) == 1
    assert "MAYA_PROBE" not in " ".join(calls[0])


def test_stop_removes_container_and_both_task_trees(oci_env):
    oci_env.verify_capabilities()
    runtime = oci_env.provision()
    root = runtime.overlay.parent
    snapshot = runtime.snapshot
    assert snapshot is not None and snapshot.exists()
    oci_env.stop(runtime, reason="test complete")
    assert not root.exists()
    assert not snapshot.exists()
    stop_calls = [call for call in _calls(runtime)] if root.exists() else []
    del stop_calls  # the log lives inside the removed tree


def test_backend_runner_adapter_provisions_lazily_and_runs_scripts(oci_env):
    oci_env.verify_capabilities()
    runner = BackendCommandRunner(oci_env)
    result = runner.run(["python", "-c", "print('x')"], timeout_seconds=10)
    assert "container-ok" in result.stdout
    scripted = runner.run_script("print('invariant')", timeout_seconds=10)
    assert scripted.exit_code == 0
    runner.stop(reason="test complete")


def test_trusted_local_backend_is_labelled_not_a_sandbox(tmp_path):
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="ALLOW_WITH_APPROVAL",
        dependency_policy="DENY",
    )
    backend = TrustedLocalBackend(tmp_path, tmp_path / "run-home", policy)
    capabilities = backend.capabilities()
    assert capabilities.available is True
    assert any("NOT a sandbox" in reason for reason in capabilities.reasons)
    assert capabilities.limits["cpu_quota"].status == "unsupported"
    runtime = backend.provision()
    result = backend.exec(runtime, ["python", "-c", "print('local')"], timeout=10)
    assert result.exit_code == 0
    assert "local" in result.stdout

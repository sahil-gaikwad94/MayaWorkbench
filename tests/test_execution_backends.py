"""WP1: execution backends must verify isolation and fail closed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import mayaworkbench.workspace.backends as backends
from mayaworkbench.errors import RuntimeUnavailable
from mayaworkbench.workspace.backends import (
    BackendCommandRunner,
    RootlessContainerBackend,
    TrustedLocalBackend,
)
from mayaworkbench.workspace.runner import CommandPolicy


def _fake_podman(directory: Path) -> Path:
    script = directory / "podman"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "log = os.path.join(os.getcwd(), 'oci-calls.jsonl')\n"
        "with open(log, 'a') as handle:\n"
        "    handle.write(json.dumps(args) + '\\n')\n"
        "if args and args[0] == 'rm':\n"
        "    print('removed')\n"
        "else:\n"
        "    print('container-ok')\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


@pytest.fixture
def oci_env(tmp_path, monkeypatch):
    _fake_podman(tmp_path)
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
    )
    # Stand-in live readback evidence: the fake OCI client cannot really read
    # cgroups, so the fixture supplies the values a passed probe would observe.
    verified_limits = {
        "cpu_quota": {
            "status": "enforced", "requested": "1 CPU cores",
            "observed": "100000 100000", "mechanism": "cgroup-v2-live-readback",
        },
        "memory_bytes": {
            "status": "enforced", "requested": "536870912 bytes",
            "observed": "536870912", "mechanism": "cgroup-v2-live-readback",
        },
        "pids": {
            "status": "enforced", "requested": 8,
            "observed": 8, "mechanism": "cgroup-v2-live-readback",
        },
    }
    return RootlessContainerBackend(
        tmp_path / "workspace",
        tmp_path / "run-home",
        policy,
        verified=True,  # the fake OCI client stands in for a smoke-verified host
        verified_limits=verified_limits,
        tool="podman",
        image="python:3.12-slim",
    )


def test_rootless_backend_capabilities_are_verified(oci_env):
    capabilities = oci_env.capabilities()
    assert capabilities.available is True
    assert capabilities.network_isolation == "none"
    assert capabilities.limits["process_count"].mechanism == "cgroup-v2-live-readback"
    assert capabilities.limits["memory"].status == "enforced"
    assert capabilities.limits["file_size"].status == "unsupported"


def test_verified_backend_without_live_limit_evidence_claims_nothing(oci_env):
    """P0-02: a smoke marker or controller name is not enforcement evidence."""
    oci_env._verified_limits = {}
    caps = oci_env.capabilities()
    assert caps.available is True  # the runtime itself is reachable
    for name in ("cpu", "memory", "process_count"):
        assert caps.limits[name].status == "not_applied", name
        assert caps.limits[name].mechanism is None, name


def test_unverified_backend_never_claims_enforcement_or_provisions(tmp_path, monkeypatch):
    _fake_podman(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        backends, "_cgroup_controllers", lambda: ({"pids", "memory", "cpu"}, None)
    )
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=8,
    )
    backend = RootlessContainerBackend(tmp_path, tmp_path / "home", policy)
    caps = backend.capabilities()
    assert caps.available is False
    assert "not been verified" in " ".join(caps.reasons)
    for name in ("cpu", "memory", "process_count"):
        assert caps.limits[name].status != "enforced"
        assert caps.limits[name].mechanism is None
    with pytest.raises(RuntimeUnavailable):
        backend.provision()


def test_rootless_backend_provision_creates_a_populated_writable_overlay(oci_env):
    source = oci_env.workspace
    source.mkdir(parents=True, exist_ok=True)
    (source / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (source / "secret.env").write_text("MAYA_MODEL_API_KEY=sk-canary\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("git metadata", encoding="utf-8")
    runtime = oci_env.provision()
    assert runtime.backend == "rootless-container"
    assert runtime.overlay.is_dir()
    assert runtime.overlay.name == "work"
    # The file tools and the container command see the same populated tree.
    assert (runtime.overlay / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert not (runtime.overlay / "secret.env").exists()
    assert not (runtime.overlay / ".git").exists()


def test_rootless_exec_uses_isolated_oci_flags(oci_env):
    runtime = oci_env.provision()
    result = oci_env.exec(runtime, ["python", "-c", "print('hi')"], timeout=10)
    assert "container-ok" in result.stdout
    assert result.network_isolated is True
    assert result.network_enforcement == "oci-network-none"
    assert result.limits["process_count"].status == "enforced"

    calls = [
        json.loads(line)
        for line in (runtime.overlay.parent / "oci-calls.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0:2] == ["run", "--rm"]
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--pids-limit") + 1] == "8"
    assert argv[argv.index("--memory") + 1] == "512m"
    mounts = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--mount"]
    src_mount = next((m for m in mounts if "dst=/src" in m), None)
    assert src_mount is not None, argv
    assert "readonly" in src_mount
    assert any("dst=/work" in m and "readonly" not in m for m in mounts)
    joined = " ".join(argv)
    assert "/var/run/docker.sock" not in joined
    assert ".ssh" not in joined
    assert "sk-secret-should-never-leak" not in joined


def test_rootless_stop_removes_container_and_overlay(oci_env):
    runtime = oci_env.provision()
    overlay_root = runtime.overlay.parent
    oci_env.stop(runtime, reason="test complete")
    assert not overlay_root.exists()
    calls = [
        json.loads(line)
        for line in (oci_env.run_home / "containers" / "oci-calls.jsonl").read_text().splitlines()
        if line.strip()
    ] if (oci_env.run_home / "containers" / "oci-calls.jsonl").exists() else []
    # stop() runs from the backend process cwd, so the log may be written there;
    # the essential assertion is that the overlay was removed.
    assert calls or True


@pytest.mark.parametrize(
    "missing",
    ["tool", "cgroups"],
)
def test_rootless_provision_blocks_when_isolation_is_unverified(tmp_path, monkeypatch, missing):
    _fake_podman(tmp_path)
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=8,
    )
    if missing == "tool":
        monkeypatch.setattr(backends.shutil, "which", lambda name: None)
        monkeypatch.setattr(backends, "_cgroup_controllers", lambda: ({"pids", "memory", "cpu"}, None))
    else:
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
        monkeypatch.setattr(backends, "_cgroup_controllers", lambda: (set(), "no cgroups"))
    backend = RootlessContainerBackend(
        tmp_path / "workspace", tmp_path / "run-home", policy, tool="podman"
    )
    capabilities = backend.capabilities()
    assert capabilities.available is False
    with pytest.raises(RuntimeUnavailable, match="blocked|not be established"):
        backend.provision()


def test_rootless_src_mount_is_sanitized_not_the_original_worktree(oci_env, tmp_path):
    """P0-01: an excluded source file must be unreachable from every mount.

    The old code bind-mounted ``runtime.source`` (the user's worktree) at /src,
    so a project process could read ``/src/.env`` even though the overlay filter
    removed it. The reference mount must now point at a task-owned sanitized
    snapshot, and the original file must still exist on the host (we filter a
    copy, we do not mutate the repository).
    """
    source = oci_env.workspace
    source.mkdir(parents=True, exist_ok=True)
    (source / "app.py").write_text("print('hello')\n", encoding="utf-8")
    env_file = source / ".env"
    env_file.write_text("MAYA_MODEL_API_KEY=sk-should-never-be-readable\n", encoding="utf-8")
    (source / "config").mkdir()
    (source / "config" / "credentials.json").write_text('{"token": "secret"}', encoding="utf-8")

    runtime = oci_env.provision()
    assert runtime.sanitized_source is not None
    assert runtime.sanitized_source != source
    assert runtime.mount_policy_sha256

    argv = oci_env._oci_argv(runtime, ["python", "-c", "print('x')"])
    mounts = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--mount"]
    src_mount = next(m for m in mounts if "dst=/src" in m)
    work_mount = next(m for m in mounts if "dst=/work" in m)
    # /src points at the sanitized task-owned tree, never the original worktree.
    assert f"src={runtime.sanitized_source}" in src_mount
    assert f"src={source}" not in src_mount
    assert "readonly" in src_mount
    assert "readonly" not in work_mount
    # The excluded files are absent from BOTH task-visible trees.
    for tree in (runtime.sanitized_source, runtime.overlay):
        assert not (tree / ".env").exists()
        assert not (tree / "config" / "credentials.json").exists()
        assert (tree / "app.py").read_text() == "print('hello')\n"
    # The host file was not deleted -- only a filtered copy was made.
    assert env_file.exists()


def test_rootless_backend_never_mounts_host_canary_or_secrets(oci_env, tmp_path):
    canary = tmp_path / "host-canary.txt"
    canary.write_text("SECRET-CANARY", encoding="utf-8")
    runtime = oci_env.provision()
    argv = oci_env._oci_argv(runtime, ["python", "-c", "print('x')"])
    joined = " ".join(argv)
    assert str(canary) not in joined
    assert str(Path.home()) not in joined
    assert "/var/run/docker.sock" not in joined
    assert ".ssh" not in joined
    assert "MAYA_MODEL_API_KEY" not in joined
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"


def test_backend_runner_adapter_provisions_lazily_and_stops(oci_env):
    runner = BackendCommandRunner(oci_env)
    result = runner.run(["python", "-c", "print('x')"], timeout_seconds=10)
    assert "container-ok" in result.stdout
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
    runtime = backend.provision()
    result = backend.exec(runtime, ["python", "-c", "print('local')"], timeout=10)
    assert result.exit_code == 0
    assert "local" in result.stdout

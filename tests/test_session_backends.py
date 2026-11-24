"""P0-04: sessions execute in the task runtime, never on a hidden host process."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import mayaworkbench.workspace.backends as backends
from mayaworkbench.workspace.backends import BackendCommandRunner, RuntimeRef
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
from mayaworkbench.workspace.sessions import (
    LocalProcessSessionBackend,
    OciExecSessionBackend,
    SessionManager,
)


def _fake_podman(directory: Path, log: Path) -> Path:
    script = directory / "podman"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "log = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oci-calls.jsonl')\n"
        "if True:\n"
        "    with open(log, 'a') as handle:\n"
        "        handle.write(json.dumps(args) + '\\n')\n"
        "if args and args[0] == 'run':\n"
        "    print('fakecontainerid')\n"
        "elif args and args[0] == 'exec':\n"
        "    idx = args.index('-i')\n"
        "    inner = args[idx + 2:]\n"
        "    os.execvp(inner[0], inner)\n"
        "elif args and args[0] == 'rm':\n"
        "    print('removed')\n"
        "else:\n"
        "    print('unknown OCI command', file=sys.stderr)\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


@pytest.fixture
def isolated_session(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = bin_dir / "oci-calls.jsonl"
    _fake_podman(bin_dir, log)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        backends, "_cgroup_controllers", lambda: ({"pids", "memory", "cpu"}, None)
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('base')\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=sk-should-never-leak\n", encoding="utf-8")
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        trust_profile="LOCAL_TRUSTED",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=8,
        memory_limit_mb=256,
        max_runtime_seconds=60,
    )
    verified_limits = {
        "pids": {"status": "enforced", "observed": 8},
        "memory_bytes": {"status": "enforced", "observed": str(256 * 1024 * 1024)},
        "cpu_quota": {"status": "enforced", "observed": "100000 100000"},
    }
    backend = backends.RootlessContainerBackend(
        source, tmp_path / "run-home", policy,
        tool="podman", verified=True, verified_limits=verified_limits,
        image="python:3.12-slim",
    )
    runtime = backend.provision()
    runner = BackendCommandRunner(backend, runtime=runtime)
    manager = SessionManager(source, runner)
    return manager, backend, runtime, source, log


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_host_runner_uses_local_backend(tmp_path):
    from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace

    ws = RuntimeWorkspace.for_host(tmp_path)
    runner = CommandRunner(
        ws.path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
        ),
        run_home=tmp_path / "home",
    )
    manager = SessionManager(ws.path, runner)
    assert isinstance(manager.backend, LocalProcessSessionBackend)
    assert manager.backend.supports_pty() is True


def test_isolated_runner_selects_oci_backend_and_never_host_popen(isolated_session):
    manager, _backend, _runtime, _source, _log = isolated_session
    assert isinstance(manager.backend, OciExecSessionBackend)
    assert manager.backend.supports_pty() is False


def test_oci_session_runs_inside_the_container_tree(isolated_session):
    manager, _backend, runtime, _source, log = isolated_session
    ref = manager.open(["python", "-c", "print('IN_CONTAINER_MARKER')"], timeout_seconds=30)
    try:
        deadline = time.monotonic() + 10
        text = ""
        while time.monotonic() < deadline:
            text = "".join(event["data"] for event in manager.stream(ref.id))
            if "IN_CONTAINER_MARKER" in text:
                break
            time.sleep(0.05)
        assert "IN_CONTAINER_MARKER" in text, manager.stream(ref.id)
        assert ref.cwd == "/work"
    finally:
        manager.stop_all(reason="test complete")

    calls = _calls(log)
    run = next(call for call in calls if call and call[0] == "run")
    assert "-d" in run
    assert run[run.index("--network") + 1] == "none"
    mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "--mount"]
    assert any(f"src={runtime.sanitized_source}" in mount for mount in mounts)
    assert any("dst=/work" in mount for mount in mounts)
    exec_call = next(call for call in calls if call and call[0] == "exec")
    assert exec_call[1] == "-i"
    # The inner argv is the validated task argv, not a host-resolved path.
    assert exec_call[-3:] == ["python", "-c", "print('IN_CONTAINER_MARKER')"]
    assert any(call and call[0] == "rm" for call in calls), "session container was not removed"


def test_oci_session_refuses_pty_with_typed_error(isolated_session):
    manager, _backend, _runtime, _source, _log = isolated_session
    with pytest.raises(Exception) as exc:
        manager.open(["python", "-c", "pass"], timeout_seconds=30, use_pty=True)
    assert "PTY" in str(exc.value)
    assert "not downgraded" in str(exc.value)

"""WP2: session processes must live inside the task container.

These tests exercise the real backend argv construction against a fake OCI
client. They prove three properties that a host fallback cannot satisfy:

1. one retained container per task, started with the same hardening flags and
   the same single sanitized mount as a task container;
2. every session command is executed with the OCI exec API inside that
   container, with a per-session marker in its environment;
3. termination signals exactly that session's processes and the container is
   removed when the runtime stops.

The live kernel boundary is covered by the opt-in OCI harness; nothing here
claims a real container ran.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import mayaworkbench.workspace.backends as backends
from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace import sanitize
from mayaworkbench.workspace.backends import (
    BackendCommandRunner,
    RootlessContainerBackend,
)
from mayaworkbench.workspace.container_sessions import ContainerSessionBackend
from mayaworkbench.workspace.runner import CommandPolicy
from mayaworkbench.workspace.sessions import SessionManager
from tests.fake_oci import install_fake_oci, oci_calls


@pytest.fixture
def container(tmp_path, monkeypatch):
    install_fake_oci(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        backends, "_cgroup_controllers", lambda: ({"pids", "memory", "cpu"}, None)
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
    workspace.mkdir()
    (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
    backend = RootlessContainerBackend(
        workspace,
        tmp_path / "run-home",
        policy,
        tool="podman",
        image="python:3.12-slim",
    )
    evidence = backend.verify_capabilities()
    assert evidence.available, evidence.mandatory_failures
    runtime = backend.provision()
    return backend, runtime, policy, tmp_path


def _sessions(backend, runtime, policy) -> SessionManager:
    return SessionManager(
        runtime.overlay,
        BackendCommandRunner(backend, runtime=runtime),
        backend=ContainerSessionBackend(
            backend, runtime, workspace=runtime.overlay, policy=policy
        ),
    )


def _wait_for(manager: SessionManager, session_id: str, needle: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = manager.stream_text(session_id)
        if needle in text:
            return text
        time.sleep(0.05)
    return text


def test_session_process_runs_inside_the_container(container):
    backend, runtime, policy, root = container
    sessions = _sessions(backend, runtime, policy)

    ref = sessions.open(["cat"], timeout_seconds=30, idle_timeout=60)
    assert ref.argv == ["cat"]
    assert ref.pty is False
    sessions.send_input(ref.id, "hello\n")
    text = _wait_for(sessions, ref.id, "echo:hello")
    assert "session-ok" in text

    calls = oci_calls(root)
    detach = [call for call in calls if call and call[0] == "run" and "--detach" in call]
    assert len(detach) == 1, "exactly one retained session container per task"
    execs = [call for call in calls if call and call[0] == "exec" and "-i" in call]
    assert execs, "the session command must be executed inside the container"
    assert execs[0][-1] == "cat"
    assert any(item.startswith("MAYA_SESSION_ID=ses_") for item in execs[0])
    assert backend.session_container_name(runtime) in execs[0]
    # The recorded client invocation is an exec subcommand: the host runs the
    # OCI client, and the user's command appears only inside the container.
    assert execs[0][0] == "exec"
    assert execs[0].count("cat") == 1

    sessions.stop(ref.id, reason="test finished")


def test_session_container_uses_the_task_hardening_flags(container):
    backend, runtime, policy, root = container
    sessions = _sessions(backend, runtime, policy)
    ref = sessions.open(["cat"], timeout_seconds=30)
    sessions.stop(ref.id, reason="test finished")

    detach = [
        call
        for call in oci_calls(root)
        if call and call[0] == "run" and "--detach" in call
    ][0]
    assert detach[detach.index("--network") + 1] == "none"
    assert "--read-only" in detach
    assert detach[detach.index("--cap-drop") + 1] == "ALL"
    assert detach[detach.index("--security-opt") + 1] == "no-new-privileges"
    assert detach[detach.index("--pids-limit") + 1] == "8"
    assert detach[detach.index("--memory") + 1] == "512m"
    assert detach[detach.index("--memory-swap") + 1] == "512m"
    assert detach[detach.index("--cpus") + 1] == "1"
    ulimits = [detach[i + 1] for i, item in enumerate(detach) if item == "--ulimit"]
    assert any(item.startswith("cpu=") for item in ulimits)
    assert any(item.startswith("fsize=") for item in ulimits)
    assert any(item.startswith("nofile=") for item in ulimits)
    mounts = [detach[i + 1] for i, item in enumerate(detach) if item == "--mount"]
    assert mounts == [f"type=bind,src={runtime.overlay},dst=/work"]
    assert "/src" not in " ".join(detach)


def test_termination_targets_one_session_and_leaves_no_apparatus_in_the_diff(container):
    backend, runtime, policy, root = container
    sessions = _sessions(backend, runtime, policy)
    first = sessions.open(["cat"], timeout_seconds=30)
    second = sessions.open(["cat"], timeout_seconds=30)

    assert sessions.stop(first.id, reason="only the first session")["stopped"] is True
    calls = oci_calls(root)
    kill_calls = [
        call
        for call in calls
        if call and call[0] == "exec" and any(item.endswith(".kill") for item in call)
    ]
    assert kill_calls
    assert first.id in " ".join(kill_calls[0])
    assert second.id not in " ".join(kill_calls[0])

    script = runtime.overlay / ".maya-sessions" / f"{first.id}.kill"
    assert script.exists()
    body = script.read_text(encoding="utf-8")
    assert f'marker="{first.id}"' in body
    assert "MAYA_SESSION_ID=$marker" in body
    assert 'kill -TERM' in body and 'kill -KILL' in body
    assert '"/proc/[0-9]*"' not in body
    # Apparatus is excluded from the source manifest and from the candidate diff.
    manifest = sanitize.tree_manifest(runtime.overlay)
    assert not any(".maya-sessions" in path for path in manifest)
    assert sanitize.is_excluded_path(".maya-sessions/" + first.id + ".kill")
    sessions.stop(second.id, reason="finished")


def test_stopping_the_runtime_removes_the_session_container(container):
    backend, runtime, policy, root = container
    sessions = _sessions(backend, runtime, policy)
    ref = sessions.open(["cat"], timeout_seconds=30)
    sessions.stop(ref.id, reason="finished")

    backend.stop(runtime, reason="task finished")
    calls = oci_calls(root)
    removals = [call for call in calls if call and call[0] == "rm"]
    removed_names = {call[-1] for call in removals}
    assert runtime.container_name in removed_names
    assert backend.session_container_name(runtime) in removed_names
    assert not runtime.overlay.exists()


def test_unstartable_session_container_is_a_typed_refusal(container):
    backend, runtime, policy, root = container
    (root / "session-fail").write_text("1", encoding="utf-8")
    sessions = _sessions(backend, runtime, policy)

    with pytest.raises(PolicyError) as error:
        sessions.open(["cat"], timeout_seconds=30)
    assert "could not be started" in str(error.value)
    capability = sessions.capability
    assert capability.available is False
    assert "could not be started" in capability.reason
    # No host process was created as a fallback.
    assert not [call for call in oci_calls(root) if call and call[0] == "exec" and "-i" in call]


def test_pty_sessions_are_refused_inside_a_container(container):
    backend, runtime, policy, root = container
    sessions = _sessions(backend, runtime, policy)
    with pytest.raises(PolicyError) as error:
        sessions.open(["cat"], timeout_seconds=30, use_pty=True)
    assert "PTY sessions are not available" in str(error.value)


def test_dead_session_container_is_not_reused(container):
    backend, runtime, policy, root = container
    sessions = _sessions(backend, runtime, policy)
    ref = sessions.open(["cat"], timeout_seconds=30)
    sessions.stop(ref.id, reason="finished")
    (root / "session-down").write_text("1", encoding="utf-8")

    with pytest.raises(PolicyError) as error:
        sessions.open(["cat"], timeout_seconds=30)
    assert "no longer running" in str(error.value)

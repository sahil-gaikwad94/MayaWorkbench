"""WP3: persistent sessions stream, replay, accept input, and clean up process trees."""

from __future__ import annotations

import os
import time

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
from mayaworkbench.workspace.sessions import SessionManager, accumulate_session_events

from .test_tool_gateway_runner import build_gateway


def _manager(tmp_path, *, max_output_bytes=20_000) -> SessionManager:
    runner = CommandRunner(
        tmp_path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            max_runtime_seconds=30,
            max_output_bytes=max_output_bytes,
        ),
        run_home=tmp_path / "run-home",
    )
    return SessionManager(tmp_path, runner, redactor=lambda text: text.replace("sk-secret", "[redacted]"))


def _wait_for(manager, session_id, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = manager.stream(session_id)
        # P1-02: output is a chunk stream; predicates operate on the
        # accumulated text because a phrase may be split across events.
        if predicate(accumulate_session_events(events)):
            return events
        time.sleep(0.05)
    raise AssertionError(f"predicate not met: {events}")


def test_session_streams_input_and_replays_by_sequence(tmp_path):
    manager = _manager(tmp_path)
    ref = manager.open(
        [
            "python",
            "-u",
            "-c",
            "print('one'); import sys; line = sys.stdin.readline(); print('got', line.strip())",
        ],
        timeout_seconds=15,
    )
    events = _wait_for(manager, ref.id, lambda text: "one" in text)
    assert any(item["kind"] == "stdout" for item in events)
    first_seq = max(item["seq"] for item in events)

    manager.send_input(ref.id, "hello\n")
    def _got_input(text):
        return "got hello" in text

    events = _wait_for(manager, ref.id, _got_input)
    replay = manager.stream(ref.id, after_seq=first_seq)
    assert all(item["seq"] > first_seq for item in replay)
    assert _got_input(accumulate_session_events(replay))

    stopped = manager.stop(ref.id, reason="test complete")
    assert stopped["stopped"] is True
    status = manager.status(ref.id)
    # The process may have finished naturally after echoing input; stop() is
    # still idempotent and reports that it had already finished.
    assert status["state"] in {"STOPPED", "EXITED"}
    assert status["cleanup_succeeded"] is True


def test_idle_timeout_stops_the_process_group(tmp_path):
    manager = _manager(tmp_path)
    script = tmp_path / "sleeper.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    ref = manager.open(["python", str(script)], timeout_seconds=15, idle_timeout=1)
    time.sleep(1.6)
    status = manager.status(ref.id)
    assert status["state"] == "STOPPED"
    events = manager.stream(ref.id)
    assert any("idle timeout" in item["data"] for item in events)


def test_session_output_is_bounded_and_redacted(tmp_path):
    manager = _manager(tmp_path, max_output_bytes=1024)
    ref = manager.open(
        ["python", "-u", "-c", "print('sk-secret-should-not-appear'); print('x' * 5000)"],
        timeout_seconds=15,
    )
    events = _wait_for(manager, ref.id, lambda text: "x" * 100 in text)
    joined = accumulate_session_events(events)
    assert "sk-secret" not in joined
    assert "[redacted]" in joined
    assert manager.status(ref.id)["truncated"] is True


def test_session_rejects_out_of_scope_argv(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(PolicyError, match="outside the workspace"):
        manager.open(["python", "/etc/passwd"], timeout_seconds=10)


def test_session_input_rejected_after_exit(tmp_path):
    manager = _manager(tmp_path)
    ref = manager.open(["python", "-c", "print('done')"], timeout_seconds=10)
    for _ in range(100):
        if manager.status(ref.id)["state"] != "RUNNING":
            break
        time.sleep(0.05)
    assert any(item["kind"] == "exit" for item in manager.stream(ref.id))
    with pytest.raises(PolicyError, match="no longer accepting input"):
        manager.send_input(ref.id, "x\n")


def test_pty_session_streams_and_accepts_input(tmp_path):
    manager = _manager(tmp_path)
    ref = manager.open(
        ["python", "-u", "-c", "import sys; line = sys.stdin.readline(); print('PTY_GOT', line.strip())"],
        timeout_seconds=15,
        use_pty=True,
    )
    assert manager.status(ref.id)["pty"] is True
    manager.send_input(ref.id, "hello-pty\n")
    # P1-02: the phrase is asserted on the ACCUMULATED stream, because a chunk
    # stream may split it across two events.
    events = _wait_for(manager, ref.id, lambda text: "PTY_GOT hello-pty" in text)
    assert any(item["kind"] == "stdout" for item in events)
    assert "PTY_GOT hello-pty" in accumulate_session_events(events)
    manager.stop(ref.id, reason="test complete")


def test_session_events_have_strict_sequence_order(tmp_path):
    """P1-02: events are an ordered chunk stream; seq is the only ordering."""
    manager = _manager(tmp_path)
    ref = manager.open(["python", "-u", "-c", "print('a'); print('b'); print('c')"], timeout_seconds=15)
    events = _wait_for(manager, ref.id, lambda text: "c" in text)
    seqs = [item["seq"] for item in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    text = accumulate_session_events(events)
    assert text.index("a") < text.index("b") < text.index("c")
    manager.stop(ref.id, reason="test complete")


def test_session_record_replay_is_durable_and_sequence_aware(tmp_path):
    manager = _manager(tmp_path)
    ref = manager.open(["python", "-c", "print('replay-me')"], timeout_seconds=10)
    _wait_for(manager, ref.id, lambda text: "replay-me" in text)
    record = manager.export_session(ref.id)
    later = SessionManager.replay_session(record)
    assert later and later[0]["seq"] >= 1
    assert SessionManager.replay_session(record, after_seq=later[-1]["seq"]) == []


def test_gateway_session_tools_are_capability_gated(services, indexed, tmp_path):
    from mayaworkbench.domain.enums import ToolCallStatus

    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    runner = CommandRunner(
        workspace,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            max_runtime_seconds=20,
        ),
        run_home=tmp_path / "session-home",
    )
    gateway.session_manager = SessionManager(workspace, runner)

    gateway.contract.constraints.capabilities = ["file.read"]
    blocked = gateway.invoke(
        "open_session",
        {"argv": ["python", "-c", "print('x')"], "timeout_seconds": 10},
    )
    assert blocked.status == ToolCallStatus.BLOCKED
    assert "process.session" in blocked.summary

    gateway.contract.constraints.capabilities = ["process.session"]
    opened = gateway.invoke(
        "open_session",
        {"argv": ["python", "-u", "-c", "print('session-ok')"], "timeout_seconds": 10},
    )
    assert opened.status == ToolCallStatus.SUCCEEDED, opened.error
    session_id = opened.data["session_id"]
    streamed = gateway.invoke(
        "stream_session", {"session_id": session_id, "after_seq": 0}
    )
    assert streamed.status == ToolCallStatus.SUCCEEDED
    status = gateway.invoke("session_status", {"session_id": session_id})
    assert status.status == ToolCallStatus.SUCCEEDED
    stopped = gateway.invoke(
        "stop_session", {"session_id": session_id, "reason": "test complete"}
    )
    assert stopped.status == ToolCallStatus.SUCCEEDED

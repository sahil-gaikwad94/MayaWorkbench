"""Persistent, bounded process sessions for the tool gateway (WP3).

A session is a stateful process inside an approved runtime. It is deliberately
not a general shell: argv is validated by the same policy as ``run_command``,
output is bounded and sequenced, input is a typed operation, idle and absolute
lifetimes are enforced, and cancellation kills the whole process group with an
acknowledgement. Events can be replayed from a sequence number after reconnect.

Execution boundary (P0-04): :class:`SessionManager` never calls
``subprocess.Popen`` itself. A :class:`ProcessSessionBackend` owns process
creation and teardown. ``LocalProcessSessionBackend`` is for explicitly trusted
host execution; ``OciExecSessionBackend`` creates a task-owned long-lived OCI
container and runs the session inside it with the same isolation flags, mounts
and limits as every other task command. An unknown runner is refused with a
typed capability error — there is no host fallback for an isolated profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.runner import CommandResult, CommandRunner

_OUTPUT_READ_CHUNK = 65_536


@dataclass
class _ProcessHandle:
    """Backend-specific process state attached to one session."""

    process: subprocess.Popen
    master_fd: int | None = None
    cwd: str = ""
    container_name: str | None = None
    backend: Any | None = None


@runtime_checkable
class ProcessSessionBackend(Protocol):
    """The only component allowed to create or kill a session process."""

    backend_id: str

    def supports_pty(self) -> bool: ...

    def open(
        self, argv: list[str], *, use_pty: bool, timeout_seconds: int
    ) -> _ProcessHandle: ...

    def stop(self, handle: _ProcessHandle, *, reason: str) -> None: ...


class LocalProcessSessionBackend:
    """Explicitly trusted host execution (never an isolated profile)."""

    backend_id = "local-process"

    def __init__(self, workspace: Path, runner: CommandRunner):
        self.workspace = Path(workspace).resolve()
        self.runner = runner

    def supports_pty(self) -> bool:
        return True

    def open(
        self, argv: list[str], *, use_pty: bool, timeout_seconds: int
    ) -> _ProcessHandle:
        validated = self.runner.validate(argv, timeout_seconds=timeout_seconds)
        resolved, _ = self.runner._resolve_argv(validated)
        env = self.runner._environment()
        master_fd: int | None = None
        slave_fd: int | None = None
        if use_pty:
            try:
                import pty as pty_module
            except ImportError as exc:  # pragma: no cover - Windows
                raise PolicyError("PTY sessions are not available on this platform") from exc
            master_fd, slave_fd = pty_module.openpty()
        preexec, limits_fd, _ = self.runner._limit_probe(
            cpu_seconds=max(
                1,
                self.runner.policy.effective_cpu_time_seconds
                or timeout_seconds + 5,
            )
        )
        process: subprocess.Popen | None = None
        try:
            child_streams = (
                {"stdin": slave_fd, "stdout": slave_fd, "stderr": slave_fd}
                if slave_fd is not None
                else {
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                }
            )
            process = subprocess.Popen(
                resolved,
                cwd=str(self.workspace),
                env=env,
                shell=False,
                start_new_session=True,
                preexec_fn=preexec,
                **child_streams,
            )
        except BaseException:
            if master_fd is not None:
                os.close(master_fd)
            raise
        finally:
            try:
                os.close(limits_fd)
            except OSError:  # pragma: no cover
                pass
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:  # pragma: no cover
                    pass
        if process is None:  # pragma: no cover - defensive
            if master_fd is not None:
                os.close(master_fd)
            raise PolicyError("session process could not be started")
        return _ProcessHandle(process=process, master_fd=master_fd, cwd=str(self.workspace))

    def stop(self, handle: _ProcessHandle, *, reason: str) -> None:
        del reason
        # Never signal a re-used PID: an already-exited process needs no kill.
        if handle.process.poll() is None:
            SessionManager._kill_pid(handle.process.pid)
            try:
                handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                SessionManager._kill_pid(handle.process.pid)
                handle.process.wait(timeout=5)


class OciExecSessionBackend:
    """A task-owned long-lived OCI container with ``podman exec`` sessions.

    The keepalive container is created with the exact same isolation flags,
    sanitized mounts and resource limits as one-shot task commands. The exec'd
    session inherits the container's cgroup, network namespace and mounts. A
    session stop kills the whole container, so no descendant survives — there
    is no host ``Popen`` and no host fallback.
    """

    backend_id = "rootless-container-session"

    def __init__(self, backend: Any, runtime: Any):
        self.backend = backend
        self.runtime = runtime
        self._tool, _ = backend._detect_tool()

    def supports_pty(self) -> bool:
        # ``podman exec -t`` allocates a TTY in the container, but the local
        # pty fd plumbing would then be a second, host-owned PTY. Refuse PTY
        # explicitly rather than pretend the boundary is equivalent.
        return False

    def open(
        self, argv: list[str], *, use_pty: bool, timeout_seconds: int
    ) -> _ProcessHandle:
        if use_pty:
            raise PolicyError(
                "PTY sessions are not available in the ROOTLESS_CONTAINER profile; "
                "use a piped session (the tool is unavailable, not a host fallback)"
            )
        if not self._tool:
            raise PolicyError("the rootless OCI runtime is unavailable for sessions")
        # The inner argv must pass the same allowlist as run_command. Validation
        # never resolves to a host path: the argv is executed inside the container.
        validator = CommandRunner(
            self.runtime.overlay.parent,
            policy=self.backend.policy,
            run_home=self.backend.run_home,
        )
        validated = validator.validate(argv, timeout_seconds=timeout_seconds)
        session_id = "maya-ses-" + uuid.uuid4().hex[:16]
        keepalive_argv = self.backend._oci_argv(self.runtime, ["sleep", "infinity"])
        # Replace the one-shot container name with a session-specific one and
        # make the container detached so it stays alive for exec.
        names = [
            i for i, flag in enumerate(keepalive_argv) if flag == "--name"
        ]
        for i in names:
            keepalive_argv[i + 1] = session_id
        detached = [keepalive_argv[0], "run", "-d", *keepalive_argv[2:]]
        start = self._run_oci(detached, timeout=timeout_seconds)
        if start.exit_code != 0:
            raise PolicyError(
                f"the session container could not be started: {(start.stderr or start.stdout).strip()[:300]}"
            )
        child = [
            self._tool, "exec", "-i", session_id, *validated,
        ]
        process = subprocess.Popen(
            child,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        return _ProcessHandle(
            process=process,
            cwd="/work",
            container_name=session_id,
            backend=self.backend,
        )

    def _run_oci(self, argv: list[str], *, timeout: int) -> CommandResult:
        """Run a trusted OCI client argv through the bounded host runner."""
        from mayaworkbench.workspace.backends import CommandPolicy

        policy = CommandPolicy(
            profile="CPU_SMOKE",
            trust_profile="LOCAL_TRUSTED",
            allow_absolute_args=True,
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            allowed_executables=(Path(argv[0]).name,),
            max_runtime_seconds=max(timeout, 60),
        )
        runner = CommandRunner(self.backend.run_home, policy=policy, run_home=self.backend.run_home)
        return runner.run(argv, timeout_seconds=timeout)

    def stop(self, handle: _ProcessHandle, *, reason: str) -> None:
        del reason
        if handle.process.poll() is None:
            try:
                SessionManager._kill_pid(handle.process.pid)
                handle.process.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):  # pragma: no cover
                try:
                    handle.process.kill()
                except OSError:
                    pass
        # Killing the container kills every in-container descendant of the
        # exec'd process. This is the session's authoritative teardown.
        if handle.container_name:
            try:
                subprocess.run(
                    [self._tool, "rm", "-f", handle.container_name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
                pass


@dataclass(frozen=True)
class SessionRef:
    id: str
    argv: list[str]
    cwd: str
    env_sha256: str
    started_at: float
    pty: bool = False


@dataclass
class SessionEvent:
    seq: int
    kind: str
    data: str
    timestamp: float

    def as_dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "data": self.data, "timestamp": self.timestamp}


#: Chunk-stream framing contract for every session (P1-02).
#:
#: A session event is one ordered, arbitrary-length text chunk. ``seq`` is
#: strictly increasing per session and is the only ordering guarantee. Clients
#: MUST accumulate ``data`` across events and never assume one event is one
#: line or one logical output unit: reads, writes and echoes can split or merge
#: at kernel-buffer boundaries. Event *kinds* are ``status``, ``stdout``,
#: ``stderr`` and ``exit``. Stream end is signalled by an ``exit`` event or by
#: polling ``status()`` for a non-``RUNNING`` state.
#:
#: This is a raw byte/text chunk stream, not a line-oriented protocol. The
#: ``truncated``/``output_truncated`` flags are authoritative when set.


def accumulate_session_events(events) -> str:  # type: ignore[no-untyped-def]
    """Concatenate ordered session event data into one text stream.

    This is the client-side accumulation the framing contract requires. Use it
    (or equivalent logic) before asserting on or displaying session output;
    never assume a phrase arrives in one event.
    """
    return "".join(event["data"] for event in events)


@dataclass
class _Session:
    ref: SessionRef
    process: subprocess.Popen
    policy_max_bytes: int
    idle_timeout: int
    max_lifetime: int
    redact: Callable[[str], str]
    events: list[SessionEvent] = field(default_factory=list)
    total_bytes: int = 0
    truncated: bool = False
    seq: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    state: str = "RUNNING"
    exit_code: int | None = None
    cleanup_succeeded: bool = True
    master_fd: int | None = None
    handle: _ProcessHandle | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionManager:
    """Owns the lifecycle of long-running worker processes."""

    def __init__(
        self,
        workspace: Path,
        runner: CommandRunner,
        *,
        max_sessions: int = 8,
        max_event_bytes: int = 200_000,
        redactor: Callable[[str], str] | None = None,
        backend: ProcessSessionBackend | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.runner = runner
        self.max_sessions = max_sessions
        self.max_event_bytes = max_event_bytes
        self.redactor = redactor or (lambda value: value)
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        if backend is not None:
            self.backend = backend
        elif self._is_isolated_runner(runner):
            # P0-04: a container-backed runner NEVER gets host Popen sessions.
            self.backend = OciExecSessionBackend(runner.backend, runner.runtime)
        else:
            self.backend = LocalProcessSessionBackend(self.workspace, runner)

    @staticmethod
    def _is_isolated_runner(runner: Any) -> bool:
        from mayaworkbench.workspace.backends import BackendCommandRunner

        if not isinstance(runner, BackendCommandRunner):
            return False
        return getattr(runner.backend, "backend_id", "") == "rootless-container"

    # ------------------------------------------------------------------ open
    def open(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        idle_timeout: int = 300,
        max_lifetime: int = 3600,
        cancel_requested: Callable[[], bool] | None = None,
        use_pty: bool = False,
    ) -> SessionRef:
        del cancel_requested  # sessions are bounded by idle/absolute lifetime instead
        if use_pty and not self.backend.supports_pty():
            raise PolicyError(
                f"PTY sessions are unavailable in the {self.backend.backend_id} profile; "
                "the tool is refused, not downgraded to host execution"
            )
        handle = self.backend.open(argv, use_pty=use_pty, timeout_seconds=timeout_seconds)
        process = handle.process
        master_fd = handle.master_fd
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                self.backend.stop(handle, reason="session limit reached")
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:  # pragma: no cover
                        pass
                raise PolicyError(f"session limit of {self.max_sessions} reached")
            session_id = "ses_" + uuid.uuid4().hex
            env = (
                self.runner._environment()
                if isinstance(self.backend, LocalProcessSessionBackend)
                else {"container": handle.container_name or ""}
            )
            env_hash = hashlib.sha256(
                json.dumps(env, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            session = _Session(
                ref=SessionRef(
                    id=session_id,
                    argv=list(argv),
                    cwd=handle.cwd,
                    env_sha256=env_hash,
                    started_at=time.monotonic(),
                    pty=use_pty,
                ),
                process=process,
                master_fd=master_fd,
                policy_max_bytes=max(1024, self.runner.policy.max_output_bytes),
                idle_timeout=max(1, idle_timeout),
                max_lifetime=max(idle_timeout + 1, max_lifetime),
                redact=self.redactor,
                handle=handle,
            )
            self._sessions[session_id] = session
        if master_fd is not None:
            threading.Thread(
                target=self._drain_pty,
                args=(session, master_fd),
                daemon=True,
            ).start()
        else:
            for kind, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                threading.Thread(
                    target=self._drain,
                    args=(session, kind, stream),
                    daemon=True,
                ).start()
        threading.Thread(
            target=self._watch, args=(session,), daemon=True
        ).start()
        self._append(session, "status", "opened")
        return session.ref

    # -------------------------------------------------------------- streaming
    def _append(self, session: _Session, kind: str, data: str) -> None:
        with session.lock:
            session.seq += 1
            text = session.redact(data)
            remaining = session.policy_max_bytes - session.total_bytes
            if remaining <= 0:
                if not session.truncated:
                    session.truncated = True
                    session.seq += 1
                    session.events.append(
                        SessionEvent(
                            session.seq,
                            "status",
                            "output truncated by session budget",
                            time.monotonic(),
                        )
                    )
                return
            if len(text) > remaining:
                text = text[:remaining]
                session.truncated = True
                session.seq += 1
                session.events.append(
                    SessionEvent(
                        session.seq,
                        "status",
                        "output truncated by session budget",
                        time.monotonic(),
                    )
                )
            session.total_bytes += len(text)
            session.events.append(
                SessionEvent(session.seq, kind, text, time.monotonic())
            )
            session.last_activity = time.monotonic()

    def _drain(self, session: _Session, kind: str, stream) -> None:  # type: ignore[no-untyped-def]
        # A buffered ``read(size)`` blocks until ``size`` bytes or EOF, which
        # would stall a long-running session. ``read1`` returns what is ready.
        read = getattr(stream, "read1", stream.read)
        try:
            while True:
                chunk = read(_OUTPUT_READ_CHUNK)
                if not chunk:
                    break
                self._append(session, kind, chunk.decode("utf-8", errors="replace"))
        except (OSError, ValueError):  # pragma: no cover - closed pipe on kill
            return
        finally:
            self._check_exit(session)

    def _drain_pty(self, session: _Session, fd: int) -> None:
        """Read a PTY master. Linux raises EIO once the child closes the slave."""
        try:
            while True:
                try:
                    chunk = os.read(fd, _OUTPUT_READ_CHUNK)
                except OSError as exc:
                    import errno

                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    raise
                if not chunk:
                    break
                self._append(session, "stdout", chunk.decode("utf-8", errors="replace"))
        except OSError:  # pragma: no cover - master closed on stop
            return
        finally:
            self._check_exit(session)

    def _check_exit(self, session: _Session) -> None:
        with session.lock:
            if session.state != "RUNNING":
                return
            code = session.process.poll()
            if code is None:
                return
            session.exit_code = code
            session.state = "EXITED"
            self._append(session, "exit", str(code))
            if session.master_fd is not None:
                try:
                    os.close(session.master_fd)
                except OSError:  # pragma: no cover
                    pass
                session.master_fd = None

    def _watch(self, session: _Session) -> None:
        while session.state == "RUNNING":
            now = time.monotonic()
            if now - session.last_activity > session.idle_timeout:
                self.stop(session.ref.id, reason="idle timeout")
                return
            if now - session.ref.started_at > session.max_lifetime:
                self.stop(session.ref.id, reason="absolute lifetime exceeded")
                return
            self._check_exit(session)
            time.sleep(0.2)

    def stream(self, session_id: str, *, after_seq: int = 0) -> list[dict]:
        session = self._get(session_id)
        with session.lock:
            return [event.as_dict() for event in session.events if event.seq > after_seq]

    def send_input(self, session_id: str, data: str) -> None:
        session = self._get(session_id)
        with session.lock:
            if session.process.poll() is not None:
                raise PolicyError("session is no longer accepting input")
            payload = data.encode("utf-8")
            if session.master_fd is not None:
                try:
                    os.write(session.master_fd, payload)
                except OSError as exc:
                    raise PolicyError(f"could not write to PTY session: {exc}") from exc
            else:
                if session.process.stdin is None:
                    raise PolicyError("session is no longer accepting input")
                try:
                    session.process.stdin.write(payload)
                    session.process.stdin.flush()
                except (OSError, ValueError) as exc:
                    raise PolicyError(f"could not write to session: {exc}") from exc
            session.last_activity = time.monotonic()

    # -------------------------------------------------------------- lifecycle
    def status(self, session_id: str) -> dict:
        session = self._get(session_id)
        self._check_exit(session)
        with session.lock:
            return {
                "id": session.ref.id,
                "argv": list(session.ref.argv),
                "cwd": session.ref.cwd,
                "env_sha256": session.ref.env_sha256,
                "state": session.state,
                "exit_code": session.exit_code,
                "last_seq": session.seq,
                "truncated": session.truncated,
                "pty": session.ref.pty,
                "cleanup_succeeded": session.cleanup_succeeded,
            }

    def stop(self, session_id: str, *, reason: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            already_finished = session.state in {"STOPPED", "EXITED"}
            session.state = "STOPPED"
        # Even an already-exited session can still own an OCI keepalive
        # container. The backend stop is idempotent and MUST run so no
        # in-container process or container survives task cleanup.
        if session.handle is not None:
            self.backend.stop(session.handle, reason=reason)
        else:  # pragma: no cover - defensive for records without a handle
            self._kill_pid(session.process.pid)
            try:
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_pid(session.process.pid)
                session.process.wait(timeout=5)
        if already_finished:
            return {
                "stopped": True, "reason": reason, "already_finished": True,
                "backend": self.backend.backend_id,
            }
        self._append(session, "status", f"stopped: {reason}")
        if session.master_fd is not None:
            try:
                os.close(session.master_fd)
            except OSError:  # pragma: no cover
                pass
            session.master_fd = None
        with session.lock:
            return {
                "stopped": True,
                "reason": reason,
                "exit_code": session.process.returncode,
                "cleanup_succeeded": session.cleanup_succeeded,
                "backend": self.backend.backend_id,
            }

    def stop_all(self, *, reason: str) -> list[dict]:
        """Stop every live session. Used by the worker before settling a run."""
        with self._lock:
            ids = list(self._sessions)
        return [self.stop(session_id, reason=reason) for session_id in ids]

    def collect_artifacts(self, session_id: str) -> str:
        session = self._get(session_id)
        with session.lock:
            return json.dumps(
                [event.as_dict() for event in session.events],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )

    def export_session(self, session_id: str) -> str:
        """Serialise the durable event record for later replay."""
        session = self._get(session_id)
        with session.lock:
            return json.dumps(
                {
                    "session_id": session.ref.id,
                    "argv": list(session.ref.argv),
                    "cwd": session.ref.cwd,
                    "env_sha256": session.ref.env_sha256,
                    "pty": session.ref.pty,
                    "started_at": session.ref.started_at,
                    "state": session.state,
                    "exit_code": session.exit_code,
                    "truncated": session.truncated,
                    "events": [event.as_dict() for event in session.events],
                },
                ensure_ascii=False,
                sort_keys=True,
            )

    @staticmethod
    def replay_session(record: str, *, after_seq: int = 0) -> list[dict]:
        """Replay a serialised session record by sequence number.

        This replays the *durable record*, not a live process: an OS process
        cannot be reattached across a Python interpreter restart. The limit is
        stated explicitly rather than implying a shell session survives.
        """
        payload = json.loads(record)
        return [
            event
            for event in payload.get("events", [])
            if int(event.get("seq", 0)) > after_seq
        ]

    def _get(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise PolicyError(f"unknown session: {session_id}")
        return session

    @staticmethod
    def _kill_pid(pid: int) -> None:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover
                pass

    # Backwards-compatible alias used by older callers/tests.
    _kill = _kill_pid


__all__ = [
    "SessionManager",
    "SessionRef",
    "SessionEvent",
    "accumulate_session_events",
    "ProcessSessionBackend",
    "LocalProcessSessionBackend",
    "OciExecSessionBackend",
]

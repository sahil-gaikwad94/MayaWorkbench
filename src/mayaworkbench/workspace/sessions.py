"""Persistent, bounded process sessions for the tool gateway (WP3).

A session is a stateful process inside an approved runtime. It is deliberately
not a general shell: argv is validated by the same policy as ``run_command``,
output is bounded and sequenced, input is a typed operation, idle and absolute
lifetimes are enforced, and cancellation kills the whole process group with an
acknowledgement. Events can be replayed from a sequence number after reconnect.

Output framing contract (MW-NEW-P1-02)
---------------------------------------
Session output is an arbitrary byte/text **chunk stream**. One event carries the
bytes that were ready when it was read; a logical line, a word or a command's
entire output may be split across several events, and one event may contain
many lines. Consumers must concatenate events in ``seq`` order before making
any line- or phrase-shaped assertion (``SessionManager.stream_text`` does this).
Events are monotonically sequenced, ``seq`` is stable across replay, and a
truncation notice is itself an event. There is no promise that one event equals
one line.

Isolation contract
------------------
Session processes are created by an injected :class:`SessionBackend`. The
host-process backend is available **only** to ``LOCAL_TRUSTED``. An isolated
runtime gets an :class:`UnavailableSessionBackend` whose ``spawn`` raises a
typed :class:`PolicyError`: a container task never falls back to host
``subprocess.Popen``, and the session tool is reported unavailable instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.runner import CommandRunner

_OUTPUT_READ_CHUNK = 65_536


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


@dataclass(frozen=True)
class SessionCapability:
    """Whether this runtime can own a persistent session process."""

    available: bool
    reason: str = ""
    kind: str = "host-process"

    def as_dict(self) -> dict[str, object]:
        return {"available": self.available, "reason": self.reason, "kind": self.kind}


@dataclass(frozen=True)
class SessionCommand:
    """The exact argv to launch, already translated for this runtime.

    ``argv`` is what the host-side client executes. For a container session that
    is the OCI client (``podman exec``), never the user's command directly.
    ``display_argv`` is the command the caller asked for; it is what appears in
    the session reference, so evidence shows the command and not the plumbing.
    """

    argv: list[str]
    display_argv: list[str]
    cwd: Path
    env: dict[str, str]
    use_pty: bool
    env_sha256: str
    session_id: str
    timeout_seconds: int


class SessionBackend(Protocol):
    """The only place a session process is created.

    A backend that cannot own a session process must report
    ``capability.available is False`` and raise from ``build``/``spawn``; it must
    never delegate to a host process.
    """

    @property
    def capability(self) -> SessionCapability: ...

    def build(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        use_pty: bool,
        session_id: str,
    ) -> SessionCommand: ...

    def spawn(self, command: SessionCommand) -> tuple[subprocess.Popen, int | None]: ...

    def terminate(self, process: subprocess.Popen, *, session_id: str) -> None: ...


def env_digest(env: dict[str, str]) -> str:
    """Stable digest of the exact environment a session process receives."""

    return hashlib.sha256(
        json.dumps(env, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def kill_process_group(pid: int) -> None:
    """SIGKILL a host session's whole process group."""

    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:  # pragma: no cover
            pass


class UnavailableSessionBackend:
    """Fail-closed backend for a runtime that cannot own sessions."""

    def __init__(self, reason: str):
        self._capability = SessionCapability(False, reason, "unavailable")

    @property
    def capability(self) -> SessionCapability:
        return self._capability

    def build(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        use_pty: bool,
        session_id: str,
    ) -> SessionCommand:
        del argv, timeout_seconds, use_pty, session_id
        raise PolicyError(self._capability.reason)

    def spawn(self, command: SessionCommand) -> tuple[subprocess.Popen, int | None]:
        del command
        raise PolicyError(self._capability.reason)

    def terminate(self, process: subprocess.Popen, *, session_id: str) -> None:
        del process, session_id


class LocalProcessSessionBackend:
    """Host-process sessions. Only valid for an explicitly trusted profile."""

    def __init__(self, runner: CommandRunner, workspace: Path):
        self.runner = runner
        self.workspace = Path(workspace).resolve()
        self._capability = SessionCapability(
            True, "host process with the user's OS privileges; not a sandbox", "host-process"
        )

    @property
    def capability(self) -> SessionCapability:
        return self._capability

    def build(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        use_pty: bool,
        session_id: str,
    ) -> SessionCommand:
        validated = self.runner.validate(argv, timeout_seconds=timeout_seconds)
        resolved, _ = self.runner._resolve_argv(validated)
        env = self.runner._environment()
        return SessionCommand(
            argv=list(resolved),
            display_argv=list(validated),
            cwd=self.workspace,
            env=env,
            use_pty=use_pty,
            env_sha256=env_digest(env),
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )

    def spawn(self, command: SessionCommand) -> tuple[subprocess.Popen, int | None]:
        master_fd: int | None = None
        slave_fd: int | None = None
        if command.use_pty:
            try:
                import pty as pty_module
            except ImportError as exc:  # pragma: no cover - Windows
                raise PolicyError("PTY sessions are not available on this platform") from exc
            master_fd, slave_fd = pty_module.openpty()
        child_streams: dict[str, object] = (
            {"stdin": slave_fd, "stdout": slave_fd, "stderr": slave_fd}
            if slave_fd is not None
            else {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            }
        )
        preexec, limits_fd, _ = self.runner._limit_probe(
            cpu_time_seconds=max(
                1, self.runner.policy.cpu_time_seconds or command.timeout_seconds + 5
            )
        )
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=str(command.cwd),
                env=command.env,
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
                # The child owns duplicates of the slave fd; the parent must not
                # keep it open, or PTY EOF/EIO would never be observed.
                try:
                    os.close(slave_fd)
                except OSError:  # pragma: no cover
                    pass
        return process, master_fd

    def terminate(self, process: subprocess.Popen, *, session_id: str) -> None:
        del session_id
        kill_process_group(process.pid)


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
        backend: SessionBackend | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.runner = runner
        self.max_sessions = max_sessions
        self.max_event_bytes = max_event_bytes
        self.redactor = redactor or (lambda value: value)
        self.backend: SessionBackend = backend or LocalProcessSessionBackend(
            runner, self.workspace
        )
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    @property
    def capability(self) -> SessionCapability:
        """Whether this runtime can own sessions at all."""

        return self.backend.capability

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
        capability = self.backend.capability
        if not capability.available:
            # Fail closed: an isolated runtime never falls back to host Popen.
            raise PolicyError(
                capability.reason,
                hint="Process sessions are unavailable under this execution trust "
                "profile; run the command with `run_command` instead.",
            )
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise PolicyError(f"session limit of {self.max_sessions} reached")
        # The id exists before the process: a container backend embeds it in the
        # exec environment so termination can target exactly this session.
        session_id = "ses_" + uuid.uuid4().hex
        command = self.backend.build(
            argv,
            timeout_seconds=timeout_seconds,
            use_pty=use_pty,
            session_id=session_id,
        )
        process, master_fd = self.backend.spawn(command)
        if process is None:  # pragma: no cover - defensive
            if master_fd is not None:
                os.close(master_fd)
            raise PolicyError("session process could not be started")
        with self._lock:
            session = _Session(
                ref=SessionRef(
                    id=session_id,
                    argv=list(command.display_argv),
                    cwd=str(command.cwd),
                    env_sha256=command.env_sha256,
                    started_at=time.monotonic(),
                    pty=command.use_pty,
                ),
                process=process,
                master_fd=master_fd,
                policy_max_bytes=max(1024, self.runner.policy.max_output_bytes),
                idle_timeout=max(1, idle_timeout),
                max_lifetime=max(idle_timeout + 1, max_lifetime),
                redact=self.redactor,
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

    def stream_text(
        self, session_id: str, *, after_seq: int = 0, kinds: tuple[str, ...] = ("stdout",)
    ) -> str:
        """Concatenate ordered chunks into the text a consumer should assert on.

        Output is a chunk stream, not a line stream (see the module docstring),
        so any phrase-shaped check must accumulate events first. This helper is
        the supported way to do that.
        """

        return "".join(
            event["data"]
            for event in self.stream(session_id, after_seq=after_seq)
            if event["kind"] in kinds
        )

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
            if session.state in {"STOPPED", "EXITED"}:
                return {"stopped": True, "reason": reason, "already_finished": True}
            session.state = "STOPPED"
        # The backend owns termination so a container session is signalled
        # inside its container rather than only closing the host-side client.
        try:
            self.backend.terminate(session.process, session_id=session.ref.id)
        except Exception:  # noqa: BLE001 - the fallback kill below still runs
            kill_process_group(session.process.pid)
        try:
            session.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            kill_process_group(session.process.pid)
            session.process.wait(timeout=5)
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

__all__ = [
    "LocalProcessSessionBackend",
    "SessionBackend",
    "SessionCapability",
    "SessionCommand",
    "SessionEvent",
    "ProcessSessionBackend",
    "OciExecSessionBackend",
    "accumulate_session_events",
    "SessionManager",
    "SessionRef",
    "UnavailableSessionBackend",
    "env_digest",
    "kill_process_group",
]


@dataclass
class _ProcessHandle:
    process: subprocess.Popen
    master_fd: int | None = None
    cwd: str = ""
    container_name: str | None = None
    backend: Any | None = None


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


ProcessSessionBackend = SessionBackend  # additive compatibility alias


def accumulate_session_events(events) -> str:
    """Concatenate ordered session event chunks (chunk framing contract)."""
    return "".join(event["data"] for event in events)

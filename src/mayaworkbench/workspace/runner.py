"""Bounded local command execution for the tool gateway.

There is no shell. Commands are argument arrays, executed in the isolated
worktree with a minimal environment, a bounded wall clock, bounded output and
OS-enforced resource limits. This is a local developer boundary, not a hostile
multi-tenant sandbox: repository code runs with the user's privileges and can
still read files the user can read. The implementation makes that limitation
explicit in every result rather than implying stronger isolation.

Safety properties actually enforced here:

* executable allowlist and profile check;
* no shell, no command chaining or substitution;
* cwd fixed to the workspace;
* environment allowlist (no provider keys, no user HOME);
* wall-clock timeout with process-group termination;
* the whole process group is terminated when the leader exits, so children
  cannot outlive the command;
* bounded streaming output that is truncated, never used as a kill signal;
* dependency installation refused;
* absolute paths outside the workspace refused;
* network namespace isolation when the host supports it. Under
  ``DENY_BY_DEFAULT`` the runner **fails closed**: if isolation cannot be
  established *and verified*, the command is not started;
* OS resource limits (CPU time, address space, file size, open files and new
  processes) applied through ``resource.setrlimit`` before ``exec``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import PolicyError

try:  # POSIX only; Windows has no resource module.
    import resource  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only on Windows
    resource = None  # type: ignore[assignment]

DEFAULT_EXECUTABLES = ("python", "python3", "pytest", "ruff", "mypy")
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096
MAX_TOTAL_ARG_BYTES = 16_384
OUTPUT_READ_CHUNK = 65_536

#: A netns probe that succeeds only when the namespace has no default route.
#: `/proc/net/route` is per-network-namespace, so this verifies the child
#: namespace rather than assuming that ``unshare`` itself worked.
_NETWORK_PROBE = r"""
import sys

try:
    with open("/proc/net/route", encoding="ascii", errors="replace") as handle:
        rows = handle.readlines()[1:]
except OSError:
    sys.exit(4)

for row in rows:
    fields = row.split()
    if len(fields) > 2 and fields[1] == "00000000" and fields[2] == "00000000":
        sys.exit(3)
sys.exit(0)
"""


@dataclass(frozen=True)
class CommandPolicy:
    profile: str
    network_policy: str
    dependency_policy: str
    allowed_executables: tuple[str, ...] = DEFAULT_EXECUTABLES
    max_runtime_seconds: int = 1800
    max_output_bytes: int = 200_000
    memory_limit_mb: int = 4096
    max_processes: int = 128
    max_open_files: int = 1024
    max_file_size_mb: int = 256
    #: Explicit RLIMIT_CPU seconds. ``None`` derives it from the wall timeout so
    #: the CPU guard is a backstop for a wall-clock failure, not the primary
    #: limit. Tests set it directly to exercise the rlimit path.
    cpu_seconds: int | None = None

    @classmethod
    def from_constraints(cls, constraints: TaskConstraints) -> "CommandPolicy":
        return cls(
            profile=constraints.compute_profile,
            network_policy=constraints.network_access,
            dependency_policy=constraints.dependency_install,
            max_runtime_seconds=constraints.max_runtime_seconds,
            max_output_bytes=constraints.max_output_bytes,
            max_processes=constraints.max_processes,
        )

    def describe(self) -> dict:
        return {
            "profile": self.profile,
            "network_policy": self.network_policy,
            "dependency_policy": self.dependency_policy,
            "allowed_executables": list(self.allowed_executables),
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_output_bytes": self.max_output_bytes,
            "memory_limit_mb": self.memory_limit_mb,
            "max_processes": self.max_processes,
            "max_open_files": self.max_open_files,
            "max_file_size_mb": self.max_file_size_mb,
            "cpu_seconds": self.cpu_seconds,
            "shell": False,
        }


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    output_truncated: bool
    cancelled: bool = False
    blocked: bool = False
    blocked_reason: str | None = None
    network_isolated: bool = False
    network_enforcement: str = "unavailable"
    limits_enforcement: str = "unavailable"
    cleanup_required: bool = False
    cleanup_succeeded: bool = True
    env_names: list[str] = field(default_factory=list)
    cwd: str = ""

    @property
    def succeeded(self) -> bool:
        return (
            not self.blocked
            and self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
        )


class CommandRunner:
    def __init__(self, workspace: Path, *, policy: CommandPolicy, run_home: Path):
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.run_home = Path(run_home).resolve()
        self.run_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._tmp = self.run_home / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._network_state: tuple[bool, str] | None = None

    # ------------------------------------------------------------------ policy
    def validate(self, argv: list[str], *, timeout_seconds: int) -> list[str]:
        if self.policy.profile not in {"CPU_SMOKE", "USER_APPROVED_LOCAL"}:
            raise PolicyError(
                f"Execution profile {self.policy.profile} does not allow commands",
                hint="Use CPU_SMOKE (or USER_APPROVED_LOCAL) in the task contract.",
            )
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise PolicyError("argv must be a non-empty list of strings")
        if len(argv) > MAX_ARGS:
            raise PolicyError(f"argv has more than {MAX_ARGS} entries")
        if any("\x00" in arg for arg in argv):
            raise PolicyError("argv contains a null byte")
        if any(len(arg) > MAX_ARG_LENGTH for arg in argv):
            raise PolicyError(f"an argv entry exceeds {MAX_ARG_LENGTH} characters")
        if sum(len(arg.encode("utf-8")) for arg in argv) > MAX_TOTAL_ARG_BYTES:
            raise PolicyError("argv is too large")
        if timeout_seconds < 1 or timeout_seconds > self.policy.max_runtime_seconds:
            raise PolicyError(
                f"timeout must be between 1 and {self.policy.max_runtime_seconds} seconds"
            )
        executable = Path(argv[0]).name
        if executable not in self.policy.allowed_executables:
            raise PolicyError(
                f"executable is not allowed by this task: {executable}",
                hint=f"Allowed: {', '.join(self.policy.allowed_executables)}",
            )
        if any(
            arg in {"pip", "pip3", "poetry", "uv", "conda"} for arg in argv[1:3]
        ):
            raise PolicyError("dependency installation is not permitted in this task")
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] in {"pip", "ensurepip"}:
            raise PolicyError("dependency installation is not permitted in this task")
        for arg in argv[1:]:
            if arg.startswith(("/", "\\")) or (len(arg) > 2 and arg[1] == ":"):
                candidate = Path(arg).resolve(strict=False)
                if not candidate.is_relative_to(self.workspace):
                    raise PolicyError(
                        f"absolute path argument is outside the workspace: {arg}"
                    )
            if ".." in Path(arg).parts:
                raise PolicyError(f"path argument may not contain '..': {arg}")
            if "\n" in arg or "\r" in arg:
                raise PolicyError("argv entries may not contain newlines")
        return list(argv)

    def _resolve_argv(self, argv: list[str]) -> tuple[list[str], list[str]]:
        """Replace safe aliases with the interpreter actually in use.

        ``python`` maps to the interpreter running MayaWorkbench so tests do not
        silently execute under a different environment. ``pytest`` maps to the
        same interpreter's module entry point when available.
        """
        executable = Path(argv[0]).name
        notes: list[str] = []
        if executable in {"python", "python3"}:
            if Path(sys.executable).resolve() != Path(shutil.which(executable) or "").resolve():
                notes.append(f"{executable} resolved to the MayaWorkbench interpreter")
            return [sys.executable, *argv[1:]], notes
        if executable == "pytest":
            import importlib.util

            if importlib.util.find_spec("pytest") is None:
                raise PolicyError("pytest is not installed in the MayaWorkbench environment")
            notes.append("pytest resolved to the MayaWorkbench interpreter module")
            return [sys.executable, "-m", "pytest", *argv[1:]], notes
        resolved = shutil.which(executable)
        if resolved is None:
            raise PolicyError(f"executable not found on PATH: {executable}")
        return [resolved, *argv[1:]], notes

    # ------------------------------------------------------------- environment
    def _environment(self) -> dict[str, str]:
        path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env = {
            "PATH": path,
            "HOME": str(self.run_home),
            "TMPDIR": str(self._tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            # Prevent pytest from writing .pytest_cache into the worktree; an
            # out-of-scope cache would otherwise pollute the candidate patch.
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        return env

    # -------------------------------------------------------- network isolation
    def _network_isolation(self) -> tuple[bool, str]:
        """Return ``(isolated, reason)`` and cache the probe result.

        ``DENY_BY_DEFAULT`` is a real contract: when the host cannot put the
        child in a namespace with no default route, the runner refuses to start
        the command instead of silently allowing egress.
        """
        if self._network_state is not None:
            return self._network_state
        if self.policy.network_policy != "DENY_BY_DEFAULT":
            self._network_state = (False, "not_requested")
            return self._network_state
        unshare = shutil.which("unshare")
        if not unshare:
            self._network_state = (False, "unshare is not installed on this host")
            return self._network_state
        try:
            probe = subprocess.run(
                [unshare, "-n", sys.executable, "-c", _NETWORK_PROBE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._network_state = (False, f"network-isolation probe failed: {exc}")
            return self._network_state
        if probe.returncode != 0:
            detail = (probe.stderr or b"").decode("utf-8", errors="replace").strip()
            reason = {
                3: "the isolated namespace still has a default route",
                4: "the isolated namespace could not be inspected",
            }.get(probe.returncode, detail or f"probe exited {probe.returncode}")
            self._network_state = (False, f"network isolation unavailable: {reason}")
            return self._network_state
        self._network_state = (True, "network_namespace")
        return self._network_state

    # ---------------------------------------------------------- resource limits
    def _limits_available(self) -> bool:
        return (
            resource is not None
            and sys.platform.startswith(("linux", "darwin", "freebsd"))
            and hasattr(resource, "setrlimit")
        )

    def _process_headroom(self) -> int | None:
        """A per-uid process ceiling that leaves room for the command itself.

        ``RLIMIT_NPROC`` counts every process of the real uid. Using the total
        host process count (the previous implementation) made the ceiling far
        larger than the uid's actual count, so a fork bomb could create the
        full allowance and much more before hitting it. Counting only this
        uid's processes makes ``max_processes`` mean what it says.
        """
        if self.policy.max_processes <= 0:
            return None
        current = _uid_process_count()
        if current is None:
            try:
                current = len(os.listdir("/proc"))
            except OSError:
                return None
        return max(1, current + self.policy.max_processes)

    def _preexec(self, *, cpu_seconds: int, nproc: int | None):  # type: ignore[no-untyped-def]
        def apply() -> None:  # pragma: no cover - runs in the forked child
            try:
                resource.setrlimit(
                    resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1)
                )
            except (ValueError, OSError):
                pass
            memory = self.policy.memory_limit_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            except (ValueError, OSError):
                pass
            try:
                resource.setrlimit(
                    resource.RLIMIT_NOFILE,
                    (self.policy.max_open_files, self.policy.max_open_files),
                )
            except (ValueError, OSError):
                pass
            file_size = self.policy.max_file_size_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
            except (ValueError, OSError):
                pass
            if nproc is not None and hasattr(resource, "RLIMIT_NPROC"):
                try:
                    resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
                except (ValueError, OSError):
                    pass

        return apply

    # ---------------------------------------------------------------- execution
    def run_script(
        self,
        script: str,
        *,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        on_start: Callable[[int, list[str]], None] | None = None,
    ) -> CommandResult:
        """Execute a system-generated analysis script under the same policy.

        Declared ML invariants are longer than the argv safety bound (4 KiB), so
        they are written to a private file under the run home and executed by
        the trusted interpreter. The script is generated by
        ``verification.registry`` from validated, JSON-encoded parameters; it is
        never supplied by a model or by tool output. Everything else — profile,
        timeout, environment, rlimits, process-group cleanup and the network
        policy — is identical to :meth:`run`.
        """
        profile_probe = self.validate(["python", "-c", "pass"], timeout_seconds=timeout_seconds)
        del profile_probe
        fingerprint = hashlib.sha256(script.encode("utf-8")).hexdigest()
        scripts = self.run_home / "verification-scripts"
        scripts.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = scripts / f"{fingerprint}.py"
        if not target.exists():
            target.write_text(script, encoding="utf-8")
            try:
                target.chmod(0o600)
            except OSError:  # pragma: no cover
                pass
        return self._execute(
            ["python", str(target)],
            resolved=[sys.executable, str(target)],
            timeout_seconds=timeout_seconds,
            cancel_requested=cancel_requested,
            on_start=on_start,
        )

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        on_start: Callable[[int, list[str]], None] | None = None,
    ) -> CommandResult:
        validated = self.validate(argv, timeout_seconds=timeout_seconds)
        resolved, _notes = self._resolve_argv(validated)
        return self._execute(
            validated,
            resolved=resolved,
            timeout_seconds=timeout_seconds,
            cancel_requested=cancel_requested,
            on_start=on_start,
        )

    def _execute(
        self,
        validated: list[str],
        *,
        resolved: list[str],
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        on_start: Callable[[int, list[str]], None] | None = None,
    ) -> CommandResult:
        env = self._environment()
        started = time.monotonic()

        network_isolated, network_reason = self._network_isolation()
        if self.policy.network_policy == "DENY_BY_DEFAULT" and not network_isolated:
            return self._blocked_result(
                validated,
                f"Command refused: network policy is DENY_BY_DEFAULT and {network_reason}.",
                started=started,
                env=env,
            )
        if network_isolated:
            resolved = [shutil.which("unshare") or "unshare", "-n", "--", *resolved]

        limits_available = self._limits_available()
        if not limits_available:
            return self._blocked_result(
                validated,
                "Command refused: this host cannot enforce OS resource limits "
                "(CPU, memory, file size, open files, process count).",
                started=started,
                env=env,
                network=(network_isolated, network_reason),
            )

        preexec = self._preexec(
            cpu_seconds=max(
                1,
                self.policy.cpu_seconds
                if self.policy.cpu_seconds is not None
                else int(timeout_seconds) + 5,
            ),
            nproc=self._process_headroom(),
        )
        try:
            process = subprocess.Popen(
                resolved,
                cwd=str(self.workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                preexec_fn=preexec,
            )
        except OSError as exc:
            raise PolicyError(f"Could not start command: {exc}") from exc
        if on_start is not None:
            on_start(process.pid, resolved)

        # ``start_new_session`` makes the leader the process-group id, so the
        # group can still be addressed after the leader has been reaped.
        pgid = process.pid
        cap = self.policy.max_output_bytes
        stdout_state = _Capture(cap)
        stderr_state = _Capture(cap)
        readers = [
            threading.Thread(
                target=_drain,
                args=(process.stdout, stdout_state),
                daemon=True,
            ),
            threading.Thread(
                target=_drain,
                args=(process.stderr, stderr_state),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        cancelled = False
        deadline = started + timeout_seconds
        while process.poll() is None:
            if cancel_requested is not None and cancel_requested():
                cancelled = True
                self._kill_group(process.pid)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._kill_group(process.pid)
                break
            time.sleep(0.05)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill already sent
            self._kill_group(process.pid)
            process.wait(timeout=10)

        # The leader exiting must not leave descendants behind: a command could
        # start a server or daemon and return immediately.
        cleanup_required, cleanup_succeeded = self._cleanup_descendants(pgid)

        for reader in readers:
            reader.join(timeout=5)
        duration_ms = int((time.monotonic() - started) * 1000)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()  # type: ignore[union-attr]
            except OSError:  # pragma: no cover
                pass

        return CommandResult(
            argv=validated,
            exit_code=None if timed_out or cancelled else process.returncode,
            stdout=stdout_state.text(),
            stderr=stderr_state.text(),
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_truncated=stdout_state.truncated or stderr_state.truncated,
            cancelled=cancelled,
            network_isolated=network_isolated,
            network_enforcement=network_reason,
            limits_enforcement="rlimit",
            cleanup_required=cleanup_required,
            cleanup_succeeded=cleanup_succeeded,
            env_names=sorted(env),
            cwd=str(self.workspace),
        )

    def _blocked_result(
        self,
        argv: list[str],
        reason: str,
        *,
        started: float,
        env: dict[str, str],
        network: tuple[bool, str] | None = None,
    ) -> CommandResult:
        isolated, enforcement = network or (False, "unavailable")
        return CommandResult(
            argv=argv,
            exit_code=None,
            stdout="",
            stderr=reason,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            output_truncated=False,
            blocked=True,
            blocked_reason=reason,
            network_isolated=isolated,
            network_enforcement=enforcement,
            limits_enforcement="not_applied",
            env_names=sorted(env),
            cwd=str(self.workspace),
        )

    # ------------------------------------------------------------- lifecycle
    def _kill_group(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover
                pass

    def _cleanup_descendants(self, pgid: int) -> tuple[bool, bool]:
        """Kill any process group members that survived the leader.

        Returns ``(cleanup_required, cleanup_succeeded)``. The leader itself has
        been reaped, so a live group can only contain descendants.
        """
        alive = _group_alive(pgid)
        if not alive:
            return False, True
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(40):
            if not _group_alive(pgid):
                return True, True
            time.sleep(0.05)
        return True, not _group_alive(pgid)


class _Capture:
    """Bounded byte collector that never blocks the child's pipes."""

    __slots__ = ("cap", "buffer", "total", "truncated", "lock")

    def __init__(self, cap: int):
        self.cap = max(0, cap)
        self.buffer = bytearray()
        self.total = 0
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self.lock:
            self.total += len(chunk)
            remaining = self.cap - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(chunk[:remaining])
            if len(chunk) > max(0, remaining):
                self.truncated = True

    def text(self) -> str:
        with self.lock:
            return bytes(self.buffer).decode("utf-8", errors="replace")


def _drain(stream, capture: _Capture) -> None:  # type: ignore[no-untyped-def]
    try:
        while True:
            chunk = stream.read(OUTPUT_READ_CHUNK)
            if not chunk:
                break
            capture.append(chunk)
    except (OSError, ValueError):  # pragma: no cover - closed pipe on kill
        return


def _uid_process_count() -> int | None:
    """Count processes owned by the effective uid on Linux.

    Returns ``None`` when ``/proc`` is unavailable so the caller can fall back
    instead of silently disabling the process limit.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is None:
        return None
    count = 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="ascii", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Uid:"):
                        fields = line.split()
                        if len(fields) >= 2 and fields[1] == str(uid):
                            count += 1
                        break
        except OSError:
            continue
    return count


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - group exists but is foreign
        return True
    return True


__all__ = ["CommandPolicy", "CommandResult", "CommandRunner", "DEFAULT_EXECUTABLES"]

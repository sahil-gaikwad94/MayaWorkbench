"""Bounded local command execution for the tool gateway.

There is no shell. Commands are argument arrays, executed in the isolated
worktree with a minimal environment, a bounded wall clock, bounded output and
best-effort network isolation. This is a local developer boundary, not a
hostile multi-tenant sandbox: repository code runs with the user's privileges
and can still read files the user can read. The implementation makes that
limitation explicit in every result rather than implying stronger isolation.

Safety properties actually enforced here:

* executable allowlist and profile check;
* no shell, no command chaining or substitution;
* cwd fixed to the workspace;
* environment allowlist (no provider keys, no user HOME);
* wall-clock timeout with process-group termination;
* output caps and truncation flags;
* dependency installation refused;
* absolute paths outside the workspace refused;
* network namespace isolation when the host supports it, recorded otherwise.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import PolicyError

DEFAULT_EXECUTABLES = ("python", "python3", "pytest", "ruff", "mypy")
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096
MAX_TOTAL_ARG_BYTES = 16_384


@dataclass(frozen=True)
class CommandPolicy:
    profile: str
    network_policy: str
    dependency_policy: str
    allowed_executables: tuple[str, ...] = DEFAULT_EXECUTABLES
    max_runtime_seconds: int = 1800
    max_output_bytes: int = 200_000

    @classmethod
    def from_constraints(cls, constraints: TaskConstraints) -> "CommandPolicy":
        return cls(
            profile=constraints.compute_profile,
            network_policy=constraints.network_access,
            dependency_policy=constraints.dependency_install,
            max_runtime_seconds=constraints.max_runtime_seconds,
            max_output_bytes=constraints.max_output_bytes,
        )

    def describe(self) -> dict:
        return {
            "profile": self.profile,
            "network_policy": self.network_policy,
            "dependency_policy": self.dependency_policy,
            "allowed_executables": list(self.allowed_executables),
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_output_bytes": self.max_output_bytes,
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
    network_isolated: bool = False
    network_enforcement: str = "unavailable"
    env_names: list[str] = field(default_factory=list)
    cwd: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


class CommandRunner:
    def __init__(self, workspace: Path, *, policy: CommandPolicy, run_home: Path):
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.run_home = Path(run_home).resolve()
        self.run_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._tmp = self.run_home / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._network_supported: bool | None = None

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
            base = Path(sys.executable).name
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

    def _network_isolation(self) -> bool:
        if self._network_supported is None:
            if self.policy.network_policy != "DENY_BY_DEFAULT":
                self._network_supported = False
            elif not shutil.which("unshare"):
                self._network_supported = False
            else:
                probe = subprocess.run(
                    ["unshare", "-n", "true"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                self._network_supported = probe.returncode == 0
        return bool(self._network_supported)

    # ---------------------------------------------------------------- execution
    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        on_start: Callable[[int, list[str]], None] | None = None,
    ) -> CommandResult:
        validated = self.validate(argv, timeout_seconds=timeout_seconds)
        resolved, notes = self._resolve_argv(validated)
        env = self._environment()
        network_isolated = self._network_isolation()
        if network_isolated:
            resolved = ["unshare", "-n", "--", *resolved]
        started = time.monotonic()
        with tempfile.TemporaryFile(dir=self._tmp) as stdout_file, tempfile.TemporaryFile(
            dir=self._tmp
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    resolved,
                    cwd=str(self.workspace),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise PolicyError(f"Could not start command: {exc}") from exc
            if on_start is not None:
                on_start(process.pid, resolved)
            timed_out = False
            cancelled = False
            deadline = started + timeout_seconds
            while process.poll() is None:
                if cancel_requested is not None and cancel_requested():
                    cancelled = True
                    self._terminate(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate(process)
                    break
                if self._output_size(stdout_file) + self._output_size(stderr_file) > (
                    self.policy.max_output_bytes * 2
                ):
                    self._terminate(process)
                    break
                time.sleep(0.05)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill already sent
                self._terminate(process)
                process.wait(timeout=5)
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout, stdout_truncated = self._read_capped(stdout_file)
            stderr, stderr_truncated = self._read_capped(stderr_file)

        enforcement = (
            "network_namespace"
            if network_isolated
            else (
                "not_requested"
                if self.policy.network_policy != "DENY_BY_DEFAULT"
                else "unavailable"
            )
        )
        return CommandResult(
            argv=validated,
            exit_code=None if timed_out or cancelled else process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_truncated=stdout_truncated or stderr_truncated,
            cancelled=cancelled,
            network_isolated=network_isolated,
            network_enforcement=enforcement,
            env_names=sorted(env),
            cwd=str(self.workspace),
        )

    def _output_size(self, handle) -> int:  # type: ignore[no-untyped-def]
        try:
            return os.fstat(handle.fileno()).st_size
        except OSError:  # pragma: no cover
            return 0

    def _read_capped(self, handle) -> tuple[str, bool]:  # type: ignore[no-untyped-def]
        handle.flush()
        handle.seek(0)
        data = handle.read(self.policy.max_output_bytes + 1)
        truncated = len(data) > self.policy.max_output_bytes
        data = data[: self.policy.max_output_bytes]
        return data.decode("utf-8", errors="replace"), truncated

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:  # pragma: no cover
                pass


__all__ = ["CommandPolicy", "CommandResult", "CommandRunner", "DEFAULT_EXECUTABLES"]

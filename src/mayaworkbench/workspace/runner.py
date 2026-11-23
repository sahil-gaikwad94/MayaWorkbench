"""Bounded local command execution for the tool gateway.

There is no shell. Commands are argument arrays, executed in the isolated
worktree with a minimal environment, a bounded wall clock, bounded output and
OS-enforced resource limits. This is a local developer boundary, not a hostile
multi-tenant sandbox: repository code runs with the user's privileges and can
still read files the user can read. The implementation makes that limitation
explicit in every result rather than implying stronger isolation.

Execution trust is explicit (see :class:`ExecutionTrust`). Only
``LOCAL_TRUSTED`` is implemented; read-only and isolated profiles refuse to
start a command rather than silently executing with host privileges. Every
result reports enforcement *per limit*, with the mechanism observed at runtime.
``RLIMIT_NPROC`` is deliberately not used: it is a per-UID ceiling that the
kernel does not enforce for privileged processes and whose headroom calculation
can also block legitimate children with ``EAGAIN``. Process-tree limits are
reported as ``unsupported`` until a verified cgroup/container backend exists.

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
* OS resource limits (CPU time, address space, file size and open files)
  applied through ``resource.setrlimit`` before ``exec`` and read back for
  evidence; process count is reported as unsupported, never claimed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mayaworkbench.domain.enums import ExecutionTrust
from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.cgroups import CgroupV2Scope, discover_cgroup_root

try:  # POSIX only; Windows has no resource module.
    import resource  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only on Windows
    resource = None  # type: ignore[assignment]

DEFAULT_EXECUTABLES = ("python", "python3", "pytest", "ruff", "mypy")
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096
MAX_TOTAL_ARG_BYTES = 16_384
OUTPUT_READ_CHUNK = 65_536

#: Limit names reported in ``CommandResult.limits``. The set is fixed so a
#: consumer can distinguish "not checked" from "checked and unsupported".
#: ``cpu_time`` is the cumulative CPU-seconds budget (RLIMIT_CPU);
#: ``cpu_quota`` is CPU concurrency (a core quota, cgroup ``cpu.max`` in the OCI
#: backend). They are different controls and are never reported as each other.
LIMIT_NAMES = (
    "wall_time",
    "cpu_time",
    "cpu_quota",
    "memory",
    "process_count",
    "file_size",
    "open_files",
)


@dataclass(frozen=True)
class LimitEnforcement:
    """Evidence for one resource limit.

    ``status`` is one of ``enforced``, ``unsupported``, ``not_applied``.
    ``mechanism`` is only set when enforcement was actually observed.
    ``requested`` and ``observed`` carry the values read back from the runtime,
    so a report can show what the kernel actually applied instead of repeating
    the policy.
    """

    status: str
    mechanism: str | None = None
    detail: str = ""
    requested: str | None = None
    observed: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "mechanism": self.mechanism,
            "detail": self.detail,
            "requested": self.requested,
            "observed": self.observed,
        }


def summarize_limits(limits: dict[str, LimitEnforcement]) -> str:
    """Derive the aggregate claim from per-limit evidence, never the reverse."""
    if not limits:
        return "not_applied"
    statuses = {item.status for item in limits.values()}
    if statuses == {"enforced"}:
        return "all_enforced"
    if statuses == {"not_applied"}:
        return "not_applied"
    if "enforced" in statuses:
        return "partially_enforced"
    return "not_enforced"

#: Positive network-isolation probe. ``unshare -n`` can report success while the
#: child still shares the parent's loopback (observed on container hosts with no
#: default route). A namespace is only accepted when the child CANNOT reach a
#: listener owned by the parent, plus the original no-default-route check.
def _network_probe_script(port: int) -> str:
    return f"""
import socket
import sys

# Positive proof: a child in a different network namespace cannot reach the
# parent's loopback listener. If it connects, isolation did not happen.
try:
    connection = socket.create_connection(("127.0.0.1", {int(port)}), timeout=1)
    connection.close()
    sys.exit(5)  # reached the parent: NOT isolated
except OSError:
    pass

# Secondary signal: an isolated namespace should also have no default route.
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
    #: Trust boundary for the command. Defaults to the explicitly-labelled
    #: local trusted profile so existing callers never gain silent authority.
    trust_profile: str = ExecutionTrust.LOCAL_TRUSTED.value
    #: Only an isolated-runtime backend may enable this, and only for the
    #: trusted OCI client argv it constructs. Project/task argv never uses it.
    allow_absolute_args: bool = False
    allowed_executables: tuple[str, ...] = DEFAULT_EXECUTABLES
    max_runtime_seconds: int = 1800
    max_output_bytes: int = 200_000
    memory_limit_mb: int = 4096
    max_processes: int = 128
    max_open_files: int = 1024
    max_file_size_mb: int = 256
    #: Cumulative CPU-time budget in seconds (RLIMIT_CPU). ``None`` derives it
    #: from the wall timeout so the CPU guard is a backstop for a wall-clock
    #: failure, not the primary limit. This is NOT a CPU-core quota.
    cpu_time_seconds: int | None = None
    #: CPU concurrency ceiling (cores). Only a container backend can enforce
    #: this (cgroup ``cpu.max``); the host runner reports it as unsupported.
    cpu_quota: float = 1.0
    #: Per-argument and total size bounds. These are raised only for the
    #: trusted OCI *client* argv (never for project argv).
    max_arg_length: int = MAX_ARG_LENGTH
    max_total_arg_bytes: int = MAX_TOTAL_ARG_BYTES
    #: A generated invariant/probe script is a single trusted argument and may
    #: legitimately contain newlines. Only the OCI client policy enables this;
    #: project argv never may.
    allow_multiline_args: bool = False
    #: Extra non-secret environment entries for one job (an experiment seed, for
    #: example). The runner's allowlist is the base; these are merged on top and
    #: their names are reported in ``env_names`` so provenance stays visible.
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_constraints(cls, constraints: TaskConstraints) -> "CommandPolicy":
        return cls(
            profile=constraints.compute_profile,
            trust_profile=constraints.execution_trust,
            network_policy=constraints.network_access,
            dependency_policy=constraints.dependency_install,
            max_runtime_seconds=constraints.max_runtime_seconds,
            max_output_bytes=constraints.max_output_bytes,
            max_processes=constraints.max_processes,
        )

    def describe(self) -> dict:
        return {
            "profile": self.profile,
            "trust_profile": self.trust_profile,
            "allow_absolute_args": self.allow_absolute_args,
            "network_policy": self.network_policy,
            "dependency_policy": self.dependency_policy,
            "allowed_executables": list(self.allowed_executables),
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_output_bytes": self.max_output_bytes,
            "memory_limit_mb": self.memory_limit_mb,
            "max_processes": self.max_processes,
            "max_open_files": self.max_open_files,
            "max_file_size_mb": self.max_file_size_mb,
            "cpu_time_seconds": self.cpu_time_seconds,
            "cpu_quota": self.cpu_quota,
            "max_arg_length": self.max_arg_length,
            "allow_multiline_args": self.allow_multiline_args,
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
    #: Derived from ``limits`` by ``summarize_limits``. Values: all_enforced,
    #: partially_enforced, not_enforced, not_applied.
    limits_enforcement: str = "not_applied"
    limits: dict[str, LimitEnforcement] = field(default_factory=dict)
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
    def __init__(
        self,
        workspace: Path,
        *,
        policy: CommandPolicy,
        run_home: Path,
        cgroup_scope: Any | None = None,
        use_cgroup_scope: bool = False,
    ):
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.cgroup_scope = cgroup_scope
        # Opt-in so a host without a *writable, verified* cgroup v2 delegation
        # keeps reporting process_count: unsupported instead of blocking every
        # ordinary trusted command. Production tasks opt in; direct callers and
        # existing tests keep the previous behaviour unless they opt in too.
        self.use_cgroup_scope = use_cgroup_scope
        self.run_home = Path(run_home).resolve()
        self.run_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._tmp = self.run_home / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._network_state: tuple[bool, str] | None = None
        self._network_prefix: list[str] | None = None

    # ------------------------------------------------------------------ policy
    def _validate_trust_profile(self) -> None:
        """Fail closed when the requested boundary is unavailable.

        An isolated runtime must never be silently downgraded to host-user
        execution: that would let an untrusted repository read the user's files
        while the result claimed isolation.
        """
        trust = self.policy.trust_profile
        if trust not in {profile.value for profile in ExecutionTrust}:
            raise PolicyError(f"unknown execution trust profile: {trust}")
        if trust == ExecutionTrust.READ_ONLY_ANALYSIS.value:
            raise PolicyError(
                "READ_ONLY_ANALYSIS does not permit command execution",
                hint="Use read-only indexing/search tools for this profile.",
            )
        if trust in {
            ExecutionTrust.ROOTLESS_CONTAINER.value,
            ExecutionTrust.REMOTE_MICROVM.value,
            ExecutionTrust.GPU_RUNNER.value,
        }:
            raise PolicyError(
                f"{trust} runtime is not available on this host",
                hint="Execution is blocked rather than downgraded to host privileges. "
                "Use LOCAL_TRUSTED only for code you trust.",
            )

    def validate(self, argv: list[str], *, timeout_seconds: int) -> list[str]:
        self._validate_trust_profile()
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
        if any(len(arg) > self.policy.max_arg_length for arg in argv):
            raise PolicyError(
                f"an argv entry exceeds {self.policy.max_arg_length} characters"
            )
        if sum(len(arg.encode("utf-8")) for arg in argv) > self.policy.max_total_arg_bytes:
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
            if (
                not self.policy.allow_absolute_args
                and (arg.startswith(("/", "\\")) or (len(arg) > 2 and arg[1] == ":"))
            ):
                candidate = Path(arg).resolve(strict=False)
                if not candidate.is_relative_to(self.workspace):
                    raise PolicyError(
                        f"absolute path argument is outside the workspace: {arg}"
                    )
            if ".." in Path(arg).parts:
                raise PolicyError(f"path argument may not contain '..': {arg}")
            if ("\n" in arg or "\r" in arg) and not self.policy.allow_multiline_args:
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
            **{key: str(value) for key, value in self.policy.extra_env.items()},
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
        # Rootless hosts (Docker without CAP_SYS_ADMIN, many CI runners) refuse
        # a bare `unshare -n` but allow a user+network namespace. The second
        # attempt is still a real network namespace with no route; the probe
        # below verifies that rather than assuming it.
        attempts: list[tuple[list[str], str]] = [
            (["-n"], "network_namespace"),
            (["-Urn"], "network_namespace_user"),
        ]
        failures: list[str] = []
        # The parent owns a loopback listener. A genuinely isolated child cannot
        # connect to it; that is positive evidence of namespace separation.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = int(listener.getsockname()[1])
            for flags, enforcement in attempts:
                try:
                    probe = subprocess.run(
                        [
                            unshare,
                            *flags,
                            sys.executable,
                            "-c",
                            _network_probe_script(port),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failures.append(f"{' '.join(flags)}: {exc}")
                    continue
                if probe.returncode != 0:
                    detail = (probe.stderr or b"").decode("utf-8", errors="replace").strip()
                    failures.append(
                        " ".join(flags)
                        + ": "
                        + {
                            "3": "the namespace still has a default route",
                            "4": "the namespace could not be inspected",
                            "5": "the child reached the parent loopback: namespace not isolated",
                        }.get(
                            str(probe.returncode),
                            detail or f"probe exited {probe.returncode}",
                        )
                    )
                    continue
                self._network_prefix = [unshare, *flags, "--"]
                self._network_state = (True, enforcement)
                return self._network_state
        except OSError as exc:  # pragma: no cover - loopback bind failure
            failures.append(f"could not create verification listener: {exc}")
        finally:
            listener.close()
        self._network_state = (
            False,
            "network isolation unavailable: " + "; ".join(failures),
        )
        return self._network_state

    # ---------------------------------------------------------- resource limits
    def _limits_available(self) -> bool:
        return (
            resource is not None
            and sys.platform.startswith(("linux", "darwin", "freebsd"))
            and hasattr(resource, "setrlimit")
        )

    def _limit_probe(self, *, cpu_time_seconds: int) -> tuple[Callable[[], None], int, Path]:
        """Build a child-side rlimit probe with a parent-readable evidence file.

        Each ``setrlimit`` is followed by a ``getrlimit`` readback inside the
        forked child, and the observed result is written to a private file. The
        parent therefore reports only limits the kernel confirmed, not limits
        we merely attempted to set. ``RLIMIT_NPROC`` is intentionally absent.
        """
        evidence = self.run_home / f"limits-{uuid.uuid4().hex}.jsonl"
        limits_fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        specs = [
            ("cpu_time", getattr(resource, "RLIMIT_CPU", None), cpu_time_seconds),
            (
                "memory",
                getattr(resource, "RLIMIT_AS", None),
                self.policy.memory_limit_mb * 1024 * 1024,
            ),
            ("open_files", getattr(resource, "RLIMIT_NOFILE", None), self.policy.max_open_files),
            (
                "file_size",
                getattr(resource, "RLIMIT_FSIZE", None),
                self.policy.max_file_size_mb * 1024 * 1024,
            ),
        ]

        def apply() -> None:  # pragma: no cover - runs in the forked child
            for name, resource_id, requested in specs:
                if resource_id is None:
                    line = f"{name}\tunsupported\t\tresource is unavailable on this host\n"
                else:
                    try:
                        resource.setrlimit(resource_id, (requested, requested))
                        observed = resource.getrlimit(resource_id)
                        if observed == (requested, requested):
                            line = (
                                f"{name}\tenforced\trlimit-{name}\t"
                                f"{observed[0]}\t{observed[1]}\n"
                            )
                        else:
                            line = (
                                f"{name}\tunsupported\t\treadback {observed!r} "
                                f"did not match requested {requested}\n"
                            )
                    except (ValueError, OSError) as exc:
                        line = f"{name}\tunsupported\t\t{type(exc).__name__}: {exc}\n"
                try:
                    os.write(limits_fd, line.encode("utf-8", errors="replace"))
                except OSError:
                    pass

        return apply, limits_fd, evidence

    @staticmethod
    def _read_limit_probe(evidence: Path) -> dict[str, LimitEnforcement]:
        try:
            lines = evidence.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {}
        observed: dict[str, LimitEnforcement] = {}
        for line in lines:
            parts = line.split("\t")
            if len(parts) < 2 or parts[0] not in LIMIT_NAMES:
                continue
            observed[parts[0]] = LimitEnforcement(
                status=parts[1],
                mechanism=parts[2] or None,
                detail="\t".join(parts[3:]),
            )
        return observed

    @staticmethod
    def _complete_limits(observed: dict[str, LimitEnforcement]) -> dict[str, LimitEnforcement]:
        limits: dict[str, LimitEnforcement] = {}
        for name in LIMIT_NAMES:
            if name == "wall_time":
                limits[name] = LimitEnforcement(
                    "enforced",
                    "supervisor",
                    "wall clock polled by the parent; the process group is killed on "
                    "timeout or cancellation",
                )
            elif name == "process_count":
                limits[name] = LimitEnforcement(
                    "unsupported",
                    None,
                    "RLIMIT_NPROC is a per-UID ceiling that the kernel does not enforce "
                    "for privileged processes, and its headroom calculation can block "
                    "legitimate children; no cgroup/container backend is available",
                )
            elif name == "cpu_quota":
                limits[name] = LimitEnforcement(
                    "unsupported",
                    None,
                    "a host process has no per-task CPU-concurrency quota; the OCI "
                    "backend reports a verified cgroup cpu.max instead",
                )
            else:
                limits[name] = observed.get(
                    name, LimitEnforcement("unsupported", None, "no runtime evidence recorded")
                )
        return limits

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
            resolved = [*(self._network_prefix or []), *resolved]

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

        # Establish the process-tree scope BEFORE spawning. An explicitly
        # supplied scope that cannot be established fails closed below; an
        # auto-discovered one that cannot be established simply leaves
        # process_count reported as unsupported (never as "enforced").
        scope = self.cgroup_scope
        if scope is None and self.use_cgroup_scope and self.policy.max_processes > 0:
            root = discover_cgroup_root()
            if root is not None:
                candidate = CgroupV2Scope(root, name=f"maya-run-{uuid.uuid4().hex[:12]}")
                try:
                    candidate.create(self.policy.max_processes)
                    scope = candidate
                except Exception:  # noqa: BLE001 - unverifiable scope stays unsupported
                    scope = None

        preexec, limits_fd, evidence = self._limit_probe(
            cpu_time_seconds=max(
                1,
                self.policy.cpu_time_seconds
                if self.policy.cpu_time_seconds is not None
                else int(timeout_seconds) + 5,
            )
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
            if scope is not None and scope is not self.cgroup_scope:
                try:
                    scope.cleanup()
                except Exception:  # noqa: BLE001 - never mask the spawn error
                    pass
            os.close(limits_fd)
            raise PolicyError(f"Could not start command: {exc}") from exc
        finally:
            # The child keeps its own copy; the parent must close its copy so
            # the evidence file is complete when we read it after the wait.
            try:
                os.close(limits_fd)
            except OSError:  # pragma: no cover - already closed on failure
                pass
        if on_start is not None:
            on_start(process.pid, resolved)

        cgroup_active = False
        if scope is not None:
            try:
                if not scope.created:  # an explicit scope is created here
                    scope.create(self.policy.max_processes)
                scope.attach(process.pid)
                cgroup_active = True
            except Exception as exc:  # noqa: BLE001 - fail closed on cgroup setup
                self._kill_group(process.pid)
                process.wait(timeout=5)
                if scope is not self.cgroup_scope:
                    try:
                        scope.cleanup()
                    except Exception:  # noqa: BLE001 - never mask the setup error
                        pass
                raise PolicyError(
                    f"Process-tree limit could not be established: {exc}",
                    hint="Use a rootless container backend or disable the cgroup request.",
                ) from exc

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
        if cgroup_active and scope is not None:
            try:
                scope.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                cleanup_succeeded = False

        for reader in readers:
            reader.join(timeout=5)
        duration_ms = int((time.monotonic() - started) * 1000)
        limits = self._complete_limits(self._read_limit_probe(evidence))
        if cgroup_active:
            limits["process_count"] = LimitEnforcement(
                "enforced",
                "cgroup-v2",
                f"pids.max={self.policy.max_processes}",
                str(self.policy.max_processes),
                str(self.policy.max_processes),
            )
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
            limits_enforcement=summarize_limits(limits),
            limits=limits,
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
        blocked_limits = {
            name: LimitEnforcement("not_applied", None, "command blocked before start")
            for name in LIMIT_NAMES
        }
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
            limits_enforcement=summarize_limits(blocked_limits),
            limits=blocked_limits,
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


__all__ = [
    "CommandPolicy",
    "CommandResult",
    "CommandRunner",
    "DEFAULT_EXECUTABLES",
    "LimitEnforcement",
    "LIMIT_NAMES",
    "summarize_limits",
]

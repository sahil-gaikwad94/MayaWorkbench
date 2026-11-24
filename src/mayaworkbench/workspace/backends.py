"""Execution backends for the worker (WP1).

The control plane asks a backend for capabilities before planning. An isolated
profile is never silently downgraded: if the OCI runtime or cgroup controllers
cannot be verified, :meth:`RootlessContainerBackend.capabilities` reports it and
:meth:`RootlessContainerBackend.provision` raises :class:`RuntimeUnavailable`.

Wire-format note (MW-NEW-P0-01/P0-02 remediation)
--------------------------------------------------
The user's worktree is **never** bind-mounted into a task container. A task gets:

* an immutable, sanitized reference snapshot derived from the pinned worktree;
* a writable overlay populated from that same sanitized snapshot;
* neither tree contains ``.git``, credential paths, credential-shaped content
  or symlinks (see :mod:`mayaworkbench.workspace.sanitize`).

Capability claims are evidence-based. :meth:`verify_capabilities` launches a
real probe container with the *same* flags a task uses and reads the effective
``pids.max``/``memory.max``/``cpu.max``, the effective rlimits, egress denial,
root-filesystem read-onlyness and canary unreadability from inside it. Until
that probe passes, every limit is reported as ``not_applied`` and the profile is
unavailable.

WP1 implements the local trusted backend and a rootless OCI backend. Remote
microVM and user-owned GPU backends are deliberately absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from mayaworkbench.errors import RuntimeUnavailable
from mayaworkbench.workspace import sanitize
from mayaworkbench.workspace.runner import (
    CommandPolicy,
    CommandResult,
    CommandRunner,
    LimitEnforcement,
    summarize_limits,
)

_CGROUP_CONTROLLERS = "/sys/fs/cgroup/cgroup.controllers"
_REQUIRED_CONTROLLERS = frozenset({"pids", "memory", "cpu"})
_PROBE_MARKER = "MAYA_PROBE "
_PROBE_TIMEOUT_SECONDS = 180

#: Generated inside the task overlay (a directory the sanitizer excludes from
#: manifests) and executed inside the container. It signals only processes whose
#: environment carries the session marker, escalates to SIGKILL after a bounded
#: grace period, and never touches PID 1 or unrelated container processes.
_SESSION_KILL_SCRIPT = r'''#!/bin/sh
# MayaWorkbench session terminator (generated per session).
marker="__SESSION__"
scan() {
    for entry in /proc/[0-9]*; do
        pid=${entry#/proc/}
        [ "$pid" = "1" ] && continue
        if tr '\0' '\n' < "$entry/environ" 2>/dev/null | grep -qx "MAYA_SESSION_ID=$marker"; then
            return 0
        fi
    done
    return 1
}
scan && {
    for entry in /proc/[0-9]*; do
        pid=${entry#/proc/}
        [ "$pid" = "1" ] && continue
        if tr '\0' '\n' < "$entry/environ" 2>/dev/null | grep -qx "MAYA_SESSION_ID=$marker"; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
}
i=0
while [ "$i" -lt 20 ]; do
    scan || exit 0
    sleep 0.1
    i=$((i + 1))
done
for entry in /proc/[0-9]*; do
    pid=${entry#/proc/}
    [ "$pid" = "1" ] && continue
    if tr '\0' '\n' < "$entry/environ" 2>/dev/null | grep -qx "MAYA_SESSION_ID=$marker"; then
        kill -KILL "$pid" 2>/dev/null || true
    fi
done
exit 0
'''

#: Limits that must be *positively observed* before the isolated profile may be
#: advertised as available. File-size and open-files limits are additionally
#: enforced through ``--ulimit`` but are not release-blocking on their own.
MANDATORY_LIMITS = ("process_count", "memory", "cpu_quota", "cpu_time")

#: Runs inside the probe container. It performs readbacks and bounded active
#: tests, and prints exactly one machine-readable line. Nothing here is trusted
#: unless it comes back with the marker; a missing marker is a failed probe.
_PROBE_SCRIPT = r'''
import json
import os
import resource
import socket
import sys
import time

CANARY = sys.argv[1] if len(sys.argv) > 1 else "/nonexistent/maya-host-canary"


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "denied"
    except OSError as exc:
        return "error:" + type(exc).__name__


def cgroup(name):
    return read_text("/sys/fs/cgroup/" + name)


def egress():
    reachable = False
    for host, port in (("203.0.113.1", 80), ("1.1.1.1", 53)):
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.settimeout(1.0)
            connection.connect((host, port))
            connection.close()
            reachable = True
            break
        except OSError:
            continue
    return "reachable" if reachable else "denied"


def root_write():
    try:
        with open("/maya-probe-write", "w", encoding="utf-8") as handle:
            handle.write("x")
        os.unlink("/maya-probe-write")
        return "writable"
    except OSError as exc:
        return "denied:" + type(exc).__name__


def pids_probe(limit):
    children = []
    try:
        for _ in range(max(2, min(limit + 4, 64))):
            pid = os.fork()
            if pid == 0:
                time.sleep(1.5)
                os._exit(0)
            children.append(pid)
    except OSError:
        pass
    finally:
        for pid in children:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
    return len(children)


def cpu_time_probe(budget):
    """Burn CPU in a child that inherits RLIMIT_CPU; report how it died."""
    if not budget or budget > 5:
        return "not_run"
    pid = os.fork()
    if pid == 0:
        try:
            while True:
                pass
        finally:
            os._exit(0)
    deadline = time.monotonic() + max(10, budget * 4)
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            if os.WIFSIGNALED(status):
                return "signal:" + str(os.WTERMSIG(status))
            return "exit:" + str(os.WEXITSTATUS(status))
        time.sleep(0.05)
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass
    return "not_limited"


def rlimit(name):
    try:
        return list(resource.getrlimit(getattr(resource, name)))
    except (AttributeError, OSError, ValueError):
        return None


payload = {
    "pids_max": cgroup("pids.max"),
    "memory_max": cgroup("memory.max"),
    "cpu_max": cgroup("cpu.max"),
    "pids_children": pids_probe(int(sys.argv[2]) if len(sys.argv) > 2 else 8),
    "cpu_time_child": cpu_time_probe(int(sys.argv[3]) if len(sys.argv) > 3 else 0),
    "rlimit_cpu": rlimit("RLIMIT_CPU"),
    "rlimit_fsize": rlimit("RLIMIT_FSIZE"),
    "rlimit_nofile": rlimit("RLIMIT_NOFILE"),
    "network_egress": egress(),
    "work_secret_read": read_text("/work/.env"),
    "source_secret_read": read_text("/src/.env"),
    "host_canary_read": read_text(CANARY),
    "root_write": root_write(),
    "uid": os.getuid() if hasattr(os, "getuid") else None,
}
print("MAYA_PROBE " + json.dumps(payload, sort_keys=True))
'''


@dataclass(frozen=True)
class RuntimeCapabilities:
    available: bool
    reasons: tuple[str, ...]
    limits: dict[str, LimitEnforcement]
    network_isolation: str
    evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "reasons": list(self.reasons),
            "limits": {name: item.as_dict() for name, item in sorted(self.limits.items())},
            "network_isolation": self.network_isolation,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class RuntimeRef:
    id: str
    backend: str
    source: Path
    overlay: Path
    image: str
    container_name: str
    #: Immutable sanitized reference tree (the only source-derived mount).
    snapshot: Path | None = None
    #: Digest of the mount/flag policy, recorded as evidence.
    mount_policy: str = ""

    def as_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "backend": self.backend,
            "source": str(self.source),
            "overlay": str(self.overlay),
            "image": self.image,
            "container_name": self.container_name,
            "snapshot": str(self.snapshot) if self.snapshot else None,
            "mount_policy": self.mount_policy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeRef":
        snapshot = data.get("snapshot")
        return cls(
            id=data["id"],
            backend=data["backend"],
            source=Path(data["source"]),
            overlay=Path(data["overlay"]),
            image=data["image"],
            container_name=data["container_name"],
            snapshot=Path(snapshot) if snapshot else None,
            mount_policy=str(data.get("mount_policy") or ""),
        )


@dataclass(frozen=True)
class ContainerEvidence:
    """What a live probe actually observed in a task-equivalent container."""

    probe_status: str
    tool: str
    image: str
    mount_policy_sha256: str
    limits: dict[str, LimitEnforcement]
    network_egress: str
    source_secret_read: str
    work_secret_read: str
    host_canary_read: str
    root_filesystem: str
    cleanup: str
    detail: str = ""
    active: dict[str, Any] = field(default_factory=dict)

    @property
    def mandatory_failures(self) -> list[str]:
        failures: list[str] = []
        if self.probe_status != "passed":
            failures.append(f"the probe did not pass ({self.probe_status}): {self.detail}")
        for name in MANDATORY_LIMITS:
            item = self.limits.get(name)
            if item is None or item.status != "enforced":
                observed = item.observed if item else None
                failures.append(f"{name} is not enforced (observed {observed!r})")
        if self.network_egress != "denied":
            failures.append(f"network egress probe returned {self.network_egress!r}")
        for label, value in (
            ("the sanitized source mount", self.source_secret_read),
            ("the writable overlay", self.work_secret_read),
            ("a host canary path", self.host_canary_read),
        ):
            if value not in {"absent", "denied"}:
                failures.append(f"a secret was readable through {label}: {value!r}")
        if not self.root_filesystem.startswith("denied"):
            failures.append(f"the root filesystem is {self.root_filesystem!r}, not read-only")
        if self.cleanup != "passed":
            failures.append(f"container cleanup {self.cleanup!r}")
        return failures

    @property
    def available(self) -> bool:
        return not self.mandatory_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_status": self.probe_status,
            "tool": self.tool,
            "image": self.image,
            "mount_policy_sha256": self.mount_policy_sha256,
            "limits": {name: item.as_dict() for name, item in sorted(self.limits.items())},
            "network_egress": self.network_egress,
            "source_secret_read": self.source_secret_read,
            "work_secret_read": self.work_secret_read,
            "host_canary_read": self.host_canary_read,
            "root_filesystem": self.root_filesystem,
            "cleanup": self.cleanup,
            "detail": self.detail,
            "active": dict(self.active),
            "available": self.available,
            "mandatory_failures": self.mandatory_failures,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContainerEvidence":
        return cls(
            probe_status=str(data.get("probe_status", "not_run")),
            tool=str(data.get("tool", "")),
            image=str(data.get("image", "")),
            mount_policy_sha256=str(data.get("mount_policy_sha256", "")),
            limits={
                name: LimitEnforcement(
                    str(item.get("status", "not_applied")),
                    item.get("mechanism"),
                    str(item.get("detail") or ""),
                    item.get("requested"),
                    item.get("observed"),
                )
                for name, item in (data.get("limits") or {}).items()
            },
            network_egress=str(data.get("network_egress", "unprobed")),
            source_secret_read=str(data.get("source_secret_read", "unprobed")),
            work_secret_read=str(data.get("work_secret_read", "unprobed")),
            host_canary_read=str(data.get("host_canary_read", "unprobed")),
            root_filesystem=str(data.get("root_filesystem", "unprobed")),
            cleanup=str(data.get("cleanup", "unprobed")),
            detail=str(data.get("detail", "")),
            active=dict(data.get("active") or {}),
        )


class ExecutionBackend(Protocol):
    def capabilities(self) -> RuntimeCapabilities: ...

    def provision(self) -> RuntimeRef: ...

    def exec(
        self,
        runtime: RuntimeRef,
        argv: list[str],
        *,
        timeout: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CommandResult: ...

    def stop(self, runtime: RuntimeRef, *, reason: str, keep_overlay: bool = False) -> None: ...

    def collect_artifacts(self, runtime: RuntimeRef) -> list[Path]: ...


def _cgroup_controllers() -> tuple[set[str], str | None]:
    try:
        with open(_CGROUP_CONTROLLERS, encoding="ascii") as handle:
            return set(handle.read().split()), None
    except OSError as exc:
        return set(), f"cgroup v2 not available: {exc}"


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TrustedLocalBackend:
    """The existing bounded local runner, labelled as LOCAL_TRUSTED."""

    backend_id = "trusted-local"

    def __init__(self, workspace: Path, run_home: Path, policy: CommandPolicy):
        self.workspace = Path(workspace).resolve()
        self.run_home = Path(run_home).resolve()
        self.policy = policy
        self._runner = CommandRunner(self.workspace, policy=policy, run_home=self.run_home)

    def capabilities(self) -> RuntimeCapabilities:
        limits = {
            "wall_time": LimitEnforcement("enforced", "supervisor"),
            "cpu_time": LimitEnforcement(
                "unsupported", None, "verified per run by a getrlimit readback"
            ),
            "cpu_quota": LimitEnforcement(
                "unsupported",
                None,
                "a host process has no per-task CPU-concurrency quota; use the "
                "rootless container backend for a verified cgroup cpu.max",
            ),
            "memory": LimitEnforcement(
                "unsupported", None, "verified per run by a getrlimit readback"
            ),
            "process_count": LimitEnforcement(
                "unsupported", None, "no per-task process-tree primitive"
            ),
            "file_size": LimitEnforcement(
                "unsupported", None, "verified per run by a getrlimit readback"
            ),
            "open_files": LimitEnforcement(
                "unsupported", None, "verified per run by a getrlimit readback"
            ),
        }
        return RuntimeCapabilities(
            True, ("host user privileges; NOT a sandbox",), limits, "not_requested"
        )

    def provision(self) -> RuntimeRef:
        runtime_id = uuid.uuid4().hex
        overlay = self.run_home / "overlay" / runtime_id
        overlay.mkdir(parents=True, exist_ok=True, mode=0o700)
        return RuntimeRef(
            id=runtime_id,
            backend=self.backend_id,
            source=self.workspace,
            overlay=overlay,
            image="host",
            container_name="",
            mount_policy=_digest({"mounts": [], "boundary": "host-user"}),
        )

    def exec(
        self,
        runtime: RuntimeRef,
        argv: list[str],
        *,
        timeout: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CommandResult:
        return self._runner.run(
            argv, timeout_seconds=timeout, cancel_requested=cancel_requested
        )

    def stop(  # pragma: no cover - local runner self-cleans
        self, runtime: RuntimeRef, *, reason: str, keep_overlay: bool = False
    ) -> None:
        del runtime, reason, keep_overlay

    def collect_artifacts(self, runtime: RuntimeRef) -> list[Path]:
        return (
            [path for path in runtime.overlay.rglob("*") if path.is_file()]
            if runtime.overlay.exists()
            else []
        )


class RootlessContainerBackend:
    """Disposable rootless OCI workspace over a sanitized snapshot.

    The container sees exactly two things: a read-only root filesystem and a
    writable overlay at ``/work`` derived from the sanitized snapshot. There is
    no mount of the user's worktree at ``/src`` (or anywhere else), so a path
    excluded from ``/work`` cannot be reached through a second mount.
    """

    backend_id = "rootless-container"

    def __init__(
        self,
        workspace: Path,
        run_home: Path,
        policy: CommandPolicy,
        *,
        image: str = "python:3.12-slim",
        tool: str | None = None,
        allow_docker: bool = False,
        evidence: ContainerEvidence | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.run_home = Path(run_home).resolve()
        self.policy = policy
        self.image = image
        self._tool_override = tool
        self._allow_docker = allow_docker
        # Evidence from a live probe. Never inferred from controller names.
        self._evidence = evidence
        # Runtimes whose retained session container this backend started, so a
        # task that never opened a session issues no session-container calls.
        self._session_containers: set[str] = set()

    # -------------------------------------------------------------- capability
    def _detect_tool(self) -> tuple[str | None, str | None]:
        if self._tool_override:
            path = shutil.which(self._tool_override)
            if path:
                return path, None
            return None, f"OCI runtime {self._tool_override!r} was not found on PATH"
        podman = shutil.which("podman")
        if podman:
            return podman, None
        docker = shutil.which("docker")
        if docker and self._allow_docker:
            return docker, "docker requires explicit rootless verification by the operator"
        return None, "no rootless OCI runtime found (podman preferred; docker needs opt-in)"

    def prerequisites(self) -> tuple[bool, list[str]]:
        """Cheap checks: an OCI client and the controllers a task will use."""

        reasons: list[str] = []
        tool, warning = self._detect_tool()
        if tool is None:
            reasons.append(warning or "OCI runtime unavailable")
        elif warning:
            reasons.append(warning)
        controllers, error = _cgroup_controllers()
        if error:
            reasons.append(error)
        missing = sorted(_REQUIRED_CONTROLLERS - controllers)
        if missing:
            reasons.append(f"missing cgroup controllers: {', '.join(missing)}")
        return not reasons, reasons

    def mount_policy_document(self) -> dict[str, Any]:
        return {
            "mounts": [{"dst": "/work", "mode": "rw", "kind": "sanitized-overlay"}],
            "source_mount": None,
            "root_filesystem": "read-only",
            "tmpfs": ["/tmp"],
            "network": "none",
            "capabilities": "dropped:ALL",
            "no_new_privileges": True,
            "user": "host-uid:gid",
            "env": "allowlist",
        }

    def limit_policy_document(self) -> dict[str, Any]:
        return {
            "pids": self.policy.max_processes,
            "memory_bytes": self.policy.memory_limit_mb * 1024 * 1024,
            "cpu_quota": self._cpu_quota(),
            "cpu_time_seconds": self.policy.cpu_time_seconds,
            "file_size_bytes": self.policy.max_file_size_mb * 1024 * 1024,
            "open_files": self.policy.max_open_files,
        }

    def _cpu_quota(self) -> float:
        return max(0.1, float(self.policy.cpu_quota))

    def limit_policy_sha256(self) -> str:
        """Digest of the requested resource policy (parity evidence)."""

        return _digest(self.limit_policy_document())

    def _cpu_time_seconds(self, timeout: int | None) -> int:
        if self.policy.cpu_time_seconds is not None:
            return max(1, self.policy.cpu_time_seconds)
        return max(1, int(timeout or self.policy.max_runtime_seconds) + 5)

    def capabilities(self) -> RuntimeCapabilities:
        ok, reasons = self.prerequisites()
        evidence = self._evidence
        if evidence is None:
            limits = {
                "wall_time": LimitEnforcement("enforced", "supervisor"),
                **{
                    name: LimitEnforcement(
                        "not_applied",
                        None,
                        "no live probe has verified this control in a task container",
                    )
                    for name in (
                        "cpu_time",
                        "cpu_quota",
                        "memory",
                        "process_count",
                        "file_size",
                        "open_files",
                    )
                },
            }
            return RuntimeCapabilities(
                False,
                tuple(reasons)
                + (
                    "rootless isolation has not been verified by a live probe in a "
                    "task-equivalent container",
                ),
                limits,
                "unverified",
                None,
            )
        limits = dict(evidence.limits)
        limits.setdefault("wall_time", LimitEnforcement("enforced", "supervisor"))
        failures = evidence.mandatory_failures
        available = ok and not failures
        return RuntimeCapabilities(
            available,
            tuple(failures) if failures else (
                "rootless OCI isolation verified by a live probe in a task-equivalent "
                "container",
            ),
            limits,
            "none" if available else "unverified",
            evidence.as_dict(),
        )

    # -------------------------------------------------------------- lifecycle
    def _materialize(self, source: Path) -> RuntimeRef:
        """Create the immutable sanitized snapshot and the writable overlay."""

        runtime_id = uuid.uuid4().hex
        root = self.run_home / "containers" / runtime_id
        snapshot_dir = root / "source"
        overlay = root / "work"
        try:
            snapshot = sanitize.build_tree(source, snapshot_dir, read_only=True)
            candidate = sanitize.build_tree(snapshot_dir, overlay, read_only=False)
        except Exception as exc:
            sanitize.remove_tree(root)
            raise RuntimeUnavailable(
                f"ROOTLESS_CONTAINER task tree could not be sanitized: {exc}"
            ) from exc
        if candidate.manifest != snapshot.manifest:
            sanitize.remove_tree(root)
            raise RuntimeUnavailable(
                "the writable task overlay does not match the sanitized snapshot manifest"
            )
        return RuntimeRef(
            id=runtime_id,
            backend=self.backend_id,
            source=Path(source).resolve(),
            overlay=overlay,
            image=self.image,
            container_name=f"maya-{runtime_id[:16]}",
            snapshot=snapshot_dir,
            mount_policy=_digest(self.mount_policy_document()),
        )

    def sanitized_manifest(self, runtime: RuntimeRef) -> dict[str, str]:
        return sanitize.tree_manifest(runtime.snapshot or runtime.overlay)

    def provision(self) -> RuntimeRef:
        capabilities = self.capabilities()
        if not capabilities.available:
            raise RuntimeUnavailable(
                "ROOTLESS_CONTAINER runtime cannot be established",
                hint="; ".join(capabilities.reasons)
                + ". The task is blocked, not downgraded to host execution.",
            )
        return self._materialize(self.workspace)

    def _hardening_flags(
        self, runtime: RuntimeRef, *, timeout: int | None
    ) -> list[str]:
        """Security and resource flags shared by task and session containers.

        Both container kinds must be identical in the properties that matter:
        the same sanitized-only mount, the same dropped capabilities, the same
        network namespace, the same cgroup/rlimit policy. Only longevity and
        entrypoint differ.
        """

        env_flags: list[str] = []
        # Never forward the host environment: it can embed provider keys, cloud
        # credentials and home paths the container must not see.
        env_flags += ["-e", "PATH=/usr/local/bin:/usr/bin:/bin"]
        env_flags += ["-e", "HOME=/work", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        env_flags += ["-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8"]
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        file_size_bytes = self.policy.max_file_size_mb * 1024 * 1024
        cpu_time = self._cpu_time_seconds(timeout)
        return [
            "--user",
            f"{uid}:{gid}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m,nosuid,nodev",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            "none",
            "--pids-limit",
            str(self.policy.max_processes),
            "--memory",
            f"{self.policy.memory_limit_mb}m",
            # No swap headroom: memory.max is then the real ceiling.
            "--memory-swap",
            f"{self.policy.memory_limit_mb}m",
            "--cpus",
            f"{self._cpu_quota():g}",
            "--ulimit",
            f"cpu={cpu_time}:{cpu_time}",
            "--ulimit",
            f"fsize={file_size_bytes}:{file_size_bytes}",
            "--ulimit",
            f"nofile={self.policy.max_open_files}:{self.policy.max_open_files}",
            # The ONLY source-derived mount is the sanitized, writable overlay.
            "--mount",
            f"type=bind,src={runtime.overlay},dst=/work",
            "-w",
            "/work",
            *env_flags,
        ]

    def _oci_argv(
        self, runtime: RuntimeRef, argv: list[str], *, timeout: int | None = None
    ) -> list[str]:
        tool, _ = self._detect_tool()
        assert tool is not None  # guarded by provision()/verify_capabilities()
        return [
            tool,
            "run",
            "--rm",
            "--name",
            runtime.container_name,
            *self._hardening_flags(runtime, timeout=timeout),
            runtime.image,
            *argv,
        ]

    def _run_oci(
        self,
        runtime: RuntimeRef,
        argv: list[str],
        *,
        timeout: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CommandResult:
        return self._run_client(
            runtime,
            self._oci_argv(runtime, argv, timeout=timeout),
            timeout=timeout,
            cancel_requested=cancel_requested,
        )

    def _run_client(
        self,
        runtime: RuntimeRef,
        oci_argv: list[str],
        *,
        timeout: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CommandResult:
        # The OCI client is a trusted, allowlisted executable. The policy that
        # permits absolute arguments is scoped to this client; the project argv
        # inside the container is never parsed by that policy.
        policy = CommandPolicy(
            profile="CPU_SMOKE",
            trust_profile="LOCAL_TRUSTED",
            allow_absolute_args=True,
            # The probe/invariant script is one Maya-generated argument; the
            # default limits exist to reject hostile model input, not this.
            allow_multiline_args=True,
            max_arg_length=65_536,
            max_total_arg_bytes=262_144,
            # The container itself enforces `--network none`; applying the host
            # netns probe here would block runs on hosts without netns support.
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            allowed_executables=(Path(oci_argv[0]).name,),
            max_runtime_seconds=max(timeout, self.policy.max_runtime_seconds),
            max_output_bytes=self.policy.max_output_bytes,
        )
        runner = CommandRunner(
            runtime.overlay.parent, policy=policy, run_home=runtime.overlay.parent
        )
        return runner.run(
            oci_argv, timeout_seconds=timeout, cancel_requested=cancel_requested
        )

    def exec(
        self,
        runtime: RuntimeRef,
        argv: list[str],
        *,
        timeout: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CommandResult:
        observed = self._run_oci(
            runtime, argv, timeout=timeout, cancel_requested=cancel_requested
        )
        capabilities = self.capabilities()
        limits = dict(capabilities.limits)
        return CommandResult(
            argv=argv,
            exit_code=observed.exit_code,
            stdout=observed.stdout,
            stderr=observed.stderr,
            duration_ms=observed.duration_ms,
            timed_out=observed.timed_out,
            output_truncated=observed.output_truncated,
            cancelled=observed.cancelled,
            blocked=observed.blocked,
            blocked_reason=observed.blocked_reason,
            network_isolated=True,
            network_enforcement="oci-network-none",
            limits_enforcement=summarize_limits(limits),
            limits=limits,
            cleanup_required=observed.cleanup_required,
            cleanup_succeeded=observed.cleanup_succeeded,
            env_names=sorted(
                {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE"}
            ),
            cwd=str(runtime.overlay),
        )

    # ------------------------------------------------------- container sessions
    #: Suffix of the retained container that owns persistent sessions. It is a
    #: separate container from the per-command ``--rm`` one, because a session
    #: must outlive the command that started it.
    SESSION_SUFFIX = "-sess"
    #: Marker carried by every exec'd session process, used by the generated
    #: terminator to signal exactly one session's processes.
    SESSION_MARKER = "MAYA_SESSION_ID"

    def session_container_name(self, runtime: RuntimeRef) -> str:
        return f"{runtime.container_name[:48]}{self.SESSION_SUFFIX}"

    def _session_container_argv(
        self, runtime: RuntimeRef, *, timeout: int | None
    ) -> list[str]:
        tool, _ = self._detect_tool()
        assert tool is not None
        return [
            tool,
            "run",
            "--detach",
            "--name",
            self.session_container_name(runtime),
            # Identical hardening to a task container: same mount policy, same
            # cgroup/rlimit policy, same dropped capabilities, same network.
            *self._hardening_flags(runtime, timeout=timeout),
            "--entrypoint",
            "/bin/sh",
            runtime.image,
            "-c",
            "trap 'exit 0' TERM INT; while :; do sleep 30; done",
        ]

    def start_session_container(self, runtime: RuntimeRef, *, timeout: int = 60) -> str:
        """Start the retained container that owns this task's sessions.

        Raises :class:`RuntimeUnavailable` when the client cannot start it; a
        caller must never fall back to a host process.
        """

        result = self._run_client(
            runtime,
            self._session_container_argv(runtime, timeout=timeout),
            timeout=timeout + 30,
        )
        if result.exit_code != 0 or result.timed_out:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeUnavailable(
                "the task session container could not be started",
                hint=detail[-400:] or "the OCI client returned a failure",
            )
        self._session_containers.add(runtime.id)
        return self.session_container_name(runtime)

    def session_container_running(self, runtime: RuntimeRef) -> bool:
        tool, _ = self._detect_tool()
        if tool is None:  # pragma: no cover - guarded by provision()
            return False
        result = self._run_client(
            runtime,
            [
                tool,
                "inspect",
                "--format",
                "{{.State.Running}}",
                self.session_container_name(runtime),
            ],
            timeout=30,
        )
        return result.exit_code == 0 and result.stdout.strip().lower() == "true"

    def session_exec_argv(
        self, runtime: RuntimeRef, argv: list[str], *, session_id: str
    ) -> list[str]:
        """Client argv that executes a session command inside the container."""

        tool, _ = self._detect_tool()
        assert tool is not None
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        return [
            tool,
            "exec",
            "-i",
            "--user",
            f"{uid}:{gid}",
            "--env",
            f"{self.SESSION_MARKER}={session_id}",
            "-w",
            "/work",
            self.session_container_name(runtime),
            *argv,
        ]

    def session_kill_script(self, runtime: RuntimeRef, *, session_id: str) -> Path:
        directory = runtime.overlay / ".maya-sessions"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        script = directory / f"{session_id}.kill"
        script.write_text(
            _SESSION_KILL_SCRIPT.replace("__SESSION__", session_id), encoding="utf-8"
        )
        script.chmod(0o700)
        return script

    def session_kill(self, runtime: RuntimeRef, *, session_id: str) -> bool:
        """Terminate one session's processes inside the container.

        Returns ``False`` when the container is already gone or the client
        failed; the caller still kills the host-side client afterwards.
        """

        tool, _ = self._detect_tool()
        if tool is None:  # pragma: no cover - guarded by provision()
            return False
        script = self.session_kill_script(runtime, session_id=session_id)
        try:
            result = self._run_client(
                runtime,
                [
                    tool,
                    "exec",
                    self.session_container_name(runtime),
                    "/bin/sh",
                    f"/work/.maya-sessions/{script.name}",
                ],
                timeout=30,
            )
        except Exception:  # noqa: BLE001 - the container may already be gone
            return False
        return result.exit_code == 0

    def stop_session_container(self, runtime: RuntimeRef) -> bool:
        """Stop and remove the session container; safe when it never started."""

        tool, _ = self._detect_tool()
        if tool is None:  # pragma: no cover - guarded by provision()
            return False
        if runtime.id not in self._session_containers:
            return False
        self._session_containers.discard(runtime.id)
        name = self.session_container_name(runtime)
        succeeded = False
        for argv in ([tool, "stop", "--time", "2", name], [tool, "rm", "-f", name]):
            try:
                result = self._run_client(runtime, argv, timeout=30)
            except Exception:  # noqa: BLE001 - cleanup must not mask the failure
                continue
            succeeded = succeeded or result.exit_code == 0
        return succeeded

    # ------------------------------------------------------------ verification
    def verify_capabilities(self) -> ContainerEvidence:
        """Probe a real task-equivalent container and record what it observed.

        A task-shaped probe workspace (with a secret path and a
        credential-shaped file) is sanitized exactly like a real task, then the
        probe container runs with the *same* flags as a task. Every claim in the
        returned evidence is a value read back from inside that container.
        """

        if self._evidence is not None:
            return self._evidence
        ok, reasons = self.prerequisites()
        if not ok:
            evidence = ContainerEvidence(
                probe_status="failed",
                tool=self._detect_tool()[0] or "",
                image=self.image,
                mount_policy_sha256=_digest(self.mount_policy_document()),
                limits={},
                network_egress="unprobed",
                source_secret_read="unprobed",
                work_secret_read="unprobed",
                host_canary_read="unprobed",
                root_filesystem="unprobed",
                cleanup="unprobed",
                detail="; ".join(reasons),
            )
            self._evidence = evidence
            return evidence

        probe_id = uuid.uuid4().hex
        probe_root = self.run_home / "capability-probe" / probe_id
        source = probe_root / "source"
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        (source / "app.py").write_text("print('probe')\n", encoding="utf-8")
        # Excluded by path: must be absent from /work.
        (source / ".env").write_text("MAYA_MODEL_API_KEY=sk-probe-canary\n", encoding="utf-8")
        # Excluded by content: also must be absent from /work.
        (source / "notes.txt").write_text(
            "sk-probecontentcanary0000000000000000\n", encoding="utf-8"
        )
        canary = probe_root / "host-canary.txt"
        canary.write_text("HOST-CANARY", encoding="utf-8")

        runtime = self._materialize(source)
        cleanup = "failed"
        detail = ""
        observed: dict[str, Any] = {}
        try:
            timeout = _PROBE_TIMEOUT_SECONDS
            probe_argv = [
                "python",
                "-c",
                _PROBE_SCRIPT,
                str(canary),
                str(min(self.policy.max_processes, 64)),
                str(self._cpu_time_seconds(timeout)),
            ]
            result = self._run_oci(runtime, probe_argv, timeout=timeout)
            marker = None
            for line in result.stdout.splitlines():
                if line.startswith(_PROBE_MARKER):
                    try:
                        marker = json.loads(line[len(_PROBE_MARKER) :])
                    except json.JSONDecodeError:
                        marker = None
            if marker is None:
                detail = (
                    "the probe printed no machine-readable evidence (exit "
                    f"{result.exit_code}): {(result.stderr or result.stdout).strip()[:400]}"
                )
            else:
                observed = marker
                detail = "probe completed"
        except Exception as exc:  # noqa: BLE001 - a probe failure is evidence, not a crash
            detail = f"{type(exc).__name__}: {exc}"[:600]
        finally:
            try:
                self.stop(runtime, reason="capability probe complete")
                cleanup = "passed"
            except Exception as exc:  # noqa: BLE001
                cleanup = f"failed: {exc}"
            sanitize.remove_tree(probe_root)

        evidence = self._evidence_from_observation(
            observed if observed else None, detail=detail, cleanup=cleanup
        )
        self._evidence = evidence
        return evidence

    def _evidence_from_observation(
        self, observed: dict[str, Any] | None, *, detail: str, cleanup: str
    ) -> ContainerEvidence:
        mount_policy = _digest(self.mount_policy_document())
        base = {
            "tool": self._detect_tool()[0] or "",
            "image": self.image,
            "mount_policy_sha256": mount_policy,
        }
        if observed is None:
            return ContainerEvidence(
                probe_status="failed",
                **base,
                limits={},
                network_egress="unprobed",
                source_secret_read="unprobed",
                work_secret_read="unprobed",
                host_canary_read="unprobed",
                root_filesystem="unprobed",
                cleanup=cleanup,
                detail=detail,
            )

        memory_bytes = self.policy.memory_limit_mb * 1024 * 1024
        file_size_bytes = self.policy.max_file_size_mb * 1024 * 1024
        cpu_quota = self._cpu_quota()
        expected_cpu_max = (
            f"{int(round(cpu_quota * 100000))} 100000"
        )
        cpu_time = self._cpu_time_seconds(_PROBE_TIMEOUT_SECONDS)

        limits: dict[str, LimitEnforcement] = {}
        limits["wall_time"] = LimitEnforcement("enforced", "supervisor", "parent deadline")
        limits["process_count"] = _compare(
            "process_count",
            self.policy.max_processes,
            observed.get("pids_max"),
            "cgroup-v2:pids.max",
        )
        limits["memory"] = _compare(
            "memory", memory_bytes, observed.get("memory_max"), "cgroup-v2:memory.max"
        )
        limits["cpu_quota"] = _compare(
            "cpu_quota", expected_cpu_max, observed.get("cpu_max"), "cgroup-v2:cpu.max"
        )
        limits["cpu_time"] = _compare_rlimit(
            "cpu_time", cpu_time, observed.get("rlimit_cpu"), "rlimit:RLIMIT_CPU"
        )
        limits["file_size"] = _compare_rlimit(
            "file_size",
            file_size_bytes,
            observed.get("rlimit_fsize"),
            "rlimit:RLIMIT_FSIZE",
        )
        limits["open_files"] = _compare_rlimit(
            "open_files",
            self.policy.max_open_files,
            observed.get("rlimit_nofile"),
            "rlimit:RLIMIT_NOFILE",
        )

        # A finite pids.max that the kernel honours must also cap a fork storm.
        forked = observed.get("pids_children")
        if isinstance(forked, int) and forked > self.policy.max_processes:
            limits["process_count"] = LimitEnforcement(
                "not_applied",
                None,
                f"pids.max read back as {observed.get('pids_max')!r} but {forked} "
                "children could still be forked",
                str(self.policy.max_processes),
                observed.get("pids_max"),
            )

        # A CPU-time budget is only proven by a child that actually hits it.
        cpu_child = str(observed.get("cpu_time_child") or "not_run")
        if cpu_child.startswith("signal:"):
            limits["cpu_time"] = LimitEnforcement(
                "enforced",
                "rlimit:RLIMIT_CPU",
                f"a CPU-bound child was terminated by signal {cpu_child.split(':', 1)[1]}",
                str(cpu_time),
                cpu_child,
            )
        elif cpu_child == "not_limited" and cpu_time <= 5:
            limits["cpu_time"] = LimitEnforcement(
                "not_applied",
                None,
                "a CPU-bound child exceeded the declared CPU-time budget",
                str(cpu_time),
                cpu_child,
            )

        return ContainerEvidence(
            probe_status="passed",
            **base,
            limits=limits,
            network_egress=str(observed.get("network_egress", "unprobed")),
            source_secret_read=str(observed.get("source_secret_read", "unprobed")),
            work_secret_read=str(observed.get("work_secret_read", "unprobed")),
            host_canary_read=str(observed.get("host_canary_read", "unprobed")),
            root_filesystem=str(observed.get("root_write", "unprobed")),
            cleanup=cleanup,
            detail=detail,
            active={
                "pids_children": observed.get("pids_children"),
                "cpu_time_child": observed.get("cpu_time_child"),
                "uid": observed.get("uid"),
                "limit_policy_sha256": _digest(self.limit_policy_document()),
            },
        )

    # ---------------------------------------------------------------- cleanup
    def stop(
        self, runtime: RuntimeRef, *, reason: str, keep_overlay: bool = False
    ) -> None:
        del reason
        # Sessions keep a second container alive; it must go first so the
        # overlay is not removed underneath a running exec.
        try:
            self.stop_session_container(runtime)
        except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
            pass
        tool, _ = self._detect_tool()
        if tool:
            root = runtime.overlay.parent
            try:
                if root.exists():
                    # Same client path as every other invocation, so the removal
                    # is recorded as evidence instead of happening invisibly.
                    self._run_client(
                        runtime, [tool, "rm", "-f", runtime.container_name], timeout=10
                    )
                else:  # pragma: no cover - interrupted run removed the tree
                    subprocess.run(
                        [tool, "rm", "-f", runtime.container_name],
                        cwd=str(self.run_home),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
            except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
                pass
        root = runtime.overlay.parent
        if root.exists() and not keep_overlay:
            sanitize.remove_tree(root)
        if not keep_overlay and runtime.snapshot and runtime.snapshot.exists():
            sanitize.remove_tree(runtime.snapshot)

    def collect_artifacts(self, runtime: RuntimeRef) -> list[Path]:
        return (
            [path for path in runtime.overlay.rglob("*") if path.is_file()]
            if runtime.overlay.exists()
            else []
        )


def _compare(
    name: str, requested: Any, observed: Any, mechanism: str
) -> LimitEnforcement:
    """Turn a readback into per-limit evidence; never infer a pass."""

    del name
    if observed is None:
        return LimitEnforcement(
            "unsupported",
            None,
            "the probe did not report this value",
            str(requested),
            None,
        )
    text = str(observed).strip()
    if text in {"absent", "denied"} or text.startswith("error:"):
        return LimitEnforcement(
            "unsupported",
            None,
            f"the effective value could not be read inside the container ({text})",
            str(requested),
            text,
        )
    if text == str(requested):
        return LimitEnforcement(
            "enforced",
            mechanism,
            "read back inside the task container",
            str(requested),
            text,
        )
    return LimitEnforcement(
        "not_applied",
        None,
        f"the kernel reported {text!r} but {requested!r} was requested",
        str(requested),
        text,
    )


def _compare_rlimit(
    name: str, requested: int, observed: Any, mechanism: str
) -> LimitEnforcement:
    del name
    if not isinstance(observed, (list, tuple)) or len(observed) != 2:
        return LimitEnforcement(
            "unsupported",
            None,
            "the probe did not report this rlimit",
            str(requested),
            None if observed is None else str(observed),
        )
    soft, hard = observed
    if soft == requested and hard == requested:
        return LimitEnforcement(
            "enforced",
            mechanism,
            "soft and hard limits read back inside the task container",
            str(requested),
            f"{soft}:{hard}",
        )
    return LimitEnforcement(
        "not_applied",
        None,
        f"read back {soft}:{hard} but {requested}:{requested} was requested",
        str(requested),
        f"{soft}:{hard}",
    )


class BackendCommandRunner:
    """Adapter exposing an isolated backend as the runner interface workers use.

    Provisioning happens on first use, so a contract that never executes a
    command never creates a container. ``stop`` is idempotent.
    """

    def __init__(self, backend: ExecutionBackend, runtime: RuntimeRef | None = None):
        self.backend = backend
        # A RuntimeWorkspace can provision once and hand the same RuntimeRef to
        # every component; provisioning must not happen twice for one task.
        self._runtime = runtime

    @property
    def policy(self) -> CommandPolicy:  # pragma: no cover - evidence consumers only
        return getattr(
            self.backend,
            "policy",
            CommandPolicy(
                profile="CPU_SMOKE",
                network_policy="DENY_BY_DEFAULT",
                dependency_policy="DENY",
            ),
        )

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        on_start: Callable[[int, list[str]], None] | None = None,
    ) -> CommandResult:
        del on_start  # the OCI client pid is not the project process pid
        if self._runtime is None:
            self._runtime = self.backend.provision()
        return self.backend.exec(
            self._runtime,
            argv,
            timeout=int(timeout_seconds),
            cancel_requested=cancel_requested,
        )

    def run_script(
        self,
        script: str,
        *,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        on_start: Callable[[int, list[str]], None] | None = None,
    ) -> CommandResult:
        """Run a generated analysis script inside the container.

        The script is hashed and written to the task overlay, then executed by
        the container's interpreter. It is system-generated from validated
        parameters (never model text), and it stays inside the same isolation
        boundary as every other command.
        """

        del on_start
        fingerprint = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if self._runtime is None:
            self._runtime = self.backend.provision()
        scripts = Path(self._runtime.overlay) / ".maya-scripts"
        scripts.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = scripts / f"{fingerprint}.py"
        if not target.exists():
            target.write_text(script, encoding="utf-8")
        relative = target.relative_to(Path(self._runtime.overlay)).as_posix()
        return self.backend.exec(
            self._runtime,
            ["python", relative],
            timeout=int(timeout_seconds),
            cancel_requested=cancel_requested,
        )

    def stop(self, *, reason: str, keep_overlay: bool = False) -> None:
        if self._runtime is not None:
            self.backend.stop(self._runtime, reason=reason, keep_overlay=keep_overlay)
            if not keep_overlay:
                self._runtime = None


__all__ = [
    "BackendCommandRunner",
    "ContainerEvidence",
    "ExecutionBackend",
    "MANDATORY_LIMITS",
    "RootlessContainerBackend",
    "RuntimeCapabilities",
    "RuntimeRef",
    "TrustedLocalBackend",
]

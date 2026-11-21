"""Execution backends for the worker (WP1).

The control plane asks a backend for capabilities before planning. An isolated
profile is never silently downgraded: if the OCI runtime or cgroup controllers
cannot be verified, :meth:`RootlessContainerBackend.capabilities` reports it and
:meth:`RootlessContainerBackend.provision` raises :class:`RuntimeUnavailable`.

WP1 implements the local trusted backend and a rootless OCI backend. Remote
microVM and user-owned GPU backends are deliberately absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from mayaworkbench.errors import RuntimeUnavailable
from mayaworkbench.workspace.runner import (
    CommandPolicy,
    CommandResult,
    CommandRunner,
    LimitEnforcement,
    summarize_limits,
)

_CGROUP_CONTROLLERS = "/sys/fs/cgroup/cgroup.controllers"
_REQUIRED_CONTROLLERS = frozenset({"pids", "memory", "cpu"})


@dataclass(frozen=True)
class RuntimeCapabilities:
    available: bool
    reasons: tuple[str, ...]
    limits: dict[str, LimitEnforcement]
    network_isolation: str

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "reasons": list(self.reasons),
            "limits": {name: item.as_dict() for name, item in sorted(self.limits.items())},
            "network_isolation": self.network_isolation,
        }


@dataclass(frozen=True)
class RuntimeRef:
    id: str
    backend: str
    source: Path
    overlay: Path
    image: str
    container_name: str


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

    def stop(self, runtime: RuntimeRef, *, reason: str) -> None: ...

    def collect_artifacts(self, runtime: RuntimeRef) -> list[Path]: ...


def _cgroup_controllers() -> tuple[set[str], str | None]:
    try:
        with open(_CGROUP_CONTROLLERS, encoding="ascii") as handle:
            return set(handle.read().split()), None
    except OSError as exc:
        return set(), f"cgroup v2 not available: {exc}"


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
            "cpu": LimitEnforcement(
                "unsupported", None, "verified per run by a getrlimit readback"
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
        return RuntimeCapabilities(True, ("host user privileges; NOT a sandbox",), limits, "not_requested")

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

    def stop(self, runtime: RuntimeRef, *, reason: str) -> None:  # pragma: no cover - local runner self-cleans
        del runtime, reason

    def collect_artifacts(self, runtime: RuntimeRef) -> list[Path]:
        return [path for path in runtime.overlay.rglob("*") if path.is_file()] if runtime.overlay.exists() else []


class RootlessContainerBackend:
    """Disposable rootless OCI workspace with read-only source and writable overlay.

    Capability detection is mandatory: no container is created unless an OCI
    client and the pids/memory/cpu cgroup controllers are present. The source is
    mounted read-only, a distinct overlay is writable, the root filesystem is
    read-only, capabilities are dropped, no-new-privileges is set, the user's
    home and Docker socket are never mounted, and the network is denied by
    default. Only allowlisted, non-secret environment variables are passed.
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
    ):
        self.workspace = Path(workspace).resolve()
        self.run_home = Path(run_home).resolve()
        self.policy = policy
        self.image = image
        self._tool_override = tool
        self._allow_docker = allow_docker

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

    def capabilities(self) -> RuntimeCapabilities:
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

        limits: dict[str, LimitEnforcement] = {}
        limits["wall_time"] = LimitEnforcement("enforced", "supervisor")
        for name in ("cpu", "memory"):
            limits[name] = (
                LimitEnforcement("enforced", "cgroup-v2", "controller present")
                if name in controllers
                else LimitEnforcement("unsupported", None, "controller missing")
            )
        limits["process_count"] = (
            LimitEnforcement("enforced", "cgroup-v2", "pids.max applied per container")
            if "pids" in controllers
            else LimitEnforcement("unsupported", None, "pids controller missing")
        )
        limits["file_size"] = LimitEnforcement(
            "unsupported", None, "no verified per-container file-size controller"
        )
        limits["open_files"] = LimitEnforcement(
            "unsupported", None, "no verified per-container fd controller"
        )
        available = tool is not None and not missing
        return RuntimeCapabilities(
            available,
            tuple(reasons) or ("rootless OCI runtime and cgroup v2 verified",),
            limits,
            "none",
        )

    # -------------------------------------------------------------- lifecycle
    def provision(self) -> RuntimeRef:
        capabilities = self.capabilities()
        if not capabilities.available:
            raise RuntimeUnavailable(
                "ROOTLESS_CONTAINER runtime cannot be established",
                hint="; ".join(capabilities.reasons)
                + ". The task is blocked, not downgraded to host execution.",
            )
        runtime_id = uuid.uuid4().hex
        overlay = self.run_home / "containers" / runtime_id / "work"
        overlay.mkdir(parents=True, exist_ok=True, mode=0o700)
        return RuntimeRef(
            id=runtime_id,
            backend=self.backend_id,
            source=self.workspace,
            overlay=overlay,
            image=self.image,
            container_name=f"maya-{runtime_id[:16]}",
        )

    def _oci_argv(self, runtime: RuntimeRef, argv: list[str]) -> list[str]:
        tool, _ = self._detect_tool()
        assert tool is not None  # guarded by provision()
        env_flags: list[str] = []
        # Never forward the host PATH verbatim: it can embed home directories,
        # credentials paths and toolchains the container must not see.
        env_flags += ["-e", "PATH=/usr/local/bin:/usr/bin:/bin"]
        env_flags += ["-e", "HOME=/work", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        return [
            tool,
            "run",
            "--rm",
            "--name",
            runtime.container_name,
            "--user",
            f"{uid}:{gid}",
            "--read-only",
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
            "--cpus",
            str(max(1, min(4, self.policy.cpu_seconds or 1))),
            "--mount",
            f"type=bind,src={runtime.source},dst=/src,readonly",
            "--mount",
            f"type=bind,src={runtime.overlay},dst=/work",
            "-w",
            "/work",
            *env_flags,
            runtime.image,
            *argv,
        ]

    def exec(
        self,
        runtime: RuntimeRef,
        argv: list[str],
        *,
        timeout: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CommandResult:
        oci_argv = self._oci_argv(runtime, argv)
        # The OCI client is a trusted, allowlisted executable. The policy that
        # permits absolute arguments is scoped to this client; the project
        # argv inside the container is never parsed by that policy.
        policy = CommandPolicy(
            profile="CPU_SMOKE",
            trust_profile="LOCAL_TRUSTED",
            allow_absolute_args=True,
            # The OCI client is trusted; the container itself enforces
            # `--network none`. Applying the host netns probe here would block
            # every container run on hosts without netns support.
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            allowed_executables=(Path(oci_argv[0]).name,),
            max_runtime_seconds=max(timeout, self.policy.max_runtime_seconds),
            max_output_bytes=self.policy.max_output_bytes,
        )
        runner = CommandRunner(runtime.overlay.parent, policy=policy, run_home=runtime.overlay.parent)
        observed = runner.run(
            oci_argv, timeout_seconds=timeout, cancel_requested=cancel_requested
        )
        limits = self.capabilities().limits
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
            env_names=sorted({"PATH", "LANG", "LC_ALL", "HOME", "PYTHONDONTWRITEBYTECODE"}),
            cwd=str(runtime.overlay),
        )

    def stop(self, runtime: RuntimeRef, *, reason: str) -> None:
        del reason
        tool, _ = self._detect_tool()
        if tool:
            try:
                work_dir = (
                    str(runtime.overlay.parent)
                    if runtime.overlay.parent.exists()
                    else str(self.run_home)
                )
                subprocess.run(
                    [tool, "rm", "-f", runtime.container_name],
                    cwd=work_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
                pass
        overlay_root = runtime.overlay.parent
        if overlay_root.exists():
            shutil.rmtree(overlay_root, ignore_errors=True)

    def collect_artifacts(self, runtime: RuntimeRef) -> list[Path]:
        return (
            [path for path in runtime.overlay.rglob("*") if path.is_file()]
            if runtime.overlay.exists()
            else []
        )


class BackendCommandRunner:
    """Adapter exposing an isolated backend as the runner interface workers use.

    Provisioning happens on first use, so a contract that never executes a
    command never creates a container. ``stop`` is idempotent.
    """

    def __init__(self, backend: ExecutionBackend):
        self.backend = backend
        self._runtime: RuntimeRef | None = None

    @property
    def policy(self) -> CommandPolicy:  # pragma: no cover - evidence consumers only
        return getattr(self.backend, "policy", CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
        ))

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

    def stop(self, *, reason: str) -> None:
        if self._runtime is not None:
            self.backend.stop(self._runtime, reason=reason)
            self._runtime = None


__all__ = [
    "BackendCommandRunner",
    "ExecutionBackend",
    "RuntimeCapabilities",
    "RuntimeRef",
    "TrustedLocalBackend",
    "RootlessContainerBackend",
]

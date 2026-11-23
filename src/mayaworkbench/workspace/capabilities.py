"""Canonical runtime capability evidence for the execution plane.

This module reports what the host can *actually* provide. It never translates
"the OS has a knob" into "Maya enforces it": a controller name is only presence
evidence, a namespace flag is only a claim, and a profile is available only
after the corresponding runtime probe passes. Per-run enforcement evidence
comes from ``CommandResult.limits`` after an in-child readback.

All consumers (CLI doctor/profiles, environment doctor, docs) use the single
:class:`ExecutionCapabilityReport` produced here so the status terminology
cannot drift between surfaces.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mayaworkbench.domain.enums import ExecutionTrust
from mayaworkbench.workspace.backends import RootlessContainerBackend
from mayaworkbench.workspace.cgroups import probe_cgroup_enforcement
from mayaworkbench.workspace.runner import (
    CommandPolicy,
    LIMIT_NAMES,
    _network_probe_script,
)

try:  # POSIX only
    import resource  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]


_RLIMIT_BY_NAME = {
    "cpu_time": "RLIMIT_CPU",
    "memory": "RLIMIT_AS",
    "open_files": "RLIMIT_NOFILE",
    "file_size": "RLIMIT_FSIZE",
}


def _network_probe() -> dict[str, Any]:
    """Positive proof only: an isolated child cannot reach a parent listener."""
    unshare = shutil.which("unshare")
    if not unshare:
        return {"available": False, "reason": "unshare is not installed"}
    failures: list[str] = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = int(listener.getsockname()[1])
        for flags in (["-n"], ["-Urn"]):
            try:
                probe = subprocess.run(
                    [unshare, *flags, sys.executable, "-c", _network_probe_script(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{' '.join(flags)}: {exc}")
                continue
            if probe.returncode == 0:
                return {
                    "available": True,
                    "mechanism": "network_namespace",
                    "flags": flags,
                    "verification": "positive_loopback_unreachability",
                }
            detail = (probe.stderr or b"").decode("utf-8", errors="replace").strip()
            failures.append(f"{' '.join(flags)}: {detail or f'exit {probe.returncode}'}")
    except OSError as exc:  # pragma: no cover - loopback bind failure
        failures.append(f"could not create verification listener: {exc}")
    finally:
        listener.close()
    return {"available": False, "reason": "; ".join(failures)}


def _cgroup_probe() -> dict[str, Any]:
    enforcement = probe_cgroup_enforcement()
    controllers_path = "/sys/fs/cgroup/cgroup.controllers"
    try:
        with open(controllers_path, encoding="ascii") as handle:
            present = handle.read().split()
    except OSError:
        present = []
    return {
        "available": bool(enforcement["available"]),
        "controllers": present,
        "pids": "pids" in present,
        "wired_into_runner": True,
        "verification": enforcement,
        "note": (
            "Controller names are presence only. A writable delegated child scope "
            "with an exact finite pids.max readback is required before the runner "
            "reports process_count: enforced."
        ),
    }


def _oci_probe() -> dict[str, Any]:
    """Verify rootless OCI availability with a live, task-equivalent probe.

    Controller names and a `container starts` smoke test are not evidence that
    a *specific* limit is applied. The authoritative answer comes from
    :meth:`RootlessContainerBackend.verify_capabilities`, which reads the
    effective ``pids.max``/``memory.max``/``cpu.max`` and rlimits back from
    inside a container launched with the same flags a task uses.

    The probe is opt-in (``MAYA_OCI_SMOKE=1``) because it starts a container;
    until it passes, every limit stays ``not_applied`` and the profile is
    unavailable.
    """

    allow_docker = os.environ.get("MAYA_ALLOW_DOCKER", "").lower() in {"1", "true", "yes"}
    podman = shutil.which("podman")
    docker = shutil.which("docker") if allow_docker else None
    tool = podman or docker
    rootless = False
    reasons: list[str] = []
    if tool is None:
        reasons.append(
            "no verified rootless OCI runtime found (podman preferred; docker requires "
            "MAYA_ALLOW_DOCKER=1 and explicit rootless verification)"
        )
    elif Path(tool).name == "podman":
        try:
            info = subprocess.run(
                [tool, "info", "--format", "{{.Host.Security.Rootless}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            rootless = (
                info.returncode == 0
                and info.stdout.decode("ascii", "replace").strip() == "true"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(f"podman rootless verification failed: {exc}")
        if not rootless:
            reasons.append("podman did not confirm a rootless host")
    else:
        reasons.append("docker rootless status is unverified by this probe")

    cgroup = probe_cgroup_enforcement()
    # Deliberately small, fast values: the probe proves the *mechanism* at these
    # values. A real task re-verifies with its own contract policy before use.
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        trust_profile="LOCAL_TRUSTED",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=16,
        memory_limit_mb=512,
        cpu_time_seconds=2,
        cpu_quota=1.0,
        max_runtime_seconds=300,
    )
    probe_home = Path(tempfile.gettempdir()) / "maya-capability-probe"
    backend = RootlessContainerBackend(
        Path.cwd(),
        probe_home,
        policy,
        tool=Path(tool).name if tool else None,
        allow_docker=allow_docker,
    )
    prerequisites_ok, prerequisite_reasons = backend.prerequisites()
    smoke_enabled = os.environ.get("MAYA_OCI_SMOKE", "").lower() in {
        "1",
        "true",
        "yes",
    }
    smoke: dict[str, Any] = {"status": "not_run"}
    evidence: dict[str, Any] | None = None
    if not smoke_enabled:
        smoke["detail"] = (
            "set MAYA_OCI_SMOKE=1 to run the bounded in-container probe; "
            "availability stays false until it passes"
        )
    elif not prerequisites_ok:
        smoke = {
            "status": "skipped",
            "detail": "; ".join(prerequisite_reasons),
        }
    else:
        observed = backend.verify_capabilities()
        evidence = observed.as_dict()
        smoke = {
            "status": observed.probe_status,
            "detail": observed.detail[:500],
            "mandatory_failures": observed.mandatory_failures,
        }

    capabilities = backend.capabilities().as_dict()
    available = bool(capabilities["available"] and smoke["status"] == "passed")
    if not available:
        reasons.extend(capabilities.get("reasons") or [])
    return {
        "available": available,
        "runtime": {"tool": tool, "rootless": rootless},
        "cgroup": cgroup,
        "smoke_test": smoke,
        "evidence": evidence,
        "capabilities": capabilities,
        "limit_policy": policy.describe(),
        "reason": "; ".join(dict.fromkeys(reasons))
        if reasons
        else "rootless OCI isolation verified by a live in-container probe",
    }


@dataclass(frozen=True)
class ExecutionCapabilityReport:
    """Machine-readable, single-source execution truth for every surface."""

    runtime: dict[str, Any]
    trust_profiles: dict[str, Any]
    limits: dict[str, Any]
    network_isolation: dict[str, Any]
    cgroup_v2: dict[str, Any]
    execution_backends: dict[str, Any]
    warning: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "trust_profiles": self.trust_profiles,
            "limits": self.limits,
            "network_isolation": self.network_isolation,
            "cgroup_v2": self.cgroup_v2,
            "execution_backends": self.execution_backends,
            "warning": self.warning,
        }


def probe_execution_capability_report() -> ExecutionCapabilityReport:
    """Generate the canonical report from live runtime probes."""
    limits: dict[str, Any] = {}
    for name in LIMIT_NAMES:
        if name == "wall_time":
            limits[name] = {
                "available": True,
                "mechanism": "supervisor",
                "detail": "parent polls the wall clock and kills the process group",
            }
        elif name == "process_count":
            limits[name] = {
                "available": False,
                "mechanism": None,
                "detail": "RLIMIT_NPROC is per-UID and not enforced for privileged "
                "processes; process count is enforced only by a verified cgroup "
                "scope (CommandRunner use_cgroup_scope) or a container backend, "
                "and is reported per run in CommandResult.limits",
            }
        elif name == "cpu_quota":
            limits[name] = {
                "available": False,
                "mechanism": None,
                "detail": "CPU concurrency (cores) is a cgroup cpu.max control; the "
                "host runner reports it as unsupported and the container backend "
                "proves it with an in-container readback",
            }
        else:
            attribute = _RLIMIT_BY_NAME[name]
            supported = resource is not None and hasattr(resource, attribute)
            limits[name] = {
                "available": bool(supported),
                "mechanism": f"rlimit-{name}" if supported else None,
                "detail": "kernel support only; per-run status comes from getrlimit "
                "readback in CommandResult.limits",
            }

    network = _network_probe()
    cgroup = _cgroup_probe()
    oci = _oci_probe()
    backend_caps = dict(oci["capabilities"])
    backend_caps["available"] = bool(backend_caps["available"] and oci["available"])
    backend_caps["oci_verification"] = oci

    profiles: dict[str, Any] = {}
    descriptions = {
        ExecutionTrust.READ_ONLY_ANALYSIS: (
            "read-only tool mask enforced in ToolGateway; zero subprocesses or writes"
        ),
        ExecutionTrust.LOCAL_TRUSTED: "host user privileges; NOT a sandbox",
        ExecutionTrust.ROOTLESS_CONTAINER: "rootless OCI with verified cgroups",
        ExecutionTrust.REMOTE_MICROVM: "per-task isolated VM (user-owned infrastructure)",
        ExecutionTrust.GPU_RUNNER: "user-owned GPU/TPU runner (user-owned infrastructure)",
    }
    for profile in ExecutionTrust:
        if profile is ExecutionTrust.LOCAL_TRUSTED:
            available, reason = True, "implemented and labelled; host privileges, not a sandbox"
        elif profile is ExecutionTrust.READ_ONLY_ANALYSIS:
            available, reason = (
                True,
                "enforced by ToolGateway read-only capability mask; "
                "no subprocesses or writes are permitted",
            )
        elif profile is ExecutionTrust.ROOTLESS_CONTAINER:
            available = oci["available"]
            reason = f"{oci['reason']}; smoke test: {oci['smoke_test']['status']}"
        else:
            available, reason = False, (
                "requires user-provided infrastructure; requests are blocked rather than downgraded"
            )
        profiles[profile.value] = {
            "available": bool(available),
            "boundary": descriptions[profile],
            "reason": reason,
        }

    uid = os.getuid() if hasattr(os, "getuid") else None
    return ExecutionCapabilityReport(
        runtime={
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "uid": uid,
            "privileged": bool(uid == 0),
        },
        trust_profiles=profiles,
        limits=limits,
        network_isolation=network,
        cgroup_v2=cgroup,
        execution_backends={
            "trusted-local": {
                "available": True,
                "boundary": "host user privileges; NOT a sandbox",
            },
            "rootless-container": backend_caps,
        },
        warning=(
            "LOCAL_TRUSTED executes project code with the user's OS privileges "
            "and is NOT a sandbox. A Git worktree is source isolation, not process or "
            "filesystem isolation."
        ),
    )


def probe_execution_capabilities() -> dict[str, Any]:
    """Backwards-compatible JSON-serialisable view of the canonical report."""
    return probe_execution_capability_report().as_dict()


__all__ = [
    "ExecutionCapabilityReport",
    "probe_execution_capability_report",
    "probe_execution_capabilities",
]

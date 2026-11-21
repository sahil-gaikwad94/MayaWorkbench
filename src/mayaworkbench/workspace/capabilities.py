"""Runtime capability evidence for the execution plane (WP0 deliverable).

This module reports what the host can *actually* provide. It never translates
"the OS has a knob" into "Maya enforces it": per-run enforcement is reported by
``CommandResult.limits`` after a getrlimit readback inside the child process.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mayaworkbench.domain.enums import ExecutionTrust
from mayaworkbench.workspace.backends import RootlessContainerBackend
from mayaworkbench.workspace.runner import CommandPolicy, LIMIT_NAMES

try:  # POSIX only
    import resource  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]


_RLIMIT_BY_NAME = {
    "cpu": "RLIMIT_CPU",
    "memory": "RLIMIT_AS",
    "file_size": "RLIMIT_FSIZE",
    "open_files": "RLIMIT_NOFILE",
}


def _network_probe() -> dict[str, Any]:
    unshare = shutil.which("unshare")
    if not unshare:
        return {"available": False, "reason": "unshare is not installed"}
    failures: list[str] = []
    for flags in (["-n"], ["-Urn"]):
        try:
            probe = subprocess.run(
                [unshare, *flags, sys.executable, "-c", "import sys; sys.exit(0)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{' '.join(flags)}: {exc}")
            continue
        if probe.returncode == 0:
            return {"available": True, "mechanism": "network_namespace", "flags": flags}
        detail = (probe.stderr or b"").decode("utf-8", errors="replace").strip()
        failures.append(f"{' '.join(flags)}: {detail or f'exit {probe.returncode}'}")
    return {"available": False, "reason": "; ".join(failures)}


def _cgroup_probe() -> dict[str, Any]:
    controllers_path = "/sys/fs/cgroup/cgroup.controllers"
    try:
        with open(controllers_path, encoding="ascii") as handle:
            controllers = handle.read().split()
    except OSError as exc:
        return {"available": False, "reason": f"cgroup v2 not mounted: {exc}"}
    return {
        "available": bool(controllers),
        "controllers": controllers,
        "pids": "pids" in controllers,
        "wired_into_runner": False,
        "note": "cgroup limits are a future isolated-runtime backend; today they "
        "are only reported, never claimed as enforced",
    }


def probe_execution_capabilities() -> dict[str, Any]:
    """Return a JSON-serialisable report of the current host's capabilities."""
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
                "processes; no cgroup/container backend is wired into the runner",
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

    profiles: dict[str, Any] = {}
    for profile in ExecutionTrust:
        implemented = profile is ExecutionTrust.LOCAL_TRUSTED
        profiles[profile.value] = {
            "available": implemented,
            "boundary": {
                ExecutionTrust.READ_ONLY_ANALYSIS: "read-only snapshot, no execution",
                ExecutionTrust.LOCAL_TRUSTED: "host user privileges; NOT a sandbox",
                ExecutionTrust.ROOTLESS_CONTAINER: "rootless OCI with cgroups (future)",
                ExecutionTrust.REMOTE_MICROVM: "per-task isolated VM (future)",
                ExecutionTrust.GPU_RUNNER: "user-owned GPU/TPU runner (future)",
            }[profile],
            "reason": (
                "implemented and labelled"
                if implemented
                else "not available; requests are blocked rather than downgraded"
            ),
        }

    container_backend = RootlessContainerBackend(
        Path.cwd(),
        Path(tempfile.gettempdir()) / "maya-capability-probe",
        CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
        ),
    )
    uid = os.getuid() if hasattr(os, "getuid") else None
    return {
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "uid": uid,
            "privileged": bool(uid == 0),
        },
        "trust_profiles": profiles,
        "limits": limits,
        "network_isolation": _network_probe(),
        "cgroup_v2": _cgroup_probe(),
        "execution_backends": {
            "trusted-local": {
                "available": True,
                "boundary": "host user privileges; NOT a sandbox",
            },
            "rootless-container": container_backend.capabilities().as_dict(),
        },
        "warning": "LOCAL_TRUSTED executes project code with the user's OS privileges "
        "and is NOT a sandbox. A Git worktree is source isolation, not process or "
        "filesystem isolation.",
    }


__all__ = ["probe_execution_capabilities"]

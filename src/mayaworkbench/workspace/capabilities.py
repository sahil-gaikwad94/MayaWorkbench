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

import json
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
    "cpu": "RLIMIT_CPU",
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


#: A bounded, machine-readable probe. It runs inside the SAME production
#: container (identical argv/flags/mounts) and reports *observed* values from
#: inside the container, never values copied from the policy object.
_OCI_LIMIT_PROBE = r"""
import json, os, socket, sys

port, expected_pids, expected_mem_mb, expected_cpus, secret_marker = sys.argv[1:6]
report = {"limits": {}, "network": {}, "secrets": {}, "mounts": {}}

# Effective cgroup v2 values as seen by THIS container.
for name, path in (
    ("pids", "/sys/fs/cgroup/pids.max"),
    ("memory_bytes", "/sys/fs/cgroup/memory.max"),
    ("cpu_quota", "/sys/fs/cgroup/cpu.max"),
):
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            report["limits"][name] = handle.read().strip()
    except OSError as exc:
        report["limits"][name] = "unreadable:" + type(exc).__name__

# Active network probe: a --network none container cannot reach a listener
# owned by the parent on loopback. Flag presence alone is not proof.
try:
    connection = socket.create_connection(("127.0.0.1", int(port)), timeout=1)
    connection.close()
    report["network"] = "REACHABLE"
except OSError:
    report["network"] = "DENIED"

# Secret denial from every task-visible mount. The original worktree's .env and
# git config must not exist in the sanitized snapshot at /src.
for label, path in (
    ("src_env", "/src/.env"),
    ("src_git_config", "/src/.git/config"),
    ("src_npmrc", "/src/.npmrc"),
    ("src_credentials", "/src/config/credentials.json"),
):
    try:
        with open(path, "rb") as handle:
            data = handle.read(256).decode("utf-8", "replace")
        report["secrets"][label] = "LEAK" if secret_marker in data else "READABLE"
    except OSError:
        report["secrets"][label] = "DENIED"

# Mount visibility: /src must be a read-only bind, /work a writable bind.
try:
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
        mountinfo = handle.read()
    report["mounts"]["src"] = any(" /src " in line for line in mountinfo.splitlines())
    report["mounts"]["work"] = any(" /work " in line for line in mountinfo.splitlines())
except OSError:
    pass

print("MAYA_OCI_PROBE=" + json.dumps(report, sort_keys=True))
"""


def _verified_limit(
    name: str, requested: Any, observed: Any, *, matcher=None
) -> dict[str, Any]:
    """Build per-limit evidence from an observed value (never from the policy)."""
    ok = False
    if matcher is not None:
        try:
            ok = bool(matcher(observed))
        except (TypeError, ValueError):
            ok = False
    else:
        # cgroup files report integers as strings; compare numerically when
        # both sides are integral so a readback of "8" matches a request of 8.
        candidate = observed
        if isinstance(observed, str) and observed.isdigit():
            candidate = int(observed)
        ok = (
            not isinstance(observed, bool)
            and candidate == requested
            and observed not in (None, "", "max", "unreadable")
        )
    return {
        "requested": requested,
        "observed": observed,
        "status": "enforced" if ok else "not_applied",
        "mechanism": "cgroup-v2-live-readback" if ok else None,
        "detail": (
            f"{name} read back inside the task container and matched the request"
            if ok
            else f"{name} could not be verified against the request inside the container"
        ),
    }


def verified_limits_from_probe(oci: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract the enforced per-limit evidence from an OCI probe report."""
    limits = oci.get("smoke_test", {}).get("limits", {})
    return {
        name: limits[name]
        for name in ("cpu_quota", "memory_bytes", "pids")
        if isinstance(limits.get(name), dict) and limits[name].get("status") == "enforced"
    }


def _oci_probe() -> dict[str, Any]:
    """Verify rootless OCI using the real backend and live in-container readbacks.

    Controller names, argv flags and a basic smoke marker are presence evidence
    only. Availability requires a bounded probe that constructs the actual
    production backend, reads effective cgroup limits from inside the container,
    proves network/secret/mount denial with active canaries, and verifies clean
    teardown. Each control is reported independently: a failed control cannot
    be masked by a passing one.
    """
    allow_docker = os.environ.get("MAYA_ALLOW_DOCKER", "").lower() in {"1", "true", "yes"}
    podman = shutil.which("podman")
    docker = shutil.which("docker") if allow_docker else None
    tool = podman or docker
    reasons: list[str] = []
    if tool is None:
        reasons.append(
            "no verified rootless OCI runtime found (podman preferred; docker requires "
            "MAYA_ALLOW_DOCKER=1 and explicit rootless verification)"
        )
    rootless = False
    if tool and Path(tool).name == "podman":
        try:
            info = subprocess.run(
                [tool, "info", "--format", "{{.Host.Security.Rootless}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            rootless = info.returncode == 0 and info.stdout.decode("ascii", "replace").strip() == "true"
        except (OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(f"podman rootless verification failed: {exc}")
        if not rootless:
            reasons.append("podman did not confirm a rootless host")
    elif tool:
        reasons.append("docker rootless status is unverified by this probe")

    cgroup = probe_cgroup_enforcement()
    if not cgroup["available"]:
        reasons.append("no writable, read-back-verified cgroup v2 process limit")

    smoke_enabled = os.environ.get("MAYA_OCI_SMOKE", "").lower() in {"1", "true", "yes"}
    smoke: dict[str, Any] = {
        "status": "not_run",
        "limits": {},
        "network": {"policy": "denied", "active_probe": "not_run"},
        "host_canary": "not_run",
        "secret_mount_probe": "not_run",
        "cleanup": "not_run",
        "mount_policy_hash": "",
    }
    if not smoke_enabled:
        smoke["detail"] = (
            "set MAYA_OCI_SMOKE=1 to run a bounded live OCI probe; "
            "availability stays false until every mandatory control passes"
        )
    elif not (tool and rootless and cgroup["available"]):
        smoke["status"] = "skipped"
        smoke["detail"] = "prerequisites (rootless runtime/cgroup) unavailable"
    else:
        try:
            smoke = _run_live_oci_probe(tool, smoke)
        except Exception as exc:  # noqa: BLE001 - a probe failure must fail closed
            smoke = {
                **smoke,
                "status": "failed",
                "detail": f"{type(exc).__name__}: {exc}"[:1000],
            }

    mandatory = [
        smoke.get("limits", {}).get(name, {}).get("status") == "enforced"
        for name in ("pids", "memory_bytes", "cpu_quota")
    ]
    network_ok = smoke.get("network", {}).get("active_probe") == "passed"
    secrets_ok = smoke.get("secret_mount_probe") == "denied"
    cleanup_ok = smoke.get("cleanup") == "passed"
    available = bool(
        tool and rootless and cgroup["available"]
        and smoke.get("status") == "passed"
        and all(mandatory) and network_ok and secrets_ok and cleanup_ok
    )
    if not available and not reasons:
        failed = [
            name for name, ok in zip(("pids", "memory_bytes", "cpu_quota"), mandatory)
            if not ok
        ]
        reason = f"rootless OCI probe is {smoke.get('status')}"
        if failed:
            reason += f"; unverified limits: {', '.join(failed)}"
        if not network_ok:
            reason += "; network denial not proven"
        if not secrets_ok:
            reason += "; secret/mount denial not proven"
        if not cleanup_ok:
            reason += "; cleanup not proven"
        reasons.append(reason)
    return {
        "available": available,
        "runtime": {"tool": tool, "rootless": rootless},
        "cgroup": cgroup,
        "smoke_test": smoke,
        "reason": "; ".join(reasons) if reasons else "rootless OCI verified by live in-container probe",
    }


def _run_live_oci_probe(tool: str, smoke: dict[str, Any]) -> dict[str, Any]:
    """Construct the real backend, run the limit probe and assemble evidence."""
    import socket as socket_module
    import tempfile

    from mayaworkbench.workspace.backends import RootlessContainerBackend

    marker = "maya-oci-secret-canary-do-not-leak"
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        trust_profile="LOCAL_TRUSTED",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=8,
        memory_limit_mb=256,
        # P0-02: use the same finite values the production backend applies so
        # the readback is compared against the real request.
        cpu_cores=2,
        cpu_time_seconds=15,
        max_runtime_seconds=60,
    )
    requested_cpus = str(max(1, min(4, policy.cpu_cores or 1)))
    with tempfile.TemporaryDirectory(prefix="maya-oci-probe-") as tmp:
        root = Path(tmp)
        source = root / "source"
        source.mkdir(mode=0o700)
        (source / "app.py").write_text("print('probe')\n", encoding="utf-8")
        # The original worktree contains a secret; the sanitized /src must not.
        (source / ".env").write_text(f"TOKEN={marker}\n", encoding="utf-8")
        (source / ".git").mkdir()
        (source / ".git" / "config").write_text(marker, encoding="utf-8")
        backend = RootlessContainerBackend(
            source, root / "run-home", policy, tool=tool, verified=True
        )
        runtime = backend.provision()
        listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = int(listener.getsockname()[1])
            result = backend.exec(
                runtime,
                [
                    "python", "-c", _OCI_LIMIT_PROBE,
                    str(port), str(policy.max_processes), str(policy.memory_limit_mb),
                    requested_cpus, marker,
                ],
                timeout=60,
            )
        finally:
            listener.close()

        payload: dict[str, Any] | None = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("MAYA_OCI_PROBE="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    payload = None
        if payload is None:
            return {
                **smoke,
                "status": "failed",
                "detail": (result.stderr or result.stdout or "probe printed no marker").strip()[:500],
            }

        limits: dict[str, Any] = {}
        observed_pids = payload["limits"].get("pids", "")
        limits["pids"] = _verified_limit("pids", policy.max_processes, observed_pids)

        observed_mem = payload["limits"].get("memory_bytes", "")
        expected_mem = policy.memory_limit_mb * 1024 * 1024

        def _memory_ok(value: Any) -> bool:
            return str(value).isdigit() and int(value) == expected_mem

        limits["memory_bytes"] = _verified_limit(
            "memory_bytes", f"{expected_mem} bytes ({policy.memory_limit_mb} MB)",
            observed_mem, matcher=_memory_ok,
        )

        observed_cpu = str(payload["limits"].get("cpu_quota", ""))
        expected_cpu_cpus = int(requested_cpus)

        def _cpu_ok(value: Any) -> bool:
            parts = str(value).split()
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                return False
            quota, period = int(parts[0]), int(parts[1])
            return period > 0 and abs(quota / period - expected_cpu_cpus) < 0.01

        limits["cpu_quota"] = _verified_limit(
            "cpu_quota", f"{requested_cpus} CPU cores (cgroup quota)",
            observed_cpu or "unreadable", matcher=_cpu_ok,
        )

        network_ok = payload.get("network") == "DENIED"
        secret_probe = payload.get("secrets", {})
        secrets_ok = bool(secret_probe) and all(
            value == "DENIED" for value in secret_probe.values()
        )
        mounts = payload.get("mounts", {})
        mounts_ok = mounts.get("src") is True and mounts.get("work") is True
        if not mounts_ok:
            secrets_ok = False
        cleanup_succeeded = True
        try:
            backend.stop(runtime, reason="capability probe complete")
        except Exception:  # noqa: BLE001
            cleanup_succeeded = False
        overlay_root = runtime.overlay.parent
        cleanup_ok = cleanup_succeeded and not overlay_root.exists()
        passed = (
            result.exit_code == 0
            and all(item["status"] == "enforced" for item in limits.values())
            and network_ok and secrets_ok and cleanup_ok
        )
        return {
            "status": "passed" if passed else "failed",
            "detail": (
                "live in-container readback of pids/memory/cpu, active network "
                "probe, secret denial and cleanup"
            ),
            "limits": limits,
            "network": {
                "policy": "denied",
                "active_probe": "passed" if network_ok else "failed",
                "detail": payload.get("network"),
            },
            "host_canary": "denied" if secrets_ok else "leaked",
            "secret_mount_probe": "denied" if secrets_ok else "leaked",
            "mount_visibility": mounts,
            "cleanup": "passed" if cleanup_ok else "failed",
            "mount_policy_hash": getattr(runtime, "mount_policy_sha256", ""),
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
    container_backend = RootlessContainerBackend(
        Path.cwd(),
        Path(tempfile.gettempdir()) / "maya-capability-probe",
        CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
        ),
    )
    backend_caps = container_backend.capabilities().as_dict()
    backend_caps["available"] = bool(backend_caps["available"] and oci["available"])
    backend_caps["oci_verification"] = oci
    # A backend may only repeat limits that the live probe actually observed.
    backend_caps["verified_limits"] = verified_limits_from_probe(oci)

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

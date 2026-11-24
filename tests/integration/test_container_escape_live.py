"""REAL rootless-OCI escape harness (opt-in, ``@pytest.mark.oci``).

This module is collected by the ordinary suite but never runs at collection
time. It runs only when BOTH gates are satisfied:

1. ``MAYA_OCI_LIVE=1`` is exported deliberately; and
2. the canonical ``probe_execution_capability_report()`` reports
   ``execution_backends["rootless-container"]["available"] is True`` — which on
   this codebase requires a rootless OCI client, a writable cgroup-v2 delegation
   with an exact limit readback, and a passed bounded smoke test.

Otherwise every test ``pytest.skip("reason: ...")`` with the concrete blocker.
A skip is never reported as a pass. No live OCI run has occurred in the
environment where this harness was added.

The harness uses the real :class:`RootlessContainerBackend` with ``verified=True``
against the real runtime discovered on PATH. It builds a throwaway fixture repo
(source file, ``.env`` fake secret) and a host canary *outside* that source, then
checks the populated overlay, the real OCI argv, runtime-level denial of host and
system reads, limit evidence and full cleanup. All subprocess timeouts are <= 60 s.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mayaworkbench.workspace.backends as backends
from mayaworkbench.workspace.backends import RootlessContainerBackend
from mayaworkbench.workspace.runner import CommandPolicy

pytestmark = pytest.mark.oci

_TRUE = {"1", "true", "yes", "on"}
_SOURCE_TEXT = "SOURCE_MARKER = 'overlay-saw-the-pinned-source'\n"
_FAKE_SECRET = "sk-fake-live-canary-do-not-forward"
_HOST_CANARY = "HOST-CANARY-DO-NOT-LEAK"
_MAX_TIMEOUT = 60  # hard ceiling for every subprocess this harness starts

# A deliberately hostile payload. It reports outcomes as machine-readable
# markers instead of leaking file contents. All target paths are passed in as
# argv after `-c` so the script itself stays a fixed, auditable string.
_ESCAPE_PROBE = r"""
import os
import sys

host_canary, host_home = sys.argv[1], sys.argv[2]
print("HOME=" + os.environ.get("HOME", "<unset>"))
print("EUID=" + str(os.geteuid()))
targets = (
    ("host_canary", host_canary),
    ("host_home", host_home),
    ("docker_sock", "/var/run/docker.sock"),
    ("shadow", "/etc/shadow"),
    ("home_dot_ssh", os.path.join(os.environ.get("HOME", ""), ".ssh")),
    # P0-01: the original worktree's secrets must be unreachable from /src.
    ("src_env", "/src/.env"),
    ("src_git_config", "/src/.git/config"),
    ("src_npmrc", "/src/.npmrc"),
)
for label, path in targets:
    try:
        with open(path, "rb") as handle:
            handle.read(64)
    except Exception:
        print("DENIED:" + label)
    else:
        print("LEAK:" + label)
"""

_PIDS_READBACK = "import sys; sys.stdout.write(open('/sys/fs/cgroup/pids.max').read().strip())"


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _gate() -> tuple[Any | None, str | None]:
    """Resolve the capability gate at TEST time, never at import/collection time."""
    if not _flag_enabled("MAYA_OCI_LIVE"):
        raw = os.environ.get("MAYA_OCI_LIVE")
        return None, (
            "MAYA_OCI_LIVE must be set to 1 (current value: "
            f"{raw!r}); this harness never runs implicitly"
        )
    # Imported lazily so a collection-only run performs no runtime probing.
    from mayaworkbench.workspace.capabilities import probe_execution_capability_report

    report = probe_execution_capability_report()
    backend = report.execution_backends.get("rootless-container")
    available = backend.get("available") if isinstance(backend, dict) else None
    if available is not True:
        oci = backend.get("oci_verification", {}) if isinstance(backend, dict) else {}
        smoke = oci.get("smoke_test", {}) if isinstance(oci, dict) else {}
        return None, (
            "rootless-container capability is not available=True "
            f"(reason: {oci.get('reason', 'not reported')}; "
            f"smoke: {smoke.get('status', 'not reported')}). "
            "Provide a verified rootless runtime and set MAYA_OCI_SMOKE=1; "
            "controller names alone are not proof"
        )
    return report, None


@pytest.fixture(scope="module")
def oci_capability():
    """The single canonical probe result, shared by this module's tests."""
    report, reason = _gate()
    if reason:
        pytest.skip(f"reason: {reason}")
    return report


@pytest.fixture
def oci_live(oci_capability, tmp_path, monkeypatch):
    """Build the throwaway fixture repo, host canary and real backend."""
    source = tmp_path / "fixture-repo"
    source.mkdir(mode=0o700)
    (source / "app.py").write_text(_SOURCE_TEXT, encoding="utf-8")
    # A secret-shaped file inside the source must never reach the overlay.
    (source / ".env").write_text(f"MAYA_MODEL_API_KEY={_FAKE_SECRET}\n", encoding="utf-8")

    # Host canary lives OUTSIDE the source tree and is never mounted.
    canary = tmp_path / "host-canary.txt"
    canary.write_text(f"{_HOST_CANARY}\n", encoding="utf-8")

    # A fake host secret in the host environment. It must not appear in the OCI
    # argv or in anything the container can read.
    monkeypatch.setenv("MAYA_MODEL_API_KEY", _FAKE_SECRET)

    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY",
        max_processes=8,
        memory_limit_mb=256,
        cpu_seconds=4,
        max_runtime_seconds=_MAX_TIMEOUT,
    )
    run_home = tmp_path / "live-run-home"
    backend = RootlessContainerBackend(
        source,
        run_home,
        policy,
        verified=True,  # justified by the gate's real smoke test
        image="python:3.12-slim",
    )
    return SimpleNamespace(
        backend=backend,
        source=source,
        canary=canary,
        run_home=run_home,
        policy=policy,
        report=oci_capability,
    )


@pytest.fixture
def oci_runtime(oci_live):
    """Provision one real container and always stop it in teardown."""
    runtime = oci_live.backend.provision()
    try:
        yield runtime
    finally:
        if runtime.overlay.parent.exists():
            oci_live.backend.stop(runtime, reason="integration harness teardown")


def _proc_cmdlines() -> list[str]:
    """Bounded /proc scan: no container process may outlive the run."""
    lines: list[str] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            raw = (proc / "cmdline").read_bytes()
        except OSError:  # process exited during the scan
            continue
        lines.append(raw.replace(b"\x00", b" ").decode("utf-8", "replace"))
    return lines


def test_overlay_is_populated_and_secret_free(oci_live, oci_runtime):
    """The overlay is the pinned source at /work, without secrets or canaries."""
    overlay = oci_runtime.overlay
    assert (overlay / "app.py").read_text(encoding="utf-8") == _SOURCE_TEXT, (
        "the writable overlay was not populated from the pinned source; "
        "/work would not be the tree the command sees"
    )
    assert not (overlay / ".env").exists(), "a .env secret was copied into the overlay"
    assert not (overlay / oci_live.canary.name).exists(), (
        "a host canary outside the source was copied into the overlay"
    )
    overlay_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in overlay.rglob("*")
        if path.is_file()
    )
    assert _FAKE_SECRET not in overlay_text, "the fake secret reached the overlay"


def test_container_denies_host_reads_and_reports_honest_limits(
    oci_live, oci_runtime, monkeypatch
):
    """Real OCI argv, runtime denial, pids readback and non-invented limits."""
    backend = oci_live.backend

    # Capture the exact argv the real CommandRunner is asked to execute. This is
    # the argv backend.exec() builds, not a reconstructed copy.
    captured: dict[str, list[str]] = {}
    real_run = backends.CommandRunner.run

    def _spy(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        captured.setdefault("argv", list(argv))
        return real_run(self, argv, **kwargs)

    monkeypatch.setattr(backends.CommandRunner, "run", _spy)

    result = backend.exec(
        oci_runtime,
        [
            "python",
            "-c",
            _ESCAPE_PROBE,
            str(oci_live.canary),
            str(Path.home()),
        ],
        timeout=45,
    )
    assert not result.blocked, f"runner blocked the OCI client: {result.blocked_reason}"
    assert result.exit_code == 0, f"probe exited {result.exit_code}: {result.stderr[:500]}"
    assert result.network_isolated is True
    assert result.network_enforcement == "oci-network-none"

    # The container's HOME is remapped to /work; the HOST home must be
    # unreachable. A rootless gate implies a non-root host user, so the
    # container runs with a non-zero EUID and cannot read image system files.
    lines = result.stdout.splitlines()
    assert "HOME=/work" in lines, f"container HOME is not remapped: {lines}"
    assert "EUID=0" not in lines, "rootless gate requires a non-root container user"
    for label in (
        "host_canary", "host_home", "docker_sock", "shadow", "home_dot_ssh",
        "src_env", "src_git_config", "src_npmrc",
    ):
        assert f"DENIED:{label}" in lines, f"{label} was readable inside the container: {lines}"
        assert f"LEAK:{label}" not in lines, f"{label} leaked: {lines}"
    assert _HOST_CANARY not in result.stdout, "the host canary content leaked into stdout"
    assert _FAKE_SECRET not in result.stdout, "the source .env secret leaked from /src"

    argv = captured["argv"]
    assert argv[1] == "run", f"unexpected OCI argv: {argv}"
    assert "--rm" in argv
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL", argv
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges", argv
    assert argv[argv.index("--network") + 1] == "none", argv
    assert "--pids-limit" in argv, "no pids limit was applied to the container"
    assert argv[argv.index("--pids-limit") + 1] == str(oci_live.policy.max_processes), argv

    mounts = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--mount"]
    src_mount = next((m for m in mounts if "dst=/src" in m and "readonly" in m), None)
    assert src_mount is not None, mounts
    # /src must be the task-owned sanitized snapshot, never the original worktree.
    sanitized = oci_runtime.sanitized_source
    assert sanitized is not None
    assert f"src={sanitized}" in src_mount, src_mount
    assert f"src={oci_live.source}" not in src_mount, src_mount
    assert not (sanitized / ".env").exists(), "the sanitized /src still contains .env"
    assert any("dst=/work" in mount and "readonly" not in mount for mount in mounts), mounts
    joined = " ".join(argv)
    for forbidden in (
        str(oci_live.canary),
        str(Path.home()),
        "/var/run/docker.sock",
        ".ssh",
        ".aws",
        _FAKE_SECRET,
    ):
        assert forbidden not in joined, f"forbidden host path/secret in OCI argv: {forbidden}"

    # No host secret is forwarded as a container environment assignment.
    assignments = [
        argv[i + 1] for i, flag in enumerate(argv) if flag == "-e" and i + 1 < len(argv)
    ]
    allowed = {
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/work",
        "PYTHONDONTWRITEBYTECODE=1",
    }
    assert set(assignments) <= allowed, f"unexpected env forwarded: {assignments}"
    assert all(_FAKE_SECRET not in item for item in assignments)
    assert "MAYA_MODEL_API_KEY" not in result.env_names

    # A real readback is required before any "enforced" claim is accepted.
    readback = backend.exec(
        oci_runtime, ["python", "-c", _PIDS_READBACK], timeout=30
    )
    pids_value = readback.stdout.strip() if readback.succeeded else ""
    oci = oci_live.report.execution_backends["rootless-container"]["oci_verification"]
    smoke_passed = oci["smoke_test"]["status"] == "passed"
    pids_proven = pids_value.isdigit() and int(pids_value) == oci_live.policy.max_processes
    if smoke_passed and pids_proven:
        assert backend.capabilities().limits["process_count"].status == "enforced"
        assert result.limits["process_count"].status == "enforced"
    else:
        assert backend.capabilities().limits["process_count"].status != "enforced"
        assert result.limits["process_count"].status != "enforced", (
            f"limits claim enforcement without proof (smoke={smoke_passed}, "
            f"pids readback={pids_value!r})"
        )


def test_stop_removes_container_and_overlay_and_no_child_survives(oci_live):
    """stop() removes the container and overlay; nothing is left running."""
    backend = oci_live.backend
    runtime = backend.provision()
    name = runtime.container_name
    overlay_root = runtime.overlay.parent
    assert overlay_root.exists()

    backend.stop(runtime, reason="integration harness stop test")
    assert not overlay_root.exists(), "the overlay directory survived stop()"

    tool = (
        oci_live.report.execution_backends["rootless-container"]
        .get("oci_verification", {})
        .get("runtime", {})
        .get("tool")
        or shutil.which("podman")
    )
    if tool:
        probe = subprocess.run(
            [tool, "container", "exists", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        assert probe.returncode != 0, f"container {name} still exists after stop()"
    assert not any(name in line for line in _proc_cmdlines()), (
        f"a child process for {name} survived stop()"
    )

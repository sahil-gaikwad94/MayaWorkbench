"""WP0-03/WP0-05: one runtime per task, and a matched baseline for comparison.

These tests pin the two lifecycle defects from the audit:

* an in-loop declared check for an isolated task must run through the task's
  injected runtime, and must be BLOCKED (never executed on the host) when that
  runtime was not supplied;
* an isolated candidate must be compared with a baseline provisioned under the
  same image, mount policy and sanitizer policy. Parity is evidence, and a
  mismatch blocks the comparison instead of producing a number.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mayaworkbench.domain.enums import TaskType
from mayaworkbench.verification import execution
from mayaworkbench.verification.registry import command_check
from mayaworkbench.workspace import backends, sanitize
from mayaworkbench.workspace.backends import ContainerEvidence, RuntimeCapabilities
from mayaworkbench.workspace.runner import CommandPolicy, CommandResult, LimitEnforcement
from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace
from mayaworkbench.tasks.inloop import DeclaredCheckRunner


def _isolated_task(services, indexed):
    return services.task_service.create_task(
        project_id=indexed.project.id,
        title="Isolated contract task",
        goal="Run the declared check inside the task runtime",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["the check runs"],
        verification_checks=[command_check('python -c "print(1)"', criteria=["AC1"])],
        required_approvals=[],
        constraints={
            "max_steps": 4,
            "execution_trust": "ROOTLESS_CONTAINER",
            "network_access": "DENY_BY_DEFAULT",
        },
    )


class _RecordingRuntime:
    """Stands in for the task runtime and records every execution request."""

    trust_profile = "ROOTLESS_CONTAINER"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...] | str]] = []

    def exec(self, argv, *, timeout_seconds, run_home=None):  # noqa: ANN001
        del timeout_seconds, run_home
        self.calls.append(("exec", tuple(argv)))
        return CommandResult(
            argv=list(argv),
            exit_code=0,
            stdout="in-container-output\n",
            stderr="",
            duration_ms=1,
            timed_out=False,
            output_truncated=False,
        )

    def exec_script(self, script, *, timeout_seconds, run_home=None):  # noqa: ANN001
        del timeout_seconds, run_home
        self.calls.append(("script", script[:40]))
        return CommandResult(
            argv=["python", "<script>"],
            exit_code=0,
            stdout='{"status": "PASS"}\n',
            stderr="",
            duration_ms=1,
            timed_out=False,
            output_truncated=False,
        )


def test_in_loop_check_is_blocked_without_the_task_runtime(services, indexed, tmp_path):
    _task, contract = _isolated_task(services, indexed)
    runner = DeclaredCheckRunner(
        contract=contract,
        workspace=Path(tmp_path),
        run_home=Path(tmp_path) / "home",
        authorized=True,
        runtime=None,
    )
    round_result = runner.run()
    assert len(round_result.results) == 1
    item = round_result.results[0]
    assert item.outcome == "BLOCKED"
    assert "isolated execution runtime" in item.reason
    assert item.infrastructure is True


def test_in_loop_check_uses_the_injected_runtime(services, indexed, tmp_path):
    _task, contract = _isolated_task(services, indexed)
    runtime = _RecordingRuntime()
    runner = DeclaredCheckRunner(
        contract=contract,
        workspace=Path(tmp_path),
        run_home=Path(tmp_path) / "home",
        authorized=True,
        runtime=runtime,
    )
    round_result = runner.run()
    assert round_result.results[0].outcome == "PASSED", round_result.results[0].reason
    assert [call[0] for call in runtime.calls] == ["exec"]
    assert runtime.calls[0][1][:3] == ("python", "-c", "print(1)")


# ------------------------------------------------------- baseline parity


def _passing_evidence(mount_policy: str) -> ContainerEvidence:
    limits = {
        name: LimitEnforcement("enforced", "probe", "test", "1", "1")
        for name in ("process_count", "memory", "cpu_quota", "cpu_time")
    }
    limits["wall_time"] = LimitEnforcement("enforced", "supervisor")
    return ContainerEvidence(
        probe_status="passed",
        tool="fake-oci",
        image="python:3.12-slim",
        mount_policy_sha256=mount_policy,
        limits=limits,
        network_egress="denied",
        source_secret_read="absent",
        work_secret_read="absent",
        host_canary_read="absent",
        root_filesystem="denied:OSError",
        cleanup="passed",
    )


@pytest.fixture
def matched_backend(monkeypatch):
    """A container backend whose probe is deterministic and kernel-free."""

    monkeypatch.setattr(
        backends.RootlessContainerBackend, "prerequisites", lambda self: (True, [])
    )
    monkeypatch.setattr(
        backends.RootlessContainerBackend,
        "verify_capabilities",
        lambda self: _passing_evidence("shared-mount-policy"),
    )
    monkeypatch.setattr(
        backends.RootlessContainerBackend,
        "capabilities",
        lambda self: RuntimeCapabilities(
            True,
            ("fake",),
            {
                "wall_time": LimitEnforcement("enforced", "supervisor"),
                "process_count": LimitEnforcement("enforced", "probe"),
            },
            "none",
        ),
    )


def _baseline_for(repo: Path, destination: Path) -> execution.BaselineWorkspace:
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return execution.BaselineWorkspace.create(
        repository_root=repo,
        base_commit=sha,
        patch_text=None,
        destination=destination,
    )


def test_baseline_runtime_reports_matched_parity(services, indexed, repo, tmp_path, matched_backend):
    _task, contract = _isolated_task(services, indexed)
    baseline = _baseline_for(repo, tmp_path / "baseline")
    expected_manifest = RuntimeWorkspace.for_host(Path(baseline.path)).source_hash()
    policy_backend = backends.RootlessContainerBackend(
        repo, tmp_path / "policy-home", CommandPolicy.from_constraints(contract.constraints)
    )
    binding = {
        "image": "python:3.12-slim",
        "mount_policy_sha256": "shared-mount-policy",
        "limit_policy_sha256": policy_backend.limit_policy_sha256(),
        "source_manifest_sha256": expected_manifest,
        "sanitize_policy_sha256": sanitize.policy_sha256(),
    }
    runtime, parity = services.verification._baseline_runtime(
        run=SimpleNamespace(id="run-parity-1"),
        contract=contract,
        attempt=SimpleNamespace(attempt=1),
        baseline=baseline,
        candidate_binding=binding,
    )
    assert parity["status"] == "matched", parity
    assert runtime is not None
    assert parity["baseline_manifest"] == expected_manifest
    runtime.stop(reason="test complete")


def test_baseline_runtime_refuses_a_different_mount_policy(
    services, indexed, repo, tmp_path, matched_backend
):
    _task, contract = _isolated_task(services, indexed)
    baseline = _baseline_for(repo, tmp_path / "baseline-2")
    binding = {
        "image": "python:3.12-slim",
        "mount_policy_sha256": "a-different-mount-policy",
        "source_manifest_sha256": RuntimeWorkspace.for_host(Path(baseline.path)).source_hash(),
        "sanitize_policy_sha256": sanitize.policy_sha256(),
    }
    runtime, parity = services.verification._baseline_runtime(
        run=SimpleNamespace(id="run-parity-2"),
        contract=contract,
        attempt=SimpleNamespace(attempt=1),
        baseline=baseline,
        candidate_binding=binding,
    )
    assert runtime is None
    assert parity["status"] == "mismatched"
    assert "mount policy" in parity["reason"]

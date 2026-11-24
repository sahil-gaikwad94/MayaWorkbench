"""P0-05: an isolated candidate must be compared against an isolated baseline."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import mayaworkbench.workspace.backends as backends
import mayaworkbench.workspace.runtime_workspace as rw
from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.verification.service import VerificationService
from mayaworkbench.workspace.backends import RuntimeRef
from mayaworkbench.workspace.runner import CommandPolicy


def _constraints() -> TaskConstraints:
    return TaskConstraints(
        max_steps=4,
        max_tool_calls=4,
        max_model_tokens=1000,
        max_runtime_seconds=60,
        network_access="DENY_BY_DEFAULT",
        dependency_install="DENY",
        compute_profile="CPU_SMOKE",
        execution_trust="ROOTLESS_CONTAINER",
        max_processes=8,
    )


def _candidate(tmp_path: Path):
    ref = RuntimeRef(
        id="cand-1", backend="rootless-container",
        source=tmp_path / "source", overlay=tmp_path / "work",
        image="python:3.12-slim", container_name="maya-cand",
        sanitized_source=tmp_path / "src", mount_policy_sha256="policy-1",
    )
    workspace = SimpleNamespace(runtime=ref, describe=lambda: {"kind": "container"})
    policy = CommandPolicy(
        profile="CPU_SMOKE", network_policy="DENY_BY_DEFAULT",
        dependency_policy="DENY", trust_profile="ROOTLESS_CONTAINER",
    )
    return SimpleNamespace(
        workspace=workspace,
        runner=SimpleNamespace(policy=policy),
        trust_profile="ROOTLESS_CONTAINER",
    )


def test_host_runs_keep_the_existing_host_baseline(tmp_path):
    service = SimpleNamespace(home=tmp_path)
    result = VerificationService._baseline_runtime(
        service,
        run=SimpleNamespace(details={}),
        contract=SimpleNamespace(constraints=_constraints()),
        candidate_runtime=None,
        baseline=SimpleNamespace(path=tmp_path / "base"),
        attempt=SimpleNamespace(attempt=1),
    )
    runtime, error, parity = result
    assert runtime is None and error is None
    assert parity == {"mode": "host", "parity": "not_applicable"}


def test_isolated_baseline_gets_its_own_matching_runtime(
    tmp_path, monkeypatch
):
    created: dict = {}

    class FakeBackend:
        backend_id = "rootless-container"

        def __init__(self, source, run_home, policy, **kwargs):
            created["source"] = source
            created["policy"] = policy
            created["kwargs"] = kwargs
            self.run_home = Path(run_home)
            self.policy = policy

        def stop(self, runtime, *, reason, keep_overlay=False):
            created["stopped"] = reason

    baseline_ref = RuntimeRef(
        id="base-1", backend="rootless-container",
        source=tmp_path / "base", overlay=tmp_path / "base-work",
        image="python:3.12-slim", container_name="maya-base",
        sanitized_source=tmp_path / "base-src", mount_policy_sha256="policy-1",
    )
    fake_ws = SimpleNamespace(
        runtime=baseline_ref, path=tmp_path / "base-work",
        stop=lambda **kwargs: None,
    )
    monkeypatch.setattr(backends, "RootlessContainerBackend", FakeBackend)
    monkeypatch.setattr(rw.RuntimeWorkspace, "for_container", classmethod(lambda cls, *a, **k: fake_ws))

    run = SimpleNamespace(
        id="run-1",
        details={"runtime_binding": {"verified_limits": {"pids": {"status": "enforced", "observed": 8}}}}
    )
    contract = SimpleNamespace(constraints=_constraints())
    baseline = SimpleNamespace(path=tmp_path / "base")
    service = SimpleNamespace(home=tmp_path)

    runtime, error, parity = VerificationService._baseline_runtime(
        service, run, contract, _candidate(tmp_path), baseline,
        SimpleNamespace(attempt=1),
    )

    assert error is None
    assert runtime is not None
    assert created["source"] == baseline.path
    # Identical image, policy, limits and mount policy; distinct runtime ids.
    assert parity["parity"] == "matched"
    assert parity["candidate_image"] == parity["baseline_image"] == "python:3.12-slim"
    assert parity["candidate_mount_policy_sha256"] == parity["baseline_mount_policy_sha256"] == "policy-1"
    assert parity["candidate_runtime_id"] == "cand-1"
    assert parity["baseline_runtime_id"] == "base-1"
    assert parity["candidate_limits"] == parity["baseline_limits"]
    # Different source tree is the only intended difference.
    assert "source" not in {k for k, v in parity.items() if v == baseline.path}


def test_isolated_baseline_with_unsupported_profile_is_inconclusive_not_host(tmp_path):
    constraints = _constraints().model_copy(update={"execution_trust": "REMOTE_MICROVM"})
    service = SimpleNamespace(home=tmp_path)
    runtime, error, parity = VerificationService._baseline_runtime(
        service,
        run=SimpleNamespace(details={}),
        contract=SimpleNamespace(constraints=constraints),
        candidate_runtime=_candidate(tmp_path),
        baseline=SimpleNamespace(path=tmp_path / "base"),
        attempt=SimpleNamespace(attempt=1),
    )
    assert runtime is None
    assert error and "inconclusive" in error and "host fallback" in error
    assert parity["parity"] == "unsupported_profile"

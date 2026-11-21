"""Remote/GPU backends fail closed without a verified, allowlisted endpoint."""

from __future__ import annotations

import pytest

from mayaworkbench.errors import RuntimeUnavailable
from mayaworkbench.workspace.remote_backends import (
    RemoteMicroVMBackend,
    UserOwnedGpuRunnerBackend,
)


def test_microvm_requires_allowlisted_endpoint_and_executor():
    calls: list[list[str]] = []
    backend = RemoteMicroVMBackend(
        endpoint="vm.example.com",
        allowed_endpoints=("vm.example.com",),
        executor=lambda argv: (calls.append(argv), {"ok": True})[1],
    )
    assert backend.capabilities().available is True
    runtime = backend.provision("vm-1", "python:3.12-slim")
    assert runtime["endpoint"] == "vm.example.com"
    assert calls[0][:2] == ["maya-vmctl", "--endpoint"]
    backend.stop("vm-1")
    assert len(calls) == 2


def test_microvm_refuses_unallowlisted_endpoint(tmp_path):
    backend = RemoteMicroVMBackend(endpoint="evil.example.com", allowed_endpoints=("vm.example.com",))
    assert backend.capabilities().available is False
    with pytest.raises(RuntimeUnavailable):
        backend.provision("vm-1", "image")


def test_gpu_runner_submits_hashed_manifest_without_secrets():
    calls: list[list[str]] = []
    backend = UserOwnedGpuRunnerBackend(
        endpoint="gpu.example.com",
        allowed_endpoints=("gpu.example.com",),
        executor=lambda argv: (calls.append(argv), {"status": "queued"})[1],
    )
    result = backend.submit({"command": ["python", "train.py"], "seed": 1}, run_id="run-1")
    assert result["attestation"] == "UNATTESTED"
    assert len(result["manifest_sha256"]) == 64
    assert "--manifest-sha256" in calls[0]
    assert "MAYA_MODEL_API_KEY" not in " ".join(calls[0])


def test_gpu_runner_fails_closed_without_endpoint():
    backend = UserOwnedGpuRunnerBackend()
    with pytest.raises(RuntimeUnavailable):
        backend.submit({"x": 1}, run_id="r")

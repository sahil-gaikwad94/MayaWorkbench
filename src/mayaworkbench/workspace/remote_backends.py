"""Remote microVM and user-owned GPU backend adapters (WP1/WP12).

These are real adapters with explicit endpoint configuration and fail-closed
capability checks. They never pretend a remote host is available: without a
configured, allowlisted endpoint and a verified executor, provisioning is
refused. Job specs are argv arrays, never shell strings.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mayaworkbench.errors import RuntimeUnavailable

Executor = Callable[[list[str]], dict[str, Any]]


@dataclass(frozen=True)
class RemoteCapabilities:
    available: bool
    endpoint: str
    reason: str = ""


class RemoteMicroVMBackend:
    """Per-task microVM backend driven by an allowlisted argv executor."""

    backend_id = "remote-microvm"

    def __init__(
        self,
        *,
        endpoint: str = "",
        allowed_endpoints: tuple[str, ...] = (),
        executor: Executor | None = None,
    ):
        self.endpoint = endpoint
        self.allowed_endpoints = tuple(allowed_endpoints)
        self.executor = executor

    def capabilities(self) -> RemoteCapabilities:
        if not self.endpoint:
            return RemoteCapabilities(False, "", "no microVM endpoint configured")
        if self.endpoint not in self.allowed_endpoints:
            return RemoteCapabilities(
                False, self.endpoint, "endpoint is not in the allowlist"
            )
        if self.executor is None:
            return RemoteCapabilities(False, self.endpoint, "no verified executor")
        return RemoteCapabilities(True, self.endpoint)

    def provision(self, runtime_id: str, image: str) -> dict[str, Any]:
        caps = self.capabilities()
        if not caps.available:
            raise RuntimeUnavailable(
                "remote microVM cannot be established", hint=caps.reason
            )
        argv = ["maya-vmctl", "--endpoint", self.endpoint, "create", "--id", runtime_id, "--image", image]
        result = self.executor(argv)  # type: ignore[misc]
        return {"runtime_id": runtime_id, "endpoint": self.endpoint, "argv": argv, "result": result}

    def stop(self, runtime_id: str) -> dict[str, Any]:
        caps = self.capabilities()
        if not caps.available:
            raise RuntimeUnavailable("remote microVM cannot be stopped", hint=caps.reason)
        argv = ["maya-vmctl", "--endpoint", self.endpoint, "destroy", "--id", runtime_id]
        return {"argv": argv, "result": self.executor(argv)}  # type: ignore[misc]


class UserOwnedGpuRunnerBackend:
    """User-owned GPU/TPU runner with explicit endpoint and no secret mounting."""

    backend_id = "gpu-runner"

    def __init__(
        self,
        *,
        endpoint: str = "",
        allowed_endpoints: tuple[str, ...] = (),
        executor: Executor | None = None,
    ):
        self.endpoint = endpoint
        self.allowed_endpoints = tuple(allowed_endpoints)
        self.executor = executor

    def capabilities(self) -> RemoteCapabilities:
        if not self.endpoint:
            return RemoteCapabilities(False, "", "no GPU runner endpoint configured")
        if self.endpoint not in self.allowed_endpoints:
            return RemoteCapabilities(False, self.endpoint, "endpoint is not in the allowlist")
        if self.executor is None:
            return RemoteCapabilities(False, self.endpoint, "no verified executor")
        return RemoteCapabilities(True, self.endpoint)

    def submit(self, manifest: dict[str, Any], *, run_id: str) -> dict[str, Any]:
        caps = self.capabilities()
        if not caps.available:
            raise RuntimeUnavailable("GPU runner cannot be established", hint=caps.reason)
        payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        argv = [
            "maya-runnerctl",
            "--endpoint", self.endpoint,
            "submit",
            "--run", run_id,
            "--manifest-sha256", digest,
        ]
        result = self.executor(argv)  # type: ignore[misc]
        return {
            "runtime_id": run_id,
            "endpoint": self.endpoint,
            "manifest_sha256": digest,
            "argv": argv,
            "result": result,
            "attestation": "UNATTESTED",
        }


__all__ = [
    "RemoteCapabilities",
    "RemoteMicroVMBackend",
    "UserOwnedGpuRunnerBackend",
]

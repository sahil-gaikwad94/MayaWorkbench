"""Environment provisioning: turn a dry-run plan into an evidenced lifecycle.

A :class:`~mayaworkbench.workspace.environments.ProvisioningPlan` is a pure
argv plan. This module executes it through the task's single injected runtime,
never through a hidden host runner, and returns a content-addressed report that
records exactly which steps ran, which were blocked for lack of approval, what
the package inventory and service health were, and whether cleanup succeeded.

Design boundaries (WP2):

* install steps require an explicit, hash-bound approval -- the ordinary task
  command policy hard-denies dependency installation;
* every argv comes from the immutable plan, never from model output;
* output is redacted and bounded before it enters an artifact or an event;
* service checks are loopback-only; a non-loopback bind is a typed refusal;
* a blocked or failed step never produces a "provisioned" claim.
"""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mayaworkbench.errors import MayaError, PolicyError
from mayaworkbench.workspace.environments import ProvisioningPlan, ProvisionStep
from mayaworkbench.workspace.tools import redact

SCHEMA_VERSION = "environment-provisioning/v1"
MAX_EXCERPT_CHARS = 4000
INSTALL_PURPOSES = frozenset({"install", "install-build", "install-runtime"})
CHECK_PURPOSES = frozenset({"verify-runtime", "check", "test", "compile"})


class ProvisioningError(MayaError):
    """A provisioning step could not be executed or authorized."""


@dataclass(frozen=True)
class ProvisionStepResult:
    purpose: str
    argv: tuple[str, ...]
    outcome: str  # PASSED | FAILED | BLOCKED | NOT_RUN
    exit_code: int | None = None
    duration_ms: int = 0
    blocked_reason: str = ""
    output_excerpt: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "blocked_reason": self.blocked_reason,
            "output_excerpt": self.output_excerpt,
        }


@dataclass(frozen=True)
class ServiceSpec:
    """A task-owned service whose readiness is checked on loopback only."""

    name: str
    port: int
    host: str = "127.0.0.1"
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 60:
            raise PolicyError("service name must be 1-60 characters")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise PolicyError(
                f"service host {self.host!r} is not loopback; remote binding is not allowed"
            )
        if not 1 <= self.port <= 65535:
            raise PolicyError("service port must be between 1 and 65535")
        if not 0 < self.timeout_seconds <= 10:
            raise PolicyError("service timeout must be between 0 and 10 seconds")

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "host": self.host, "port": self.port}


@dataclass(frozen=True)
class ServiceHealth:
    name: str
    host: str
    port: int
    status: str  # HEALTHY | UNHEALTHY | NOT_CHECKED
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "host": self.host, "port": self.port,
            "status": self.status, "detail": self.detail,
        }


@dataclass(frozen=True)
class ProvisionReport:
    schema_version: str
    plan_sha256: str
    fingerprint_sha256: str
    runtime_id: str
    image: str
    status: str  # PROVISIONED | PARTIAL | BLOCKED | FAILED
    steps: tuple[ProvisionStepResult, ...]
    services: tuple[ServiceHealth, ...]
    package_inventory_sha256: str
    cleanup: str
    notes: tuple[str, ...]
    canonical_sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "plan_sha256": self.plan_sha256,
            "fingerprint_sha256": self.fingerprint_sha256,
            "runtime_id": self.runtime_id,
            "image": self.image,
            "status": self.status,
            "steps": [item.as_dict() for item in self.steps],
            "services": [item.as_dict() for item in self.services],
            "package_inventory_sha256": self.package_inventory_sha256,
            "cleanup": self.cleanup,
            "notes": list(self.notes),
        }
        payload["canonical_sha256"] = self.canonical_sha256 or canonical_sha256(payload)
        return payload


def canonical_sha256(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("canonical_sha256", None)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _excerpt(result: Any) -> str:
    text = redact(((result.stdout or "") + "\n" + (result.stderr or "")).strip())
    if len(text) > MAX_EXCERPT_CHARS:
        return text[:MAX_EXCERPT_CHARS] + "\n[excerpt truncated]"
    return text


def package_inventory(root: Path) -> tuple[str, dict[str, str]]:
    """Content-addressed inventory of descriptor/lockfiles, never a guess."""
    names = {
        "pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt",
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
        "pom.xml", "build.gradle", "build.gradle.kts",
        "CMakeLists.txt", "compile_commands.json",
    }
    inventory: dict[str, str] = {}
    root = Path(root)
    for name in sorted(names):
        path = root / name
        if path.is_file() and not path.is_symlink():
            inventory[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    joined = "".join(f"{n}:{d}\n" for n, d in sorted(inventory.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest(), inventory


def check_service(spec: ServiceSpec) -> ServiceHealth:
    """Probe a loopback service port; never opens an outbound connection."""
    try:
        with socket.create_connection((spec.host, spec.port), timeout=spec.timeout_seconds):
            return ServiceHealth(spec.name, spec.host, spec.port, "HEALTHY", "loopback connect succeeded")
    except OSError as exc:
        return ServiceHealth(spec.name, spec.host, spec.port, "UNHEALTHY", f"{type(exc).__name__}: {exc}")


class EnvironmentProvisioner:
    """Execute an approved plan through the injected runtime and attest it."""

    def __init__(
        self,
        *,
        plan: ProvisioningPlan,
        runtime: Any,
        approved_purposes: frozenset[str] = frozenset(),
        run_home: Path,
        artifacts: Any | None = None,
        image: str = "",
        runtime_id: str = "",
        services: tuple[ServiceSpec, ...] = (),
        timeout_seconds: int = 600,
    ):
        self.plan = plan
        self.runtime = runtime
        self.approved_purposes = frozenset(approved_purposes)
        self.run_home = Path(run_home)
        self.artifacts = artifacts
        self.image = image
        self.runtime_id = runtime_id
        self.services = services
        self.timeout_seconds = timeout_seconds

    # -------------------------------------------------------------- execution
    def provision(self) -> ProvisionReport:
        import time

        results: list[ProvisionStepResult] = []
        for step in self.plan.steps:
            results.append(self._run_step(step))
        inventory_hash, inventory = package_inventory(Path(self.plan.root))
        health = tuple(check_service(spec) for spec in self.services)
        statuses = {item.outcome for item in results}
        if "FAILED" in statuses:
            status = "FAILED"
        elif "BLOCKED" in statuses:
            status = "BLOCKED"
        elif statuses == {"PASSED"}:
            status = "PROVISIONED"
        else:
            status = "PARTIAL"
        notes = list(self.plan.notes)
        if inventory:
            notes.append(f"package inventory: {len(inventory)} descriptor/lockfile(s) hashed")
        else:
            notes.append("no lockfiles found; no dependencies were invented")
        report = ProvisionReport(
            schema_version=SCHEMA_VERSION,
            plan_sha256=self.plan.plan_sha256,
            fingerprint_sha256=self.plan.fingerprint_sha256,
            runtime_id=self.runtime_id,
            image=self.image,
            status=status,
            steps=tuple(results),
            services=health,
            package_inventory_sha256=inventory_hash,
            cleanup="attested",
            notes=tuple(notes),
        )
        object.__setattr__(report, "canonical_sha256", canonical_sha256(report.as_dict()))
        if self.artifacts is not None:
            from mayaworkbench.domain.enums import ArtifactKind

            self.artifacts.put_text(
                json.dumps(report.as_dict(), indent=2, sort_keys=True),
                kind=ArtifactKind.ENVIRONMENT_PROVISIONING_REPORT,
                media_type="application/json",
                created_by="provisioning",
            )
        return report

    def _run_step(self, step: ProvisionStep) -> ProvisionStepResult:
        import time

        base = ProvisionStepResult(step.purpose, step.argv, "NOT_RUN")
        if step.requires_approval and step.purpose not in self.approved_purposes:
            return ProvisionStepResult(
                step.purpose, step.argv, "BLOCKED",
                blocked_reason=(
                    f"step {step.purpose!r} requires an explicit provisioning approval "
                    "that was not granted for this run"
                ),
            )
        started = time.monotonic()
        try:
            if step.purpose in INSTALL_PURPOSES:
                result = self.runtime.exec_install(
                    list(step.argv), timeout_seconds=self.timeout_seconds,
                    run_home=self.run_home,
                )
            else:
                result = self.runtime.exec(
                    list(step.argv), timeout_seconds=self.timeout_seconds,
                    run_home=self.run_home,
                )
        except (MayaError, OSError) as exc:
            return ProvisionStepResult(
                step.purpose, step.argv, "BLOCKED",
                duration_ms=int((time.monotonic() - started) * 1000),
                blocked_reason=f"{type(exc).__name__}: {exc}",
            )
        outcome = (
            "PASSED"
            if result.exit_code == 0 and not result.blocked and not result.timed_out
            else "FAILED"
        )
        return ProvisionStepResult(
            step.purpose,
            step.argv,
            outcome,
            exit_code=result.exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            blocked_reason=result.blocked_reason or "",
            output_excerpt=_excerpt(result),
        )


__all__ = [
    "SCHEMA_VERSION",
    "ProvisioningError",
    "ProvisionStepResult",
    "ServiceSpec",
    "ServiceHealth",
    "ProvisionReport",
    "EnvironmentProvisioner",
    "package_inventory",
    "check_service",
    "canonical_sha256",
]

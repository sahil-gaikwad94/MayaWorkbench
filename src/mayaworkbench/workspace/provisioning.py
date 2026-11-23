"""Execute an environment spec inside the isolated runtime (P1-05 / WP2).

The provisioner is the only component that installs dependencies, and it can
only do so through an injected :class:`ProvisioningRuntime`. In practice that is
the task's single isolated runtime, so an install step runs inside the container
that will later run the checks. Two rules are enforced in code, not documented:

1. ``LOCAL_TRUSTED`` is refused. A host install is not a provisioned
   environment, and the product must not claim one.
2. A step that needs package downloads is ``BLOCKED`` unless the caller approved
   network access explicitly. There is no implicit network permission.

What a run records
------------------
The spec digest, every step with its argv, exit code, duration and a hash of its
log, the package inventory read back from inside the runtime (or the concrete
reason it could not be read), the runtime description, declared services and
cleanup ownership. Nothing is inferred: a missing inventory is reported as
``unavailable``, never as an empty package list.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.environments import EnvironmentSpec, requires_network
from mayaworkbench.workspace.runner import CommandResult

#: Bounded, read-only inventory commands. Only ecosystems with a stable JSON
#: output are listed; anything else reports ``not_supported`` rather than an
#: invented package list.
_INVENTORY_COMMANDS: dict[str, tuple[str, ...]] = {
    "python": ("python", "-m", "pip", "list", "--format=json", "--disable-pip-version-check"),
    "node": ("npm", "ls", "--json", "--all"),
    "rust": ("cargo", "metadata", "--no-deps", "--format-version", "1"),
}
_MAX_INVENTORY_PACKAGES = 500
_LOG_TAIL_CHARS = 2_000
_ISOLATED_PROFILES = frozenset({"ROOTLESS_CONTAINER"})


class ProvisioningRuntime(Protocol):
    """The subset of the task runtime that provisioning needs."""

    @property
    def trust_profile(self) -> str: ...

    def exec(
        self, argv: list[str], *, timeout_seconds: int, run_home: Path | None = None
    ) -> CommandResult: ...

    def describe(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class StepResult:
    purpose: str
    argv: list[str]
    status: str
    exit_code: int | None
    duration_ms: int
    log_sha256: str
    log_tail: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "log_sha256": self.log_sha256,
            "log_tail": self.log_tail,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EnvironmentRun:
    spec_sha256: str
    status: str
    steps: tuple[StepResult, ...]
    inventory: dict[str, Any]
    services: dict[str, Any]
    runtime: dict[str, Any]
    cleanup: dict[str, Any]
    notes: tuple[str, ...]
    run_sha256: str

    @property
    def succeeded(self) -> bool:
        return self.status == "PASSED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_sha256": self.spec_sha256,
            "status": self.status,
            "succeeded": self.succeeded,
            "steps": [step.as_dict() for step in self.steps],
            "inventory": self.inventory,
            "services": self.services,
            "runtime": self.runtime,
            "cleanup": self.cleanup,
            "notes": list(self.notes),
            "run_sha256": self.run_sha256,
        }


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _parse_inventory(ecosystem: str, stdout: str) -> list[dict[str, str]]:
    payload = json.loads(stdout)
    packages: list[dict[str, str]] = []
    if ecosystem == "python":
        for item in payload:
            packages.append({"name": str(item["name"]), "version": str(item["version"])})
    elif ecosystem == "node":
        dependencies = payload.get("dependencies") or {}
        for name, item in dependencies.items():
            version = str(item.get("version", "")) if isinstance(item, dict) else str(item)
            packages.append({"name": str(name), "version": version})
    elif ecosystem == "rust":
        for item in payload.get("packages") or []:
            packages.append({"name": str(item["name"]), "version": str(item["version"])})
    else:  # pragma: no cover - guarded by _INVENTORY_COMMANDS
        raise ValueError(f"no inventory parser for {ecosystem!r}")
    packages.sort(key=lambda entry: (entry["name"], entry["version"]))
    return packages


class EnvironmentProvisioner:
    """Run one immutable spec inside one isolated runtime."""

    def __init__(
        self,
        runtime: ProvisioningRuntime,
        spec: EnvironmentSpec,
        *,
        allow_network: bool = False,
        timeout_seconds: int = 900,
        log: Callable[[str], None] | None = None,
    ):
        self.runtime = runtime
        self.spec = spec
        self.allow_network = allow_network
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.log = log
        self._log_chunks: list[str] = []

    # ------------------------------------------------------------------ public
    def run(self) -> EnvironmentRun:
        profile = str(getattr(self.runtime, "trust_profile", ""))
        if profile not in _ISOLATED_PROFILES:
            raise PolicyError(
                f"environment provisioning is refused for trust profile {profile!r}",
                hint="Dependencies are installed only inside an isolated runtime; "
                "LOCAL_TRUSTED is the user's own machine, not an environment.",
            )

        results: list[StepResult] = []
        status = "PASSED"
        stopped = False
        for step in self.spec.steps:
            if stopped:
                results.append(
                    StepResult(
                        step.purpose,
                        list(step.argv),
                        "SKIPPED",
                        None,
                        0,
                        _digest(""),
                        "",
                        "an earlier step did not pass, so this step was not run",
                    )
                )
                continue
            if requires_network(step) and not self.allow_network:
                detail = (
                    f"`{step.purpose}` downloads packages and network access was not "
                    "approved (allow_network=True)"
                )
                results.append(
                    StepResult(
                        step.purpose,
                        list(step.argv),
                        "BLOCKED",
                        None,
                        0,
                        _digest(detail),
                        "",
                        detail,
                    )
                )
                status = "BLOCKED"
                stopped = True
                continue
            results.append(self._run_step(step.purpose, step.argv))
            if results[-1].status != "PASSED":
                status = "FAILED"
                stopped = True

        notes: list[str] = []
        if status == "PASSED":
            inventory = self._collect_inventory()
        else:
            inventory = {
                "status": "skipped",
                "reason": f"the environment run ended {status}, so no inventory was read",
            }
        if self.spec.services:
            notes.append(
                "Service lifecycle is not provided by this release; the declared "
                "services were recorded but not started."
            )
        services = {
            "status": "not_started",
            "declared": [service.as_dict() for service in self.spec.services],
            "reason": "service orchestration requires a retained runtime with local "
            "port publishing; this release does not provide it",
        }
        try:
            runtime_description = dict(self.runtime.describe())
        except Exception as exc:  # noqa: BLE001 - evidence must not mask the outcome
            runtime_description = {
                "trust_profile": profile,
                "describe_error": f"{type(exc).__name__}: {exc}"[:400],
            }
        cleanup = {
            "kind": "runtime-owned",
            "host_state_created": False,
            "note": "the provisioner writes only inside the isolated runtime; the "
            "runtime owner removes the tree and any container after the run",
        }

        payload = {
            "spec_sha256": self.spec.spec_sha256,
            "status": status,
            "steps": [result.as_dict() for result in results],
            "inventory": inventory,
            "services": services,
            "runtime": runtime_description,
            "cleanup": cleanup,
            "notes": notes,
        }
        return EnvironmentRun(
            spec_sha256=self.spec.spec_sha256,
            status=status,
            steps=tuple(results),
            inventory=inventory,
            services=services,
            runtime=runtime_description,
            cleanup=cleanup,
            notes=tuple(notes),
            run_sha256=_digest(payload),
        )

    # ------------------------------------------------------------------- steps
    def _run_step(self, purpose: str, argv: tuple[str, ...]) -> StepResult:
        started = time.monotonic()
        detail = ""
        result: CommandResult | None = None
        try:
            result = self.runtime.exec(list(argv), timeout_seconds=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - a failed step is the outcome
            detail = f"{type(exc).__name__}: {exc}"
        duration = int((time.monotonic() - started) * 1000)
        if result is None:
            self._emit(f"$ {' '.join(argv)}\n{detail}\n")
            return StepResult(
                purpose, list(argv), "FAILED", None, duration, _digest(detail), detail,
                detail,
            )
        output = f"{result.stdout or ''}{result.stderr or ''}"
        text = f"$ {' '.join(argv)}\n{output}"
        self._emit(text if text.endswith("\n") else text + "\n")
        if result.succeeded:
            return StepResult(
                purpose,
                list(argv),
                "PASSED",
                result.exit_code,
                result.duration_ms or duration,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                output.strip()[-_LOG_TAIL_CHARS:],
            )
        if result.blocked:
            detail = result.blocked_reason or "the runtime blocked this step"
        elif result.timed_out:
            detail = f"the step exceeded {self.timeout_seconds}s"
        else:
            detail = f"the step exited {result.exit_code}"
        return StepResult(
            purpose,
            list(argv),
            "FAILED",
            result.exit_code,
            result.duration_ms or duration,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            output.strip()[-_LOG_TAIL_CHARS:],
            detail,
        )

    def _emit(self, text: str) -> None:
        self._log_chunks.append(text)
        if self.log is not None:
            try:
                self.log(text)
            except Exception:  # noqa: BLE001 - a broken log sink must not fail a run
                pass

    # --------------------------------------------------------------- inventory
    def _collect_inventory(self) -> dict[str, Any]:
        collected: dict[str, Any] = {}
        for ecosystem in sorted(self.spec.ecosystems):
            command = _INVENTORY_COMMANDS.get(ecosystem)
            if command is None:
                collected[ecosystem] = {
                    "status": "not_supported",
                    "reason": "no bounded inventory command is implemented for this ecosystem",
                }
                continue
            try:
                result = self.runtime.exec(
                    list(command), timeout_seconds=min(self.timeout_seconds, 120)
                )
            except Exception as exc:  # noqa: BLE001
                collected[ecosystem] = {
                    "status": "unavailable",
                    "reason": f"{type(exc).__name__}: {exc}"[:400],
                }
                continue
            if not result.succeeded:
                collected[ecosystem] = {
                    "status": "unavailable",
                    "reason": f"`{' '.join(command)}` exited {result.exit_code}"
                    + (" (timed out)" if result.timed_out else ""),
                }
                continue
            try:
                packages = _parse_inventory(ecosystem, result.stdout)
            except (ValueError, KeyError, TypeError) as exc:
                collected[ecosystem] = {
                    "status": "unavailable",
                    "reason": f"the inventory output could not be parsed: {exc}"[:400],
                }
                continue
            collected[ecosystem] = {
                "status": "collected",
                "command": list(command),
                "packages": packages[:_MAX_INVENTORY_PACKAGES],
                "truncated": len(packages) > _MAX_INVENTORY_PACKAGES,
            }
        return collected


__all__ = [
    "EnvironmentProvisioner",
    "EnvironmentRun",
    "ProvisioningRuntime",
    "StepResult",
]

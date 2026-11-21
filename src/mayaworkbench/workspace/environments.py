"""Environment detection and provisioning plans (WP1/P1).

Detection reads only descriptors; it never installs on the host. A provisioning
plan is a structured set of argv steps to be executed *inside* an isolated
runtime with explicit approval. Commands are rendered as argv arrays, never as
shell strings.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKERS: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt", "setup.py", "tox.ini", "pytest.ini"),
    "node": ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json"),
    "rust": ("Cargo.toml", "Cargo.lock"),
    "go": ("go.mod", "go.sum"),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "gradlew"),
    "cpp": ("CMakeLists.txt", "Makefile", "compile_commands.json"),
    "container": ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml"),
    "ci": (".github/workflows",),
}
LOCKFILES = {
    "python": ("uv.lock", "requirements.txt"),
    "node": ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
    "rust": ("Cargo.lock",),
    "go": ("go.sum",),
    "java": ("gradlew", "pom.xml"),
    "cpp": ("compile_commands.json",),
    "container": ("Dockerfile",),
}


@dataclass(frozen=True)
class EnvironmentDetection:
    root: str
    ecosystems: tuple[str, ...]
    descriptors: dict[str, tuple[str, ...]]
    lockfiles: dict[str, tuple[str, ...]]
    confidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "ecosystems": list(self.ecosystems),
            "descriptors": {key: list(value) for key, value in self.descriptors.items()},
            "lockfiles": {key: list(value) for key, value in self.lockfiles.items()},
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ProvisionStep:
    purpose: str
    argv: tuple[str, ...]
    requires_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "requires_approval": self.requires_approval,
        }


@dataclass(frozen=True)
class ProvisioningPlan:
    root: str
    runtime: str
    steps: tuple[ProvisionStep, ...]
    plan_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "runtime": self.runtime,
            "steps": [step.as_dict() for step in self.steps],
            "plan_sha256": self.plan_sha256,
        }


def detect(root: str | Path) -> EnvironmentDetection:
    base = Path(root).resolve()
    descriptors: dict[str, tuple[str, ...]] = {}
    lockfiles: dict[str, tuple[str, ...]] = {}
    for ecosystem, markers in MARKERS.items():
        present = tuple(name for name in markers if (base / name).exists())
        if present:
            descriptors[ecosystem] = present
            locks = tuple(name for name in LOCKFILES.get(ecosystem, ()) if (base / name).exists())
            if locks:
                lockfiles[ecosystem] = locks
    if not descriptors:
        confidence = "NONE"
    elif sum(len(items) for items in descriptors.values()) >= 3:
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"
    return EnvironmentDetection(
        root=str(base),
        ecosystems=tuple(sorted(descriptors)),
        descriptors=descriptors,
        lockfiles=lockfiles,
        confidence=confidence,
    )


def build_plan(
    root: str | Path,
    *,
    runtime: str = "ROOTLESS_CONTAINER",
    detection: EnvironmentDetection | None = None,
) -> ProvisioningPlan:
    """Build a deterministic argv plan. No step is executed here."""
    info = detection or detect(root)
    steps: list[ProvisionStep] = [
        ProvisionStep("verify-runtime", ("maya", "runner", "profiles", "--json")),
    ]
    if "python" in info.ecosystems:
        steps.append(ProvisionStep("python-install", ("python", "-m", "pip", "install", "-r", "requirements.txt"), True))
        steps.append(ProvisionStep("python-check", ("python", "-m", "pytest", "-q")))
    if "node" in info.ecosystems:
        steps.append(ProvisionStep("node-install", ("npm", "ci"), True))
        steps.append(ProvisionStep("node-check", ("npm", "test", "--", "--runInBand")))
    if "rust" in info.ecosystems:
        steps.append(ProvisionStep("rust-check", ("cargo", "test", "--locked")))
    if "go" in info.ecosystems:
        steps.append(ProvisionStep("go-check", ("go", "test", "./...")))
    if "container" in info.ecosystems:
        steps.append(ProvisionStep("container-build", ("docker", "build", "."), True))
    payload = {
        "root": info.root,
        "runtime": runtime,
        "steps": [step.as_dict() for step in steps],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return ProvisioningPlan(info.root, runtime, tuple(steps), digest)


__all__ = [
    "MARKERS",
    "EnvironmentDetection",
    "ProvisionStep",
    "ProvisioningPlan",
    "detect",
    "build_plan",
]

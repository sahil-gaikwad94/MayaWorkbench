"""Environment detection and provisioning plans (WP1/P1).

Detection reads only descriptors; it never installs on the host. A provisioning
plan is a structured set of argv steps to be executed *inside* an isolated
runtime with explicit approval. Commands are rendered as argv arrays, never as
shell strings.

Resolvers are pure and deterministic: they choose a reproducible command from
the files that were actually detected. Nothing in this module is executed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKERS: dict[str, tuple[str, ...]] = {
    "python": (
        "pyproject.toml", "uv.lock", "poetry.lock", "requirements*.txt",
        "setup.py", "tox.ini", "pytest.ini",
    ),
    "node": ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json"),
    "rust": ("Cargo.toml", "Cargo.lock"),
    "go": ("go.mod", "go.sum"),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "gradlew", "mvnw"),
    "cpp": ("CMakeLists.txt", "Makefile", "compile_commands.json"),
    "container": ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml"),
    "ci": (".github/workflows",),
}
LOCKFILES: dict[str, tuple[str, ...]] = {
    "python": ("uv.lock", "poetry.lock", "requirements*.txt"),
    "node": ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
    "rust": ("Cargo.lock",),
    "go": ("go.sum",),
    "java": ("gradlew", "mvnw", "pom.xml"),
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
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "requires_approval": self.requires_approval,
            "note": self.note,
        }


@dataclass(frozen=True)
class ProvisioningPlan:
    root: str
    runtime: str
    steps: tuple[ProvisionStep, ...]
    plan_sha256: str
    fingerprint_sha256: str = ""
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "runtime": self.runtime,
            "steps": [step.as_dict() for step in self.steps],
            "plan_sha256": self.plan_sha256,
            "fingerprint_sha256": self.fingerprint_sha256,
            "notes": list(self.notes),
        }


def _matched(base: Path, marker: str) -> tuple[str, ...]:
    """Return the concrete file names present for a marker (supports globs)."""
    if "*" in marker or "?" in marker:
        return tuple(sorted(p.name for p in base.glob(marker) if p.is_file()))
    return (marker,) if (base / marker).exists() else ()


def detect(root: str | Path) -> EnvironmentDetection:
    base = Path(root).resolve()
    descriptors: dict[str, tuple[str, ...]] = {}
    lockfiles: dict[str, tuple[str, ...]] = {}
    for ecosystem, markers in MARKERS.items():
        present: list[str] = []
        for marker in markers:
            for name in _matched(base, marker):
                if name not in present:
                    present.append(name)
        present.sort()
        if present:
            descriptors[ecosystem] = tuple(present)
            locks: list[str] = []
            for marker in LOCKFILES.get(ecosystem, ()):
                for name in _matched(base, marker):
                    if name in present and name not in locks:
                        locks.append(name)
            if locks:
                lockfiles[ecosystem] = tuple(locks)
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


# --------------------------------------------------------------- file helpers
def _read_text(base: Path, name: str) -> str:
    try:
        return (base / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
        return ""


def _fingerprint(base: Path, info: EnvironmentDetection) -> str:
    """Hash the bytes of every detected descriptor/lockfile, sorted by name."""
    names = sorted(
        {name for items in info.descriptors.values() for name in items}
        | {name for items in info.lockfiles.values() for name in items}
    )
    entries: list[list[str]] = []
    for name in names:
        path = base / name
        try:
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_dir():
                digest = hashlib.sha256(("dir:" + name).encode("utf-8")).hexdigest()
            else:
                digest = hashlib.sha256(("missing:" + name).encode("utf-8")).hexdigest()
        except OSError:  # pragma: no cover - unreadable path
            digest = hashlib.sha256(("unreadable:" + name).encode("utf-8")).hexdigest()
        entries.append([name, digest])
    canonical = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ ecosystem plans
def _python_steps(base: Path, info: EnvironmentDetection) -> list[ProvisionStep]:
    descriptors = info.descriptors.get("python", ())
    locks = info.lockfiles.get("python", ())
    pyproject = _read_text(base, "pyproject.toml") if "pyproject.toml" in descriptors else ""
    steps: list[ProvisionStep] = []

    if "uv.lock" in locks:
        steps.append(ProvisionStep(
            "python-install", ("uv", "sync", "--frozen"), True,
            "Locked install from uv.lock; runs inside the isolated runtime.",
        ))
    elif "poetry.lock" in locks and "[tool.poetry" in pyproject:
        steps.append(ProvisionStep(
            "python-install", ("poetry", "install", "--no-interaction"), True,
            "Locked install from poetry.lock; runs inside the isolated runtime.",
        ))
    else:
        requirements = tuple(
            name for name in locks
            if name.startswith("requirements") and name.endswith(".txt")
        )
        if requirements:
            pinned = requirements[0]
            steps.append(ProvisionStep(
                "python-install",
                ("python", "-m", "pip", "install", "-r", pinned), True,
                f"Install from the detected {pinned}; review before approval.",
            ))
        else:
            argv = ["python", "-m", "pip", "install", "-e", "."]
            if "[project.optional-dependencies]" in pyproject:
                section = pyproject.split("[project.optional-dependencies]", 1)[1]
                if re.search(r"(?m)^\s*dev\s*=", section):
                    argv[-1] = ".[dev]"
            steps.append(ProvisionStep(
                "python-install", tuple(argv), True,
                "No lockfile or requirements*.txt was detected. This editable "
                "install is a REQUEST, not a guess that requirements.txt exists; "
                "review the project metadata before approving.",
            ))

    has_pytest = (
        "pytest.ini" in descriptors
        or "tox.ini" in descriptors
        or "[tool.pytest" in pyproject
    )
    if has_pytest:
        steps.append(ProvisionStep("python-check", ("python", "-m", "pytest", "-q")))
    else:
        steps.append(ProvisionStep(
            "python-check", ("python", "-m", "compileall", "-q", "."),
            note="Bounded syntax check; no pytest configuration was evidenced.",
        ))
    return steps


def _node_steps(info: EnvironmentDetection) -> list[ProvisionStep]:
    locks = info.lockfiles.get("node", ())
    steps: list[ProvisionStep] = []
    if "package-lock.json" in locks:
        steps.append(ProvisionStep(
            "node-install", ("npm", "ci"), True,
            "Locked install from package-lock.json.",
        ))
    elif "pnpm-lock.yaml" in locks:
        steps.append(ProvisionStep(
            "node-install", ("pnpm", "install", "--frozen-lockfile"), True,
            "Locked install from pnpm-lock.yaml.",
        ))
    elif "yarn.lock" in locks:
        steps.append(ProvisionStep(
            "node-install", ("yarn", "install", "--immutable"), True,
            "Locked install from yarn.lock (immutable / Yarn Berry).",
        ))
    else:
        steps.append(ProvisionStep(
            "node-install", ("npm", "install"), True,
            "No lockfile detected. The package manager is ambiguous: this npm "
            "install is an explicit PROPOSAL, not a guess. Review before approval.",
        ))
    steps.append(ProvisionStep("node-check", ("npm", "test")))
    return steps


def _rust_steps() -> list[ProvisionStep]:
    return [
        ProvisionStep("rust-check", ("cargo", "check", "--locked")),
        ProvisionStep("rust-test", ("cargo", "test", "--locked")),
    ]


def _go_steps() -> list[ProvisionStep]:
    return [
        ProvisionStep("go-vet", ("go", "vet", "./...")),
        ProvisionStep("go-check", ("go", "test", "./...")),
    ]


def _java_steps(base: Path, info: EnvironmentDetection) -> list[ProvisionStep]:
    descriptors = info.descriptors.get("java", ())
    has_maven = "pom.xml" in descriptors
    has_gradle = "build.gradle" in descriptors or "build.gradle.kts" in descriptors
    has_mvnw = "mvnw" in descriptors
    has_gradlew = "gradlew" in descriptors
    steps: list[ProvisionStep] = []
    if has_maven:
        prefix = ("./mvnw",) if has_mvnw else ("mvn",)
        steps.append(ProvisionStep(
            "java-test", (*prefix, "-B", "test"),
            note="Maven wrapper-first when mvnw is present; never assume a global mvn.",
        ))
    elif has_gradle:
        if has_gradlew:
            steps.append(ProvisionStep(
                "java-test", ("./gradlew", "test", "--no-daemon"),
                note="Gradle wrapper present; no global gradle is assumed.",
            ))
        else:
            steps.append(ProvisionStep("java-test", ("gradle", "test")))
    return steps


def _cpp_steps(base: Path, info: EnvironmentDetection) -> tuple[list[ProvisionStep], list[str]]:
    descriptors = info.descriptors.get("cpp", ())
    steps: list[ProvisionStep] = []
    notes: list[str] = []
    if "CMakeLists.txt" in descriptors:
        steps.append(ProvisionStep(
            "cpp-configure", ("cmake", "-S", ".", "-B", "build"), True,
            "CMake configure writes a build/ directory; requires approval.",
        ))
        steps.append(ProvisionStep(
            "cpp-build", ("cmake", "--build", "build"), True,
            "CMake build mutates build/; requires approval.",
        ))
        steps.append(ProvisionStep(
            "cpp-test", ("ctest", "--test-dir", "build", "--output-on-failure"),
        ))
    elif "Makefile" in descriptors:
        steps.append(ProvisionStep(
            "cpp-build", ("make",), True,
            "make mutates build artifacts; requires approval.",
        ))
        makefile = _read_text(base, "Makefile")
        if re.search(r"(?m)^test\s*:", makefile):
            steps.append(ProvisionStep("cpp-test", ("make", "test")))
    elif "compile_commands.json" in descriptors:
        notes.append(
            "compile_commands.json is compiler metadata, not a build system; "
            "no build command is proposed."
        )
    return steps, notes


def _container_steps() -> list[ProvisionStep]:
    # Never emit a bare host `docker build`. Container work must go through the
    # selected isolated runner backend, and this is explicitly a proposal.
    return [ProvisionStep(
        "container-build-proposal", ("maya", "runner", "doctor", "--json"), True,
        "PROPOSAL: container work must run through the selected isolated "
        "backend. A bare host `docker build` is intentionally not emitted; "
        "review this under explicit approval.",
    )]


def build_plan(
    root: str | Path,
    *,
    runtime: str = "ROOTLESS_CONTAINER",
    detection: EnvironmentDetection | None = None,
) -> ProvisioningPlan:
    """Build a deterministic argv plan. No step is executed here."""
    info = detection or detect(root)
    base = Path(info.root)

    steps: list[ProvisionStep] = [
        # Use the module entry point, not an assumed console-script on PATH:
        # the editable install is mandatory in this project, the binary is not.
        ProvisionStep(
            "verify-runtime",
            ("python", "-m", "mayaworkbench.cli.main", "--json", "runner", "profiles"),
        ),
    ]
    notes: list[str] = []

    if "python" in info.ecosystems:
        steps.extend(_python_steps(base, info))
    if "node" in info.ecosystems:
        steps.extend(_node_steps(info))
    if "rust" in info.ecosystems:
        steps.extend(_rust_steps())
    if "go" in info.ecosystems:
        steps.extend(_go_steps())
    if "java" in info.ecosystems:
        steps.extend(_java_steps(base, info))
    if "cpp" in info.ecosystems:
        cpp_steps, cpp_notes = _cpp_steps(base, info)
        steps.extend(cpp_steps)
        notes.extend(cpp_notes)
    if "container" in info.ecosystems:
        steps.extend(_container_steps())
    if "ci" in info.ecosystems:
        notes.append(
            "CI workflows detected (.github/workflows); no execution is "
            "invented. Inspect the workflow files directly."
        )

    fingerprint = _fingerprint(base, info)
    payload = {
        "root": info.root,
        "runtime": runtime,
        "fingerprint_sha256": fingerprint,
        "notes": list(notes),
        "steps": [step.as_dict() for step in steps],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return ProvisioningPlan(info.root, runtime, tuple(steps), digest, fingerprint, tuple(notes))


__all__ = [
    "MARKERS",
    "LOCKFILES",
    "EnvironmentDetection",
    "ProvisionStep",
    "ProvisioningPlan",
    "detect",
    "build_plan",
]

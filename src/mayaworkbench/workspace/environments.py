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
        ProvisionStep("verify-runtime", ("maya", "runner", "profiles", "--json")),
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


# ------------------------------------------------------- environment contract
#: Steps that download packages. Kept as purposes so a provisioner can gate them
#: without inferring semantics from argv (an install step is not identifiable
#: from the binary alone once wrappers are involved).
_NETWORK_PURPOSES = frozenset(
    {
        "container-build-proposal",
        "go-check",
        "go-vet",
        "java-test",
        "node-install",
        "python-install",
        "rust-check",
        "rust-test",
    }
)

#: Ecosystems for which a missing lockfile is a real reproducibility risk.
_LOCKFILE_EXPECTED = frozenset({"python", "node", "rust", "go"})


def requires_network(step: ProvisionStep) -> bool:
    """Whether a step needs package downloads, and therefore approval."""

    if step.purpose in _NETWORK_PURPOSES:
        return True
    return step.argv[:1] in {
        ("cargo",),
        ("go",),
        ("mvn",),
        ("./mvnw",),
        ("gradle",),
        ("./gradlew",),
    }


@dataclass(frozen=True)
class ServiceSpec:
    """A service the environment declares but does not (yet) manage.

    ``health_check`` is the argv a runtime would execute inside the task
    environment. Declaring the dependency is useful for parity and evidence;
    starting it is a separate capability that must be verified, so the
    provisioner reports services as ``not_started`` rather than pretending.
    """

    name: str
    port: int
    health_check: tuple[str, ...] = ()
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "health_check": list(self.health_check),
            "required": self.required,
        }


@dataclass(frozen=True)
class EnvironmentSpec:
    """Immutable, replayable description of an engineering environment.

    This is the contract a provisioner executes and a baseline reuses. It
    records what was *detected* (lockfile hashes, managers) and what was
    *requested* (image, services, secret references) separately, so evidence
    never presents a guess as a pin.
    """

    root: str
    runtime: str
    image: str
    image_digest: str
    ecosystems: tuple[str, ...]
    steps: tuple[ProvisionStep, ...]
    lockfiles: dict[str, str]
    lockfile_absent: tuple[str, ...]
    package_managers: dict[str, str]
    system_packages: tuple[str, ...]
    services: tuple[ServiceSpec, ...]
    env: dict[str, str]
    network_gated_steps: tuple[str, ...]
    hardware: dict[str, Any]
    provenance: dict[str, Any]
    notes: tuple[str, ...]
    spec_sha256: str

    @property
    def commands(self) -> dict[str, tuple[str, ...]]:
        """Purpose -> argv for every step, in plan order."""

        return {step.purpose: step.argv for step in self.steps}

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "runtime": self.runtime,
            "image": self.image,
            "image_digest": self.image_digest,
            "ecosystems": list(self.ecosystems),
            "steps": [step.as_dict() for step in self.steps],
            "commands": {
                purpose: list(argv) for purpose, argv in sorted(self.commands.items())
            },
            "lockfiles": dict(sorted(self.lockfiles.items())),
            "lockfile_absent": list(self.lockfile_absent),
            "package_managers": dict(sorted(self.package_managers.items())),
            "system_packages": list(self.system_packages),
            "services": [service.as_dict() for service in self.services],
            "env": dict(sorted(self.env.items())),
            "network_gated_steps": list(self.network_gated_steps),
            "hardware": dict(sorted(self.hardware.items())),
            "provenance": self.provenance,
            "notes": list(self.notes),
            "spec_sha256": self.spec_sha256,
        }


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _package_manager(purpose: str, argv: tuple[str, ...]) -> tuple[str, str] | None:
    if not argv:
        return None
    if purpose == "python-install":
        return "python", {"uv": "uv", "poetry": "poetry"}.get(argv[0], "pip")
    if purpose == "node-install":
        return "node", argv[0]
    if purpose.startswith("rust-"):
        return "rust", "cargo"
    if purpose.startswith("go-"):
        return "go", "go"
    if purpose == "java-test":
        manager = "gradle" if argv[0].endswith("gradlew") or argv[0] == "gradle" else "maven"
        return "java", manager
    if purpose.startswith("cpp-"):
        return "cpp", "cmake" if argv[0] == "cmake" else "make"
    return None


def resolve_spec(
    root: str | Path,
    *,
    runtime: str = "ROOTLESS_CONTAINER",
    image: str = "python:3.12-slim",
    image_digest: str = "",
    services: tuple[ServiceSpec, ...] | list[ServiceSpec] = (),
    secret_refs: tuple[str, ...] | list[str] = (),
    hardware: dict[str, Any] | None = None,
) -> EnvironmentSpec:
    """Resolve a deterministic environment spec from detected files.

    The spec never invents a lockfile: ``lockfiles`` contains only files that
    exist, with the hash of their real bytes, and ``lockfile_absent`` names the
    ecosystems where reproducibility is therefore weaker.
    """

    info = detect(root)
    plan = build_plan(info.root, runtime=runtime, detection=info)
    base = Path(info.root)

    lockfiles: dict[str, str] = {}
    for items in info.lockfiles.values():
        for name in items:
            digest = _sha256_file(base / name)
            if digest is not None:
                lockfiles[name] = digest
    lockfile_absent = tuple(
        ecosystem
        for ecosystem in sorted(info.ecosystems)
        if ecosystem in _LOCKFILE_EXPECTED and not info.lockfiles.get(ecosystem)
    )

    managers: dict[str, str] = {}
    for step in plan.steps:
        found = _package_manager(step.purpose, step.argv)
        if found is not None and found[0] not in managers:
            managers[found[0]] = found[1]

    steps = tuple(step for step in plan.steps if step.purpose != "verify-runtime")
    notes = list(plan.notes)
    notes.append(
        "Runtime pre-flight is not part of the spec: the runtime is verified and "
        "provisioned before any step runs."
    )
    if lockfile_absent:
        notes.append(
            "No lockfile was detected for: "
            + ", ".join(lockfile_absent)
            + ". Install steps are explicit requests, not invented pins."
        )
    if not hardware:
        notes.append("No hardware requirement was declared; the runtime default applies.")
    notes.append(
        "System packages are properties of the pinned image; they are not parsed "
        "from a Dockerfile."
    )

    env = {name: f"secret:{name}" for name in sorted(set(secret_refs))}
    network_gated = tuple(
        sorted({step.purpose for step in steps if requires_network(step)})
    )

    payload: dict[str, Any] = {
        "root": info.root,
        "runtime": runtime,
        "image": image,
        "image_digest": image_digest,
        "ecosystems": list(info.ecosystems),
        "steps": [step.as_dict() for step in steps],
        "lockfiles": dict(sorted(lockfiles.items())),
        "lockfile_absent": list(lockfile_absent),
        "package_managers": dict(sorted(managers.items())),
        "system_packages": [],
        "services": [service.as_dict() for service in services],
        "env": dict(sorted(env.items())),
        "network_gated_steps": list(network_gated),
        "hardware": dict(sorted((hardware or {}).items())),
        "provenance": {
            "detection_confidence": info.confidence,
            "descriptors": {key: list(value) for key, value in sorted(info.descriptors.items())},
            "fingerprint_sha256": plan.fingerprint_sha256,
            "plan_sha256": plan.plan_sha256,
        },
        "notes": notes,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return EnvironmentSpec(
        root=info.root,
        runtime=runtime,
        image=image,
        image_digest=image_digest,
        ecosystems=info.ecosystems,
        steps=steps,
        lockfiles=lockfiles,
        lockfile_absent=lockfile_absent,
        package_managers=managers,
        system_packages=(),
        services=tuple(services),
        env=env,
        network_gated_steps=network_gated,
        hardware=dict(hardware or {}),
        provenance=payload["provenance"],
        notes=tuple(notes),
        spec_sha256=digest,
    )


__all__ = [
    "MARKERS",
    "LOCKFILES",
    "EnvironmentDetection",
    "EnvironmentSpec",
    "ProvisionStep",
    "ProvisioningPlan",
    "ServiceSpec",
    "build_plan",
    "detect",
    "requires_network",
    "resolve_spec",
]

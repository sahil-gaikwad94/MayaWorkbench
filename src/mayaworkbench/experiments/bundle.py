"""Exportable, independently verifiable experiment reproduction bundle.

Phase 5's exit criterion is that another technical user can reproduce a small
run from the bundle *or understand exactly why it cannot be reproduced*. A
bundle therefore contains:

* the pre-registration (definition) and its content hash;
* every run's immutable manifest, including the commit, patch hash, command,
  seed, limits, environment and declared input hashes;
* the raw observations, not just summary statistics;
* the comparison payload, its Markdown report, and the statistical method;
* a ``reproduce.json`` with commands and an explicit ``gaps`` list;
* a root hash over every file.

Verification recomputes every hash, refuses missing/extra/unsafe paths, and
cross-checks runs ↔ manifests ↔ observations ↔ comparison. A bundle that fails
verification is not evidence.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.errors import ExperimentError
from mayaworkbench.experiments.protocol import canonical_sha256
from mayaworkbench.ids import new_ulid
from mayaworkbench.version import EXPERIMENT_PROTOCOL_VERSION

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = "1.0"
SINGLE_REQUIRED = ("README.md", "definition.json", "runs.json", "observations.json", "reproduce.json")
COMPARISON_REQUIRED = (
    "README.md",
    "baseline-definition.json",
    "candidate-definition.json",
    "comparison.json",
    "report.md",
    "runs.json",
    "observations.json",
    "reproduce.json",
)


@dataclass
class ExperimentBundleResult:
    path: Path
    manifest: dict[str, Any]


@dataclass
class ExperimentBundleVerification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    comparison: dict[str, Any] = field(default_factory=dict)
    reproduce: dict[str, Any] = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _root_hash(entries: list[dict[str, Any]]) -> str:
    lines = "\n".join(
        f"{entry['sha256']}  {entry['name']}"
        for entry in sorted(entries, key=lambda item: item["name"])
    )
    return _sha256(lines.encode("utf-8"))


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_experiment_bundle(
    destination: str | Path,
    *,
    kind: str,
    subject: dict[str, Any],
    documents: dict[str, bytes],
    gaps: list[str],
) -> ExperimentBundleResult:
    if kind not in {"EXPERIMENT", "COMPARISON"}:
        raise ExperimentError(f"Unknown experiment bundle kind: {kind}")
    target = Path(destination).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ExperimentError(
            f"Bundle destination is not empty: {target}",
            hint="Choose a new directory; reproduction bundles are never overwritten.",
        )
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    for name, data in sorted(documents.items()):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ExperimentError(f"Invalid bundle path: {name}")
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
        entries.append({"name": name, "sha256": _sha256(data), "bytes": len(data)})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": "xbundle_" + new_ulid(),
        "kind": kind,
        "subject": subject,
        "created_at": utcnow().isoformat(),
        "files": entries,
        "root_sha256": _root_hash(entries),
        "gaps": gaps,
        "note": (
            "Hashes cover every file except this manifest. Verify with "
            "`maya experiment verify-bundle <directory>`."
        ),
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return ExperimentBundleResult(path=target, manifest=manifest)


def verify_experiment_bundle(directory: str | Path) -> ExperimentBundleVerification:
    target = Path(directory).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.is_file():
        return ExperimentBundleVerification(
            ok=False, errors=["manifest.json is missing"]
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ExperimentBundleVerification(
            ok=False, errors=[f"manifest.json is unreadable: {exc}"]
        )
    kind = manifest.get("kind")
    if kind not in {"EXPERIMENT", "COMPARISON"}:
        errors.append(f"manifest kind is not EXPERIMENT/COMPARISON: {kind!r}")
        required: tuple[str, ...] = ()
    else:
        required = COMPARISON_REQUIRED if kind == "COMPARISON" else SINGLE_REQUIRED
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        return ExperimentBundleVerification(
            ok=False, manifest=manifest, errors=["manifest has no files"]
        )
    listed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            errors.append("manifest contains a malformed file entry")
            continue
        name = entry["name"]
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or name == MANIFEST_NAME
        ):
            errors.append(f"unsafe path in manifest: {name}")
            continue
        listed[name] = entry
        path = target / name
        if not path.is_file():
            errors.append(f"listed file is missing: {name}")
            continue
        data = path.read_bytes()
        if len(data) != entry.get("bytes"):
            errors.append(f"size mismatch for {name}")
        if _sha256(data) != entry.get("sha256"):
            errors.append(f"hash mismatch for {name}")
    if manifest.get("root_sha256") != _root_hash(entries):
        errors.append("manifest root_sha256 does not match its file list")
    on_disk = {
        str(path.relative_to(target))
        for path in target.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    for name in sorted(on_disk - set(listed)):
        errors.append(f"file is present but not listed in the manifest: {name}")
    for name in required:
        if name not in listed:
            errors.append(f"required bundle file is absent: {name}")

    documents = _load_documents(target, listed, errors)
    reproduce = documents.get("reproduce.json", {})
    if reproduce:
        for key in (
            "commit_sha",
            "commits",
            "patch_hashes",
            "patch_paths",
            "seeds",
            "commands",
            "gaps",
        ):
            if key not in reproduce:
                errors.append(f"reproduce.json is missing {key!r}")
        commands = reproduce.get("commands")
        if isinstance(commands, list) and not commands and kind == "COMPARISON":
            warnings.append("reproduce.json declares no commands")
        gaps = reproduce.get("gaps")
        if not isinstance(gaps, list):
            errors.append("reproduce.json gaps must be a list")
        commits = reproduce.get("commits")
        seeds = reproduce.get("seeds")
        if isinstance(commits, dict) and isinstance(seeds, dict):
            if set(commits) != set(seeds):
                errors.append(
                    "reproduce.json commits and seeds name different experiments"
                )
            if len(set(commits.values())) == 1:
                only = next(iter(commits.values()))
                if reproduce.get("commit_sha") != only:
                    errors.append(
                        "reproduce.json commit_sha disagrees with the per-arm commits"
                    )
            elif reproduce.get("commit_sha") is not None:
                errors.append(
                    "reproduce.json must not claim one commit_sha when arms differ"
                )
            if isinstance(commands, list):
                for command in commands:
                    if not isinstance(command, dict):
                        errors.append("reproduce.json contains a malformed command")
                        continue
                    experiment_id = command.get("experiment_id")
                    if command.get("commit_sha") != commits.get(experiment_id):
                        errors.append(
                            f"command for {experiment_id} names a different commit "
                            "than reproduce.json commits"
                        )
                    if not command.get("argv"):
                        # Imported arms have no command to re-run; the gap list
                        # already says their environment was not verified here.
                        warnings.append(
                            f"reproduce.json has no command for imported arm "
                            f"{experiment_id}"
                        )
        patches = reproduce.get("patch_hashes")
        patch_paths = reproduce.get("patch_paths")
        if isinstance(patches, dict) and isinstance(patch_paths, dict):
            if set(patches) != set(patch_paths):
                errors.append(
                    "reproduce.json patch_hashes and patch_paths name different "
                    "experiments"
                )
            for experiment_id, patch_hash in patches.items():
                path = patch_paths.get(experiment_id)
                if patch_hash and not path:
                    errors.append(
                        f"experiment {experiment_id} declares a patch hash but no "
                        "bundled patch path"
                    )
                elif bool(patch_hash) != bool(path):
                    errors.append(
                        f"experiment {experiment_id} patch hash and patch path disagree"
                    )
                elif path:
                    if path not in listed:
                        errors.append(
                            f"reproduce.json patch path {path!r} is not in the bundle"
                        )
                    elif not path.endswith(".diff"):
                        errors.append(
                            f"reproduce.json patch path {path!r} is not a .diff file"
                        )

    comparison = documents.get("comparison.json", {})
    if kind == "COMPARISON" and comparison:
        claimed = comparison.get("json_sha256")
        payload = {key: value for key, value in comparison.items() if key != "json_sha256"}
        if not claimed:
            errors.append("comparison.json has no json_sha256")
        elif canonical_sha256(payload) != claimed:
            errors.append("comparison.json content does not match its json_sha256")
        subject = manifest.get("subject") or {}
        if subject.get("comparison_id") and subject["comparison_id"] != comparison.get(
            "comparison_id"
        ):
            errors.append("manifest subject does not match comparison.json")

    runs = documents.get("runs.json", [])
    if not isinstance(runs, list):
        errors.append("runs.json must be a list")
        runs = []
    run_ids = {
        item.get("id") for item in runs if isinstance(item, dict) and item.get("id")
    }
    observations = documents.get("observations.json", {})
    if not isinstance(observations, dict):
        errors.append("observations.json must be an object keyed by run id")
        observations = {}
    for run in runs:
        if not isinstance(run, dict):
            errors.append("runs.json contains a malformed run")
            continue
        run_id = run.get("id")
        manifest_id = run.get("manifest_id")
        if manifest_id:
            name = f"manifests/{manifest_id}.json"
            document = documents.get(name)
            if document is None:
                errors.append(f"run {run_id} cites a manifest not in the bundle: {manifest_id}")
            else:
                claimed = document.get("manifest_sha256")
                payload = {
                    key: value
                    for key, value in document.items()
                    if key not in {"manifest_sha256", "created_at", "id"}
                }
                if not claimed or canonical_sha256(payload) != claimed:
                    errors.append(f"manifest {manifest_id} does not match its hash")
                if document.get("seed") != run.get("seed"):
                    errors.append(f"manifest {manifest_id} seed differs from its run")
        values = observations.get(run_id)
        if values is None:
            errors.append(f"no observations are recorded for run {run_id}")
            continue
        if not isinstance(values, list):
            errors.append(f"observations for run {run_id} are malformed")
            continue
        if run.get("observation_count") not in (None, len(values)):
            errors.append(f"run {run_id} observation_count differs from observations.json")
    for run_id in sorted(set(observations) - run_ids):
        errors.append(f"observations reference an unknown run: {run_id}")

    for name in ("definition.json", "baseline-definition.json", "candidate-definition.json"):
        document = documents.get(name)
        if document is None:
            continue
        payload = document.get("content_payload")
        claimed = document.get("content_sha256")
        if payload is None or not claimed:
            errors.append(f"{name} has no content_payload/content_sha256")
            continue
        if canonical_sha256(payload) != claimed:
            errors.append(f"{name} content does not match its content_sha256")

    if kind == "COMPARISON":
        report = documents.get("report.md", b"")
        if report and b"## Reproduce this comparison" not in report:
            warnings.append("report.md has no reproduction section")
    return ExperimentBundleVerification(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        manifest=manifest,
        comparison=comparison,
        reproduce=reproduce,
    )


def _load_documents(
    target: Path, listed: dict[str, dict[str, Any]], errors: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in listed:
        path = target / name
        if not path.is_file():
            continue
        try:
            if name.endswith(".json"):
                out[name] = json.loads(path.read_text(encoding="utf-8"))
            else:
                out[name] = path.read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read {name}: {exc}")
    return out


# ------------------------------------------------------------ runner bundles
#
# A *run* bundle is a single portable gzip-tar archive, not a directory. It is
# the user-owned hand-off to a runner this workbench does not control (a lab
# GPU box, a notebook, a future remote service). Every member except the root
# manifest is hashed; the manifest records the root hash. Verification is
# purely local and needs no crypto. Import additionally checks an optional
# attestation envelope through ``verification.attestation`` and always labels
# imported results as executed outside this workbench.

RUN_BUNDLE_SCHEMA_VERSION = "1.0"
RUN_BUNDLE_MANIFEST = "manifest.json"
MAX_RUN_BUNDLE_BYTES = 200 * 1024 * 1024
_RUN_BUNDLE_REQUIRED = (
    "definition.json",
    "runs.json",
    "observations.json",
    "reproduce.json",
    "environment.json",
    "hardware.json",
)
_RUN_BUNDLE_EXEMPT = (RUN_BUNDLE_MANIFEST, "attestation.json")
_REPRODUCE_KEYS = (
    "schema_version",
    "commit_sha",
    "commits",
    "seeds",
    "patch_hashes",
    "environment_lock_hashes",
    "dataset_fingerprint",
    "expected_outputs",
    "hardware",
    "environment",
    "commands",
    "gaps",
)
IMPORT_LIMITATION = (
    "These results were produced outside this workbench and imported from a "
    "portable runner bundle; execution was not verified here, so they are "
    "unattested unless a trusted-runner signature verifies."
)


@dataclass
class RunBundleResult:
    path: Path
    manifest: dict[str, Any]
    archive_sha256: str


@dataclass
class RunBundleVerification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    reproduce: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, Any] = field(default_factory=dict)
    archive_sha256: str = ""
    attestation_present: bool = False


@dataclass
class RunBundleImportResult:
    path: Path
    archive_sha256: str
    attestation: str
    limitation: str
    manifest: dict[str, Any]
    definitions: list[Any] = field(default_factory=list)
    runs: list[Any] = field(default_factory=list)
    created_run_ids: list[str] = field(default_factory=list)
    observation_count: int = 0
    notes: list[str] = field(default_factory=list)


def sha256_file(path: str | Path) -> str:
    """Content sha256 of a declared input file (no metadata, no path)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dataset_fingerprint(inputs: dict[str, str]) -> str:
    """Fingerprint declared inputs from their content hashes.

    ``inputs`` maps a workspace-relative name to a hex sha256. The digest is a
    sha256 over sorted ``hash  name`` lines, so it is stable across machines
    regardless of declaration order and never embeds a local path.
    """
    if not inputs:
        return ""
    lines: list[str] = []
    for name in sorted(inputs):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ExperimentError(f"invalid declared input path: {name!r}")
        digest = inputs[name]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ExperimentError(
                f"declared input {name!r} is not a sha256 digest",
                hint="Fingerprints are built from content hashes, not free-form text.",
            )
        lines.append(f"{digest}  {name}")
    return _sha256("\n".join(lines).encode("utf-8"))


def _bundle_relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ExperimentError(f"invalid bundle member name: {name!r}")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ExperimentError(f"unsafe bundle member name: {name!r}")
    return name


def _runner_reproduce(
    *,
    definition: dict[str, Any],
    manifests: list[dict[str, Any]],
    environment: dict[str, Any],
    dataset_fingerprint: str,
    hardware: dict[str, Any],
    expected_outputs: list[dict[str, Any]],
    gaps: list[str] | None,
) -> dict[str, Any]:
    commits: dict[str, Any] = {}
    patches: dict[str, Any] = {}
    locks: dict[str, Any] = {}
    seeds: dict[str, list[int]] = {}
    commands: list[dict[str, Any]] = []
    for manifest in manifests:
        experiment_id = str(manifest.get("experiment_id", ""))
        commits[experiment_id] = manifest.get("commit_sha")
        patches[experiment_id] = manifest.get("patch_hash")
        protocol = manifest.get("protocol") or {}
        env = protocol.get("environment") or {}
        locks[experiment_id] = {
            "lockfile_path": env.get("lockfile_path"),
            "lockfile_hash": env.get("lockfile_hash"),
            "image_digest": env.get("image_digest"),
            "python_version": env.get("python_version"),
            "framework_versions": env.get("framework_versions") or {},
        }
        bucket = seeds.setdefault(experiment_id, [])
        seed = manifest.get("seed")
        if seed is not None and seed not in bucket:
            bucket.append(seed)
        commands.append(
            {
                "experiment_id": experiment_id,
                "seed": seed,
                "attempt": manifest.get("attempt"),
                "commit_sha": manifest.get("commit_sha"),
                "argv": manifest.get("command") or [],
                "manifest_id": manifest.get("id"),
                "manifest_sha256": manifest.get("manifest_sha256"),
            }
        )
    for experiment_id, values in (definition.get("declared_seeds") or {}).items():
        bucket = seeds.setdefault(str(experiment_id), [])
        for seed in values:
            if seed not in bucket:
                bucket.append(seed)
    declared_commits = {
        str(key): value
        for key, value in (definition.get("commit_sha_by_arm") or {}).items()
        if value
    }
    commits.update({key: value for key, value in declared_commits.items() if not commits.get(key)})
    distinct = {value for value in commits.values() if value}
    return {
        "schema_version": EXPERIMENT_PROTOCOL_VERSION,
        "kind": definition.get("kind", "SINGLE"),
        # A comparison can span two revisions; commit_sha is only set when all
        # arms agree. ``commits`` is authoritative.
        "commit_sha": next(iter(distinct)) if len(distinct) == 1 else None,
        "commits": commits,
        "seeds": seeds,
        "patch_hashes": patches,
        "environment_lock_hashes": locks,
        "dataset_fingerprint": dataset_fingerprint,
        "expected_outputs": expected_outputs,
        "hardware": hardware,
        "environment": environment,
        "commands": commands,
        "command_templates": definition.get("command_templates") or {},
        "recorded_models": "none (the Experiment Lab never calls a model)",
        "prerequisites": [
            "check out the commit in `commits` and apply the declared patch when present",
            "install the exact lockfile/image recorded in `environment_lock_hashes`",
            "use the recorded seeds and argv verbatim; no argument is inferred after the fact",
            "feed the same declared dataset (see `dataset_fingerprint`)",
            "run offline; a remote runner is user-owned and is not a trusted sandbox",
        ],
        "gaps": list(gaps or []),
        "verify": "maya runner import <bundle> recomputes every hash before importing",
    }


def write_run_bundle(
    destination: str | Path,
    definition: dict[str, Any],
    manifests: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    observations: dict[str, Any],
    outputs: dict[str, bytes],
    environment: dict[str, Any],
    dataset_fingerprint: str,
    hardware: dict[str, Any],
    *,
    gaps: list[str] | None = None,
    attestation: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> RunBundleResult:
    """Write a deterministic, self-verifying portable runner bundle.

    No timestamps or random ids are emitted: the same evidence always produces
    byte-identical archives. ``attestation`` is an optional envelope produced
    by ``verification.attestation.sign_bundle`` on the manifest root; it is the
    only member not covered by the root hash, mirroring that module's format.
    """
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise ExperimentError(
            f"Run bundle destination already exists: {target}",
            hint="Choose a new path; bundles are never overwritten.",
        )
    if not isinstance(definition, dict) or not definition:
        raise ExperimentError("A run bundle needs a definition document")
    if not isinstance(runs, list) or not isinstance(observations, dict):
        raise ExperimentError("A run bundle needs runs and an observations object")
    if not isinstance(manifests, list):
        raise ExperimentError("A run bundle needs a manifest list")

    documents: dict[str, bytes] = {
        "definition.json": _json_bytes(definition),
        "runs.json": _json_bytes(runs),
        "observations.json": _json_bytes(observations),
        "environment.json": _json_bytes(environment or {}),
        "hardware.json": _json_bytes(hardware or {}),
    }
    if context is not None:
        documents["context.json"] = _json_bytes(context)
    for manifest in manifests:
        if not isinstance(manifest, dict) or not manifest.get("id"):
            raise ExperimentError("Every bundled manifest needs an id")
        name = _bundle_relative(f"manifests/{manifest['id']}.json")
        documents[name] = _json_bytes(manifest)
    for name, data in (outputs or {}).items():
        member = _bundle_relative(f"outputs/{name}")
        documents[member] = bytes(data)

    entries: list[dict[str, Any]] = []
    for name, data in sorted(documents.items()):
        entries.append({"name": name, "sha256": _sha256(data), "bytes": len(data)})
    reproduce = _runner_reproduce(
        definition=definition,
        manifests=manifests,
        environment=environment or {},
        dataset_fingerprint=dataset_fingerprint or "",
        hardware=hardware or {},
        expected_outputs=[
            {"name": item["name"], "sha256": item["sha256"], "bytes": item["bytes"]}
            for item in entries
            if item["name"].startswith("outputs/")
        ],
        gaps=gaps,
    )
    documents["reproduce.json"] = _json_bytes(reproduce)
    reproduce_bytes = documents["reproduce.json"]
    entries.append(
        {
            "name": "reproduce.json",
            "sha256": _sha256(reproduce_bytes),
            "bytes": len(reproduce_bytes),
        }
    )
    entries.sort(key=lambda item: item["name"])
    manifest_doc = {
        "schema_version": RUN_BUNDLE_SCHEMA_VERSION,
        "kind": "RUN_BUNDLE",
        "algorithm": "sha256",
        "files": entries,
        "root_sha256": _root_hash(entries),
        "note": (
            "Hashes cover every member except manifest.json and the optional "
            "attestation.json. Verify with maya runner import <bundle>."
        ),
    }
    documents[RUN_BUNDLE_MANIFEST] = _json_bytes(manifest_doc)
    if attestation is not None:
        if not isinstance(attestation, dict) or not isinstance(
            attestation.get("signature"), str
        ):
            raise ExperimentError(
                "attestation must be a signature envelope from verification.attestation"
            )
        documents["attestation.json"] = _json_bytes(attestation)

    target.parent.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(documents.items()):
            if len(name.encode("utf-8")) > 100:
                raise ExperimentError(
                    f"bundle member name is too long for a portable tar: {name!r}"
                )
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    payload = gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)
    target.write_bytes(payload)
    return RunBundleResult(
        path=target, manifest=manifest_doc, archive_sha256=_sha256(payload)
    )


def _read_run_archive(source: Path, errors: list[str]) -> tuple[dict[str, bytes], str]:
    contents: dict[str, bytes] = {}
    try:
        archive_bytes = source.read_bytes()
    except OSError as exc:
        errors.append(f"run bundle is unreadable: {exc}")
        return contents, ""
    if len(archive_bytes) > MAX_RUN_BUNDLE_BYTES:
        errors.append("run bundle exceeds the 200 MiB import ceiling")
        return contents, _sha256(archive_bytes)
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for member in archive.getmembers():
                name = member.name
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    errors.append(f"unsafe archive member type: {name}")
                    continue
                if member.isdir():
                    continue
                try:
                    _bundle_relative(name)
                except ExperimentError:
                    errors.append(f"unsafe archive path: {name}")
                    continue
                if name in contents:
                    errors.append(f"duplicate archive member: {name}")
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    errors.append(f"unreadable archive member: {name}")
                    continue
                data = handle.read()
                total += len(data)
                if total > MAX_RUN_BUNDLE_BYTES:
                    errors.append("run bundle expands beyond the 200 MiB import ceiling")
                    break
                contents[name] = data
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        errors.append(f"archive cannot be read as a gzip tar: {exc}")
    return contents, _sha256(archive_bytes)


def _json_member(
    contents: dict[str, bytes], name: str, errors: list[str]
) -> dict[str, Any] | list[Any] | None:
    data = contents.get(name)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot parse {name}: {exc}")
        return None


def _run_documents(
    contents: dict[str, bytes], listed: dict[str, dict[str, Any]], errors: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in listed:
        if name.endswith(".json"):
            parsed = _json_member(contents, name, errors)
            if parsed is not None:
                out[name] = parsed
        else:
            out[name] = contents[name]
    return out


def _check_definition_document(document: Any, errors: list[str]) -> None:
    if not isinstance(document, dict):
        return
    arms = document.get("arms")
    candidates = (
        [item for item in arms if isinstance(item, dict)]
        if isinstance(arms, list)
        else [document]
    )
    for arm in candidates:
        payload = arm.get("content_payload")
        claimed = arm.get("content_sha256")
        if payload is None or not claimed:
            errors.append("definition has no content_payload/content_sha256")
            continue
        if canonical_sha256(payload) != claimed:
            errors.append("definition content does not match its content_sha256")


def _cross_check_run_bundle(
    documents: dict[str, Any],
    listed: dict[str, dict[str, Any]],
    errors: list[str],
    warnings: list[str],
) -> None:
    runs = documents.get("runs.json")
    observations = documents.get("observations.json")
    if not isinstance(runs, list):
        errors.append("runs.json must be a list")
        runs = []
    if not isinstance(observations, dict):
        errors.append("observations.json must be an object keyed by run id")
        observations = {}
    run_ids: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            errors.append("runs.json contains a malformed run")
            continue
        run_id = str(run.get("id", ""))
        run_ids.add(run_id)
        values = observations.get(run_id)
        if values is None:
            errors.append(f"no observations are recorded for run {run_id}")
            continue
        if not isinstance(values, list):
            errors.append(f"observations for run {run_id} are malformed")
            continue
        if run.get("observation_count") not in (None, len(values)):
            errors.append(f"run {run_id} observation_count differs from observations.json")
        manifest_id = run.get("manifest_id")
        if not manifest_id:
            continue
        document = documents.get(f"manifests/{manifest_id}.json")
        if document is None:
            errors.append(f"run {run_id} cites a missing manifest: {manifest_id}")
            continue
        if not isinstance(document, dict):
            errors.append(f"manifest {manifest_id} is malformed")
            continue
        if document.get("seed") != run.get("seed"):
            errors.append(f"manifest {manifest_id} seed differs from its run")
        claimed = (run.get("details") or {}).get("manifest_sha256")
        if claimed and document.get("manifest_sha256") != claimed:
            errors.append(f"manifest {manifest_id} hash differs from its run")
        if document.get("schema_version") not in (None, "1.0"):
            errors.append(f"manifest {manifest_id} has an unsupported schema version")
    for run_id in sorted(set(observations) - run_ids):
        errors.append(f"observations reference an unknown run: {run_id}")

    reproduce = documents.get("reproduce.json")
    if not isinstance(reproduce, dict):
        errors.append("reproduce.json must be an object")
        return
    commands = reproduce.get("commands")
    if not isinstance(commands, list):
        errors.append("reproduce.json commands must be a list")
    else:
        for command in commands:
            if not isinstance(command, dict) or not isinstance(command.get("argv"), list):
                errors.append("reproduce.json contains a malformed command")
    expected = reproduce.get("expected_outputs")
    if not isinstance(expected, list):
        errors.append("reproduce.json expected_outputs must be a list")
    else:
        for item in expected:
            if not isinstance(item, dict) or not item.get("name"):
                errors.append("reproduce.json contains a malformed expected output")
                continue
            name = item["name"]
            entry = listed.get(name)
            if entry is None:
                errors.append(f"expected output is not in the bundle: {name}")
            elif entry.get("sha256") != item.get("sha256"):
                errors.append(f"expected output hash mismatch: {name}")
    _check_definition_document(documents.get("definition.json"), errors)
    if not isinstance(reproduce.get("gaps"), list):
        errors.append("reproduce.json gaps must be a list")
    if not reproduce.get("commands"):
        warnings.append("bundle declares no executable commands")


def verify_run_bundle(path: str | Path) -> RunBundleVerification:
    """Recompute every hash and refuse unsafe, extra or missing archive members."""
    source = Path(path).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not source.is_file():
        return RunBundleVerification(ok=False, errors=[f"run bundle is not a file: {source}"])
    contents, archive_sha = _read_run_archive(source, errors)
    if errors:
        return RunBundleVerification(ok=False, errors=errors, archive_sha256=archive_sha)
    manifest_doc = _json_member(contents, RUN_BUNDLE_MANIFEST, errors)
    if not isinstance(manifest_doc, dict):
        return RunBundleVerification(
            ok=False,
            errors=errors or ["manifest.json is missing or malformed"],
            archive_sha256=archive_sha,
        )
    if manifest_doc.get("schema_version") != RUN_BUNDLE_SCHEMA_VERSION:
        errors.append(
            f"unsupported run-bundle manifest schema version: "
            f"{manifest_doc.get('schema_version')!r}"
        )
    if manifest_doc.get("kind") != "RUN_BUNDLE":
        errors.append(f"manifest kind is not RUN_BUNDLE: {manifest_doc.get('kind')!r}")
    entries = manifest_doc.get("files")
    if not isinstance(entries, list) or not entries:
        return RunBundleVerification(
            ok=False,
            errors=errors + ["manifest has no files"],
            manifest=manifest_doc,
            archive_sha256=archive_sha,
        )
    listed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            errors.append("manifest contains a malformed file entry")
            continue
        name = entry["name"]
        try:
            _bundle_relative(name)
        except ExperimentError:
            errors.append(f"unsafe path in manifest: {name}")
            continue
        if name in _RUN_BUNDLE_EXEMPT:
            errors.append(f"manifest must not hash an exempt member: {name}")
            continue
        listed[name] = entry
        if name not in contents:
            errors.append(f"listed file is missing: {name}")
            continue
        data = contents[name]
        if len(data) != entry.get("bytes"):
            errors.append(f"size mismatch for {name}")
        if _sha256(data) != entry.get("sha256"):
            errors.append(f"hash mismatch for {name}")
    if manifest_doc.get("root_sha256") != _root_hash(entries):
        errors.append("manifest root_sha256 does not match its file list")
    for name in sorted(set(contents) - set(listed) - set(_RUN_BUNDLE_EXEMPT)):
        errors.append(f"file is present but not listed in the manifest: {name}")
    for name in _RUN_BUNDLE_REQUIRED:
        if name not in listed:
            errors.append(f"required bundle file is absent: {name}")

    attestation_present = "attestation.json" in contents
    if attestation_present:
        envelope = _json_member(contents, "attestation.json", errors)
        if isinstance(envelope, dict):
            if envelope.get("root_sha256") != manifest_doc.get("root_sha256"):
                errors.append("attestation refers to a different manifest root")
            if not isinstance(envelope.get("signature"), str):
                errors.append("attestation has no signature")

    documents = _run_documents(contents, listed, errors)
    reproduce = documents.get("reproduce.json")
    if isinstance(reproduce, dict):
        if reproduce.get("schema_version") != EXPERIMENT_PROTOCOL_VERSION:
            errors.append(
                f"unsupported reproduce schema version: {reproduce.get('schema_version')!r}"
            )
        for key in _REPRODUCE_KEYS:
            if key not in reproduce:
                errors.append(f"reproduce.json is missing {key!r}")
    _cross_check_run_bundle(documents, listed, errors, warnings)
    return RunBundleVerification(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        manifest=manifest_doc,
        reproduce=reproduce if isinstance(reproduce, dict) else {},
        documents=documents,
        archive_sha256=archive_sha,
        attestation_present=attestation_present,
    )


def _extract_run_archive(source: Path, target: Path) -> None:
    """Extract only validated regular files into ``target`` (no path escapes)."""
    errors: list[str] = []
    contents, _ = _read_run_archive(source, errors)
    if errors:
        raise ExperimentError("cannot extract run bundle: " + "; ".join(errors[:3]))
    for name, data in contents.items():
        _bundle_relative(name)
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _ensure_bundle_context(documents: dict[str, Any], store: Any, notes: list[str]) -> None:
    """Recreate the FK prerequisites for imported definitions and runs.

    The imported home has no repository or snapshot of its own, so the bundle
    carries their immutable metadata. Artifact references are deliberately
    dropped: the artifact bytes belong to the exporting home and are not
    silently re-pointed at unrelated local ids.
    """
    from pydantic import ValidationError

    from mayaworkbench.domain.models import (
        Project,
        Repository,
        RepositorySnapshot,
    )
    from mayaworkbench.errors import NotFoundError
    from mayaworkbench.storage.stores import (
        ProjectStore,
        RepositoryStore,
        SnapshotStore,
    )

    context = documents.get("context.json")
    if not isinstance(context, dict):
        notes.append("bundle has no context.json; FK prerequisites were not imported")
        return
    projects = ProjectStore(store.db)
    repositories = RepositoryStore(store.db)
    snapshots = SnapshotStore(store.db)
    for payload in context.get("projects", []):
        try:
            project = Project.model_validate(payload)
        except ValidationError as exc:
            raise ExperimentError(f"invalid project in run bundle: {exc}") from exc
        try:
            projects.get(project.id)
        except NotFoundError:
            projects.create(project)
    for payload in context.get("repositories", []):
        try:
            repository = Repository.model_validate(payload)
        except ValidationError as exc:
            raise ExperimentError(f"invalid repository in run bundle: {exc}") from exc
        try:
            repositories.get(repository.id)
        except NotFoundError:
            repositories.add(repository)
    for payload in context.get("snapshots", []):
        data = dict(payload)
        data["patch_artifact_id"] = None
        data["untracked_manifest_artifact_id"] = None
        try:
            snapshot = RepositorySnapshot.model_validate(data)
        except ValidationError as exc:
            raise ExperimentError(f"invalid snapshot in run bundle: {exc}") from exc
        try:
            snapshots.get(snapshot.id)
        except NotFoundError:
            snapshots.create(snapshot)
    notes.append(
        "artifact references were not imported: outputs live in the bundle, "
        "and patch artifacts are not bundled"
    )


def _definition_arms(documents: dict[str, Any]) -> list[dict[str, Any]]:
    document = documents.get("definition.json")
    if not isinstance(document, dict):
        return []
    if isinstance(document.get("arms"), list):
        return [item for item in document["arms"] if isinstance(item, dict)]
    if "definition" in document:
        return [document]
    return []


def import_run_bundle(
    path: str | Path,
    store: Any,
    *,
    key: bytes | str | None = None,
) -> RunBundleImportResult:
    """Import a verified bundle's runs as IMPORTED evidence.

    Verification is mandatory. An optional attestation is checked with the
    existing ``verification.attestation`` HMAC helper (no new crypto); a
    signature identifies the signing key, it does not prove the result. Every
    imported run is labelled ``execution=IMPORTED`` and gets an explicit
    limitation containing "outside this workbench", which is the signal Gate F
    already uses to downgrade experiment evidence.
    """
    from pydantic import ValidationError

    from mayaworkbench.domain.models import (
        ExperimentDefinition,
        ExperimentManifest,
        ExperimentRun,
    )
    from mayaworkbench.errors import NotFoundError
    from mayaworkbench.experiments.metrics import ExtractedMetrics, build_observations
    from mayaworkbench.verification.attestation import verify_attestation

    verification = verify_run_bundle(path)
    if not verification.ok:
        raise ExperimentError(
            "Run bundle failed verification; refusing to import",
            hint="; ".join(verification.errors[:3]) or None,
            detail="\n".join(verification.errors),
        )
    source = Path(path).expanduser().resolve()
    documents = verification.documents
    notes: list[str] = []

    level = "UNATTESTED"
    attestation_details: dict[str, Any] = {"level": "UNATTESTED", "verified": False}
    if verification.attestation_present:
        with tempfile.TemporaryDirectory(prefix="maya-run-bundle-") as staging:
            _extract_run_archive(source, Path(staging))
            secret = key if key is not None else os.environ.get("MAYA_SIGNING_KEY")
            checked = verify_attestation(Path(staging), key=secret)
        attestation_details = dict(checked)
        if checked.get("verified") and isinstance(checked.get("level"), str):
            level = checked["level"]
        elif not checked.get("ok", True):
            notes.append(f"attestation error: {checked.get('error')}")
    limitation = IMPORT_LIMITATION
    if level != "UNATTESTED":
        limitation += (
            f" A trusted-runner signature for key {attestation_details.get('key_id')} "
            "verified; that identifies the signing key only and does not prove "
            "the scientific result or how the command executed."
        )

    _ensure_bundle_context(documents, store, notes)

    definitions: list[Any] = []
    for arm in _definition_arms(documents):
        payload = arm.get("definition")
        if not isinstance(payload, dict):
            raise ExperimentError("run bundle definition arm has no definition document")
        data = dict(payload)
        original_task = data.get("task_id")
        details = dict(data.get("details") or {})
        details["imported_original_task_id"] = original_task
        details["imported_from_bundle_sha256"] = verification.archive_sha256
        details["attestation"] = level
        details["imported_limitation"] = limitation
        data["task_id"] = None  # FK: the importing home has no such task
        data["details"] = details
        try:
            definition = ExperimentDefinition.model_validate(data)
        except ValidationError as exc:
            raise ExperimentError(f"invalid definition in run bundle: {exc}") from exc
        try:
            existing = store.get_definition(definition.id)
        except NotFoundError:
            store.create_definition(definition)
        else:
            if existing.content_sha256 != definition.content_sha256:
                raise ExperimentError(
                    f"bundle definition {definition.id} conflicts with the local record"
                )
            definition = existing
        definitions.append(definition)

    for name, payload in sorted(documents.items()):
        if not name.startswith("manifests/") or not name.endswith(".json"):
            continue
        if not isinstance(payload, dict):
            raise ExperimentError(f"malformed manifest in run bundle: {name}")
        try:
            manifest = ExperimentManifest.model_validate(payload)
        except ValidationError as exc:
            raise ExperimentError(f"invalid manifest in run bundle: {name}: {exc}") from exc
        try:
            existing = store.get_manifest(manifest.id)
        except NotFoundError:
            store.create_manifest(manifest)
        else:
            if existing.manifest_sha256 != manifest.manifest_sha256:
                raise ExperimentError(
                    f"bundle manifest {manifest.id} conflicts with the local record"
                )

    runs_payload = documents.get("runs.json")
    observations = documents.get("observations.json") or {}
    created_run_ids: list[str] = []
    runs: list[Any] = []
    observation_count = 0
    for payload in runs_payload if isinstance(runs_payload, list) else []:
        if not isinstance(payload, dict):
            raise ExperimentError("malformed run in run bundle")
        data = dict(payload)
        details = dict(data.get("details") or {})
        details["execution"] = "IMPORTED"
        details["attestation"] = level
        details["imported_from_bundle_sha256"] = verification.archive_sha256
        details["imported_limitation"] = limitation
        details["imported_note"] = (
            "Metrics were produced by a user-owned runner and imported; no local "
            "command was executed for this record."
        )
        data["details"] = details
        data["task_id"] = None
        data["output_artifact_id"] = None
        data["metrics_artifact_id"] = None
        try:
            run = ExperimentRun.model_validate(data)
        except ValidationError as exc:
            raise ExperimentError(f"invalid run in run bundle: {exc}") from exc
        try:
            store.get_run(run.id)
        except NotFoundError:
            store.create_run(run)
            created_run_ids.append(run.id)
            rows = observations.get(run.id) or []
            values: dict[str, list[tuple[int, float | None, float]]] = {}
            source_name = "IMPORTED"
            for item in rows:
                if not isinstance(item, dict) or not item.get("metric_key"):
                    continue
                key_name = str(item["metric_key"])
                values.setdefault(key_name, []).append(
                    (
                        int(item.get("sample_index", 0)),
                        item.get("step"),
                        float(item["value"]),
                    )
                )
                source_name = str(item.get("source") or "IMPORTED")
            extracted = ExtractedMetrics(values=values, source=source_name)
            rebuilt = build_observations(
                run_id=run.id,
                experiment_id=run.experiment_id,
                project_id=run.project_id,
                extracted=extracted,
            )
            store.add_observations(run, rebuilt)
            observation_count += len(rebuilt)
        else:
            notes.append(f"run {run.id} already exists locally; skipped")
        runs.append(run)

    return RunBundleImportResult(
        path=source,
        archive_sha256=verification.archive_sha256,
        attestation=level,
        limitation=limitation,
        manifest=verification.manifest,
        definitions=definitions,
        runs=runs,
        created_run_ids=created_run_ids,
        observation_count=observation_count,
        notes=notes,
    )


__all__ = [
    "ExperimentBundleResult",
    "ExperimentBundleVerification",
    "write_experiment_bundle",
    "verify_experiment_bundle",
    "MANIFEST_NAME",
    "RUN_BUNDLE_SCHEMA_VERSION",
    "RUN_BUNDLE_MANIFEST",
    "IMPORT_LIMITATION",
    "RunBundleResult",
    "RunBundleVerification",
    "RunBundleImportResult",
    "sha256_file",
    "compute_dataset_fingerprint",
    "write_run_bundle",
    "verify_run_bundle",
    "import_run_bundle",
]

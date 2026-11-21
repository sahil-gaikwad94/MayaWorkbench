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

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.errors import ExperimentError
from mayaworkbench.experiments.protocol import canonical_sha256
from mayaworkbench.ids import new_ulid

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


__all__ = [
    "ExperimentBundleResult",
    "ExperimentBundleVerification",
    "write_experiment_bundle",
    "verify_experiment_bundle",
    "MANIFEST_NAME",
]

"""Exportable and verifiable evidence bundle (§13.2 Gate G).

A bundle is a directory of immutable evidence plus a manifest of hashes. It is
self-describing: the verifier recomputes every hash, refuses missing or extra
files, and cross-checks that the report names the checks, criteria and patch the
bundle actually contains. A bundle that fails verification is not evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.errors import VerificationError
from mayaworkbench.ids import new_ulid

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = "1.0"
REQUIRED_FILES = ("report.json", "report.md", "patch.diff", "checks.json", "criteria.json")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _root_hash(entries: list[dict[str, Any]]) -> str:
    lines = "\n".join(
        f"{entry['sha256']}  {entry['name']}"
        for entry in sorted(entries, key=lambda item: item["name"])
    )
    return _sha256(lines.encode("utf-8"))


@dataclass
class BundleResult:
    path: Path
    manifest: dict[str, Any]


@dataclass
class BundleVerification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)


def write_bundle(destination: str | Path, payloads: dict[str, bytes]) -> BundleResult:
    target = Path(destination).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise VerificationError(
            f"Bundle destination is not empty: {target}",
            hint="Choose a new directory; evidence bundles are never overwritten.",
        )
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    for name, data in sorted(payloads.items()):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise VerificationError(f"Invalid bundle path: {name}")
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
        entries.append(
            {"name": name, "sha256": _sha256(data), "bytes": len(data)}
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": "bundle_" + new_ulid(),
        "created_at": utcnow().isoformat(),
        "files": entries,
        "root_sha256": _root_hash(entries),
        "note": (
            "Hashes cover every file except this manifest. Verify with "
            "`maya verify bundle --verify <directory>`."
        ),
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return BundleResult(path=target, manifest=manifest)


def verify_bundle(directory: str | Path) -> BundleVerification:
    target = Path(directory).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.is_file():
        return BundleVerification(ok=False, errors=["manifest.json is missing"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return BundleVerification(ok=False, errors=[f"manifest.json is unreadable: {exc}"])
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        return BundleVerification(ok=False, manifest=manifest, errors=["manifest has no files"])
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
        digest = _sha256(data)
        if digest != entry.get("sha256"):
            errors.append(f"hash mismatch for {name}")
    expected_root = _root_hash(entries)
    if manifest.get("root_sha256") != expected_root:
        errors.append("manifest root_sha256 does not match its file list")
    on_disk = {
        str(path.relative_to(target))
        for path in target.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    for name in sorted(on_disk - set(listed)):
        errors.append(f"file is present but not listed in the manifest: {name}")
    for name in REQUIRED_FILES:
        if name not in listed:
            errors.append(f"required evidence file is absent: {name}")

    report: dict[str, Any] = {}
    report_path = target / "report.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"report.json is not valid JSON: {exc}")
        else:
            patch_path = target / "patch.diff"
            if patch_path.is_file():
                patch_digest = _sha256(patch_path.read_bytes())
                if report.get("patch_sha256") != patch_digest:
                    errors.append(
                        "report.json names a different patch than patch.diff contains"
                    )
            checks_path = target / "checks.json"
            if checks_path.is_file():
                try:
                    checks = json.loads(checks_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    errors.append(f"checks.json is not valid JSON: {exc}")
                    checks = []
                if isinstance(checks, list):
                    reported_ids = set(report.get("check_ids") or [])
                    present_ids = {
                        item.get("id") for item in checks if isinstance(item, dict)
                    }
                    if reported_ids and reported_ids != present_ids:
                        errors.append(
                            "report.json check_ids do not match checks.json"
                        )
                    for item in checks:
                        if not isinstance(item, dict):
                            continue
                        if not item.get("outcome"):
                            errors.append(f"check {item.get('key')} has no outcome")
                        artifact = item.get("artifact_id")
                        if artifact and not (target / "artifacts" / artifact).is_file():
                            errors.append(
                                f"check {item.get('key')} cites a missing artifact: {artifact}"
                            )
            criteria_path = target / "criteria.json"
            if criteria_path.is_file():
                try:
                    criteria = json.loads(criteria_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    errors.append(f"criteria.json is not valid JSON: {exc}")
                    criteria = []
                if isinstance(criteria, list) and report.get("criteria") is not None:
                    if len(criteria) != len(report.get("criteria") or []):
                        errors.append("criteria.json and report.json disagree on criteria")
            if not report.get("blocking") and report.get("decision") in {
                "REJECTED",
                "BLOCKED",
                "INCONCLUSIVE",
            }:
                warnings.append(
                    "the decision is negative but no blocking reason was recorded"
                )
    else:
        errors.append("report.json is missing; the bundle has no decision")
    return BundleVerification(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        manifest=manifest,
        report=report,
    )


__all__ = [
    "write_bundle",
    "verify_bundle",
    "BundleResult",
    "BundleVerification",
    "MANIFEST_NAME",
    "REQUIRED_FILES",
]

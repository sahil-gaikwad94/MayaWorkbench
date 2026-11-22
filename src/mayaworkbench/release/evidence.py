"""Machine-readable release evidence from real inputs (WP-LAUNCH-1).

Design constraints (see ``docs/spec-launch-readiness.md``):

* The generator reads, never mutates, the repository and never executes
  repository code. Git is queried with bounded, shell-free subprocess calls.
* It never invents counts. Without a pytest report the test section is
  ``NOT_PROVIDED``, not a set of zeroes.
* Runtime capability comes from the single canonical probe in
  ``mayaworkbench.workspace.capabilities``.
* Secret values and the full environment are never written to the record; a
  redaction guard checks the payload before it is returned.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.clock import utcnow
from mayaworkbench.errors import MayaError
from mayaworkbench.workspace.capabilities import probe_execution_capability_report

SCHEMA_VERSION = "release-evidence/v1"
DEFAULT_TEST_COMMAND = "python -m pytest -q"

#: Status documents whose content hashes are worth pinning alongside the record.
STATUS_DOC_PATHS = (
    "README.md",
    "IMPLEMENTATION_STATUS.md",
    "docs/test-matrix.md",
    "docs/build-report.md",
)

_SECRET_NAME_PATTERNS = ("MAYA_*KEY*", "MAYA_*TOKEN*", "MAYA_*SECRET*", "AWS_*", "GH_*")
_INTEGER_RE = re.compile(r"[0-9]+")


class EvidenceError(MayaError):
    """A required evidence input is missing, unreadable or malformed."""

    code = "EVIDENCE_ERROR"
    exit_code = 2


class ReleaseEvidence(BaseModel):
    """The frozen release-evidence contract. Extra fields are forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-evidence/v1"] = SCHEMA_VERSION
    commit_sha: str
    worktree_dirty: bool
    python_version: str
    platform: str
    pytest_version: str
    pydantic_version: str
    dependency_manifest_sha256: str
    test_command: str
    tests: dict[str, Any]
    capability_report: dict[str, Any]
    adversarial: dict[str, Any]
    status_docs: dict[str, str]
    unverified: list[str]
    generated_at: datetime = Field(default_factory=utcnow)
    canonical_sha256: str


# --------------------------------------------------------------------- helpers
def _coerce_count(value: Any, label: str) -> int:
    """Accept a non-negative integer (or a decimal string) and nothing else."""
    if isinstance(value, bool):
        raise EvidenceError(f"{label} must be a non-negative integer, got boolean {value!r}")
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and _INTEGER_RE.fullmatch(value.strip()):
        count = int(value.strip())
    else:
        raise EvidenceError(f"{label} must be a non-negative integer, got {value!r}")
    if count < 0:
        raise EvidenceError(f"{label} must be non-negative, got {count}")
    return count


def _add(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _tests_result(
    *,
    collected: int | None,
    passed: int | None,
    failed: int | None,
    skipped: int | None,
    errors: int | None,
) -> dict[str, Any]:
    return {
        "status": "PROVIDED",
        "collected": collected,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
    }


def _not_provided_tests() -> dict[str, Any]:
    return {
        "status": "NOT_PROVIDED",
        "collected": None,
        "passed": None,
        "failed": None,
        "skipped": None,
        "errors": None,
    }


def _not_provided_adversarial() -> dict[str, Any]:
    return {
        "status": "NOT_PROVIDED",
        "total": None,
        "passed": None,
        "failed": None,
        "errors": None,
        "TOTAL": None,
        "PASS": None,
        "FAIL": None,
        "ERROR": None,
        "files": [],
    }


# -------------------------------------------------------------- pytest reports
def _parse_json_report(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON pytest report {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"JSON pytest report {path} must contain an object at the top level")
    summary = data.get("summary", data)
    if not isinstance(summary, dict):
        raise EvidenceError(f"pytest report {path}: 'summary' must be an object")

    def pick(*names: str) -> int | None:
        for name in names:
            if name in summary and summary[name] is not None:
                return _coerce_count(summary[name], f"{path.name}:{name}")
        return None

    return _tests_result(
        collected=pick("collected", "total"),
        passed=pick("passed"),
        failed=pick("failed"),
        skipped=pick("skipped"),
        errors=pick("error", "errors"),
    )


def _parse_junit_report(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EvidenceError(f"invalid JUnit XML pytest report {path}: {exc}") from exc

    def attributes(element: ET.Element) -> tuple[int | None, int | None, int | None, int | None]:
        def count(*names: str) -> int | None:
            for name in names:
                if name in element.attrib:
                    return _coerce_count(element.attrib[name], f"{path.name}:<{element.tag}> {name}")
            return None

        return (
            count("tests"),
            count("failures", "failed"),
            count("errors", "error"),
            count("skipped"),
        )

    if root.tag == "testsuites":
        # The aggregate attributes on <testsuites> are authoritative when present;
        # summing them with their <testsuite> children would double count.
        collected, failed, errors, skipped = attributes(root)
        if all(value is None for value in (collected, failed, errors, skipped)):
            for suite in root.iter("testsuite"):
                values = attributes(suite)
                collected, failed, errors, skipped = (
                    _add(collected, values[0]),
                    _add(failed, values[1]),
                    _add(errors, values[2]),
                    _add(skipped, values[3]),
                )
    elif root.tag == "testsuite":
        collected, failed, errors, skipped = attributes(root)
    else:
        raise EvidenceError(
            f"unsupported JUnit XML root <{root.tag}> in {path}: "
            "expected <testsuites> or <testsuite>"
        )

    if all(value is None for value in (collected, failed, errors, skipped)):
        raise EvidenceError(f"no test counts found in JUnit XML report {path}")

    # JUnit has no 'passed' attribute. Derive it by subtraction from real counts.
    passed: int | None = None
    if all(value is not None for value in (collected, failed, errors, skipped)):
        passed = max(0, collected - failed - errors - skipped)  # type: ignore[operator]
    return _tests_result(
        collected=collected,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
    )


def parse_pytest_report(path: Path | str) -> dict[str, Any]:
    """Parse a supplied pytest JSON or JUnit XML report into exact counts.

    A missing file is an error, never a set of zero counts. Callers that have no
    report should use :func:`_not_provided_tests` (via ``build_evidence``) so the
    status is ``NOT_PROVIDED``.
    """
    target = Path(path)
    if not target.is_file():
        raise EvidenceError(
            f"pytest report does not exist: {target}",
            hint="Pass --pytest-report PATH pointing to a pytest JSON or JUnit XML report.",
        )
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"cannot read pytest report {target}: {exc}") from exc
    stripped = raw.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped:
        raise EvidenceError(f"pytest report is empty: {target}")
    if stripped.startswith(b"{"):
        return _parse_json_report(raw, target)
    if stripped.startswith(b"<"):
        return _parse_junit_report(raw, target)
    raise EvidenceError(
        f"unsupported pytest report format in {target}: expected JSON or JUnit XML"
    )


# ------------------------------------------------------- adversarial harnesses
def _find_count(mapping: Mapping[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return name, mapping[name]
    return None


def parse_adversarial_results(directory: Path | str | None) -> dict[str, Any]:
    """Sum counts from ``results*.json`` files, or report ``NOT_PROVIDED``.

    Key names are tolerant (upper-case, lower-case and common aliases), while
    malformed files are a hard error rather than a silently ignored result.
    """
    if directory is None:
        return _not_provided_adversarial()
    root = Path(directory)
    if not root.is_dir():
        return _not_provided_adversarial()
    files = sorted(root.glob("results*.json"))
    if not files:
        return _not_provided_adversarial()

    totals = {"TOTAL": 0, "PASS": 0, "FAIL": 0, "ERROR": 0}
    aliases = {
        "TOTAL": ("TOTAL", "total", "collected"),
        "PASS": ("PASS", "pass", "passed"),
        "FAIL": ("FAIL", "fail", "failed"),
        "ERROR": ("ERROR", "error", "errors"),
    }
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"invalid adversarial results file {path}: {exc}") from exc
        # The project's harnesses write a JSON ARRAY of per-case records; other
        # producers may write an object with summary counts. Accept both.
        if isinstance(data, list):
            data = {"results": data}
        if not isinstance(data, dict):
            raise EvidenceError(
                f"adversarial results file {path} must contain a JSON object or array"
            )
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else data
        found = {
            key: _find_count(counts, names) for key, names in aliases.items()
        }
        if not any(found.values()):
            # Fall back to per-case outcome records when no summary counts exist.
            cases = data.get("results") or data.get("cases") or []
            if not isinstance(cases, list):
                raise EvidenceError(
                    f"adversarial results file {path} has no recognised counts or case list"
                )
            label = {
                "PASS": ("PASS", "PASSED", "OK"),
                "FAIL": ("FAIL", "FAILED"),
                "ERROR": ("ERROR", "ERRORS"),
            }
            for case in cases:
                if not isinstance(case, dict):
                    raise EvidenceError(f"adversarial case in {path} must be an object")
                outcome = str(case.get("outcome", case.get("status", ""))).upper()
                for key, values in label.items():
                    if outcome in values:
                        totals[key] += 1
                        totals["TOTAL"] += 1
                        break
        else:
            for key, hit in found.items():
                if hit is not None:
                    name, value = hit
                    totals[key] += _coerce_count(value, f"{path.name}:{name}")

    total, passed, failed, errors = (
        totals["TOTAL"], totals["PASS"], totals["FAIL"], totals["ERROR"]
    )
    return {
        "status": "PROVIDED",
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "TOTAL": total,
        "PASS": passed,
        "FAIL": failed,
        "ERROR": errors,
        "files": [path.name for path in files],
    }


# -------------------------------------------------------------------- redaction
def assert_no_secrets(payload: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
    """Refuse to emit a payload containing a secret value from the environment."""
    source: Mapping[str, str] = os.environ if env is None else env
    serialised = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    serialised_ascii = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
    for name, value in source.items():
        if not any(fnmatch.fnmatchcase(name.upper(), pattern) for pattern in _SECRET_NAME_PATTERNS):
            continue
        if not isinstance(value, str) or len(value) < 8:
            continue
        escaped = json.dumps(value)[1:-1]
        if (
            value in serialised
            or escaped in serialised
            or value in serialised_ascii
            or escaped in serialised_ascii
        ):
            raise EvidenceError(
                f"secret value from environment variable {name} would be written into release evidence",
                hint="Remove the secret from the evidence inputs and retry.",
            )


# --------------------------------------------------------------------- evidence
def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except Exception:  # noqa: BLE001 - versions are optional metadata
        return "unknown"


def _git(args: list[str], root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise EvidenceError("git executable was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise EvidenceError(f"git {' '.join(args)} timed out after 10 seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise EvidenceError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {detail or 'no diagnostic'}",
            detail=detail,
        )
    return result.stdout.strip()


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"cannot hash {path}: {exc}") from exc


def _unverified(
    capability: dict[str, Any], tests: dict[str, Any], adversarial: dict[str, Any]
) -> list[str]:
    """State honestly what this record does *not* prove."""
    notes: list[str] = []
    profile = capability.get("trust_profiles", {}).get("ROOTLESS_CONTAINER", {})
    if not profile.get("available"):
        reason = profile.get("reason") or "the capability report did not confirm availability"
        notes.append(
            f"ROOTLESS_CONTAINER is unavailable: {reason}; the live OCI container-escape "
            "gate was not exercised."
        )
    backend = capability.get("execution_backends", {}).get("rootless-container", {})
    smoke = backend.get("oci_verification", {}).get("smoke_test", {})
    if smoke.get("status") != "passed":
        notes.append(
            f"Rootless OCI smoke test status is {smoke.get('status', 'unknown')}; a live OCI "
            "gate requires its own verified runtime and is separate from this record."
        )
    network = capability.get("network_isolation", {})
    if not network.get("available"):
        notes.append(
            "Network isolation is unavailable: "
            f"{network.get('reason', 'no reason recorded')}"
        )
    notes.append(
        "The live-model gate is separate: no model call was made here. It requires "
        "MAYA_LIVE_MODEL=1, an injected key and a recorded spend cap."
    )
    if tests.get("status") != "PROVIDED":
        notes.append(
            "No pytest report was supplied: test counts are NOT_PROVIDED and no tests ran "
            "as part of generating this record."
        )
    if adversarial.get("status") != "PROVIDED":
        notes.append(
            "No adversarial results were supplied: adversarial counts are NOT_PROVIDED."
        )
    warning = capability.get("warning")
    if warning:
        notes.append(str(warning))
    return notes


def _canonical_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in {"generated_at", "canonical_sha256"}
    }


def _digest(data: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonical_payload(data),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_sha256(evidence: ReleaseEvidence) -> str:
    """Recompute the canonical hash (timestamps are deliberately excluded)."""
    return _digest(evidence.model_dump(mode="json"))


def build_evidence(
    *,
    root: Path,
    pytest_report: Path | None,
    test_command: str,
    adversarial_dir: Path | None = None,
    generated_at: datetime | str | None = None,
) -> ReleaseEvidence:
    """Build a release evidence record from real, already-available inputs."""
    repo_root = Path(root).expanduser()
    if not repo_root.is_dir():
        raise EvidenceError(f"repository root is not a directory: {repo_root}")

    commit_sha = _git(["rev-parse", "HEAD"], repo_root)
    if not commit_sha:
        raise EvidenceError(f"{repo_root} has no commit to attest (git rev-parse HEAD was empty)")
    worktree_dirty = bool(_git(["status", "--porcelain"], repo_root))

    manifest = repo_root / "pyproject.toml"
    if not manifest.is_file():
        raise EvidenceError(
            f"dependency manifest is missing: {manifest}",
            hint="Release evidence pins pyproject.toml; run from the repository root.",
        )
    dependency_manifest_sha256 = _hash_file(manifest)

    tests = (
        parse_pytest_report(pytest_report)
        if pytest_report is not None
        else _not_provided_tests()
    )
    adversarial = parse_adversarial_results(adversarial_dir)
    capability_report = probe_execution_capability_report().as_dict()
    status_docs = {
        relative: _hash_file(repo_root / relative)
        for relative in STATUS_DOC_PATHS
        if (repo_root / relative).is_file()
    }
    unverified = _unverified(capability_report, tests, adversarial)

    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "commit_sha": commit_sha,
        "worktree_dirty": worktree_dirty,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "pytest_version": _package_version("pytest"),
        "pydantic_version": _package_version("pydantic"),
        "dependency_manifest_sha256": dependency_manifest_sha256,
        "test_command": test_command,
        "tests": tests,
        "capability_report": capability_report,
        "adversarial": adversarial,
        "status_docs": status_docs,
        "unverified": unverified,
        "generated_at": generated_at if generated_at is not None else utcnow(),
    }
    assert_no_secrets(data)
    data["canonical_sha256"] = _digest(data)
    return ReleaseEvidence(**data)


# ------------------------------------------------------------------- rendering
def to_json(evidence: ReleaseEvidence) -> str:
    """Return the evidence record as a deterministic JSON document."""
    return evidence.model_dump_json(indent=2)


def render_markdown(evidence: ReleaseEvidence) -> str:
    """Render a concise human-readable view; the JSON file is authoritative."""
    lines = [
        "# Release evidence",
        "",
        f"- Schema: `{evidence.schema_version}`",
        f"- Commit: `{evidence.commit_sha}`",
        f"- Worktree dirty: {'yes' if evidence.worktree_dirty else 'no'}",
        f"- Generated at: {evidence.generated_at.isoformat()}",
        f"- Canonical SHA-256: `{evidence.canonical_sha256}`",
        "",
        "## Environment",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| python | {evidence.python_version} |",
        f"| platform | {evidence.platform} |",
        f"| pytest | {evidence.pytest_version} |",
        f"| pydantic | {evidence.pydantic_version} |",
        f"| dependency manifest sha256 | `{evidence.dependency_manifest_sha256}` |",
        "",
        "## Tests",
        "",
        f"Exact command: `{evidence.test_command}`",
        "",
        "| field | value |",
        "| --- | --- |",
    ]
    for key in ("status", "collected", "passed", "failed", "skipped", "errors"):
        value = evidence.tests.get(key)
        lines.append(f"| {key} | {value if value is not None else '—'} |")
    lines += ["", "## Adversarial results", ""]
    for key in ("status", "total", "passed", "failed", "errors", "files"):
        value = evidence.adversarial.get(key)
        lines.append(f"- {key}: {value if value not in (None, []) else '—'}")

    report = evidence.capability_report
    profile = report.get("trust_profiles", {}).get("ROOTLESS_CONTAINER", {})
    smoke = (
        report.get("execution_backends", {})
        .get("rootless-container", {})
        .get("oci_verification", {})
        .get("smoke_test", {})
    )
    lines += [
        "",
        "## Capability report (summary)",
        "",
        f"- Rootless container available: {profile.get('available', False)}",
        f"- Rootless container reason: {profile.get('reason', 'not recorded')}",
        f"- OCI smoke test: {smoke.get('status', 'not recorded')}",
        "- Full canonical report: see `capability_report` in release-evidence.json.",
        "",
        "## Status documents",
        "",
    ]
    if evidence.status_docs:
        for path, digest in sorted(evidence.status_docs.items()):
            lines.append(f"- `{path}` — `{digest}`")
    else:
        lines.append("- none present at generation time")
    lines += ["", "## Unverified / not exercised", ""]
    for note in evidence.unverified:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_TEST_COMMAND",
    "STATUS_DOC_PATHS",
    "EvidenceError",
    "ReleaseEvidence",
    "parse_pytest_report",
    "parse_adversarial_results",
    "assert_no_secrets",
    "build_evidence",
    "canonical_sha256",
    "to_json",
    "render_markdown",
]

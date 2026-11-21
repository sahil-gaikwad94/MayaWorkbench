"""Deterministic security evidence for a verification attempt (audit §11.2/§11.3).

This is a structured report assembled only from data the workbench already
recorded: the captured diff, the deterministic review, the contract and the
candidate workspace. It requires **no model** and makes no penetration-test or
sandbox claim. Every finding carries a severity and an evidence link to the
stored report artifact so a reader can follow it back to the raw data.

Checks are deliberately shallow and honest:

* ``scope`` — recorded scope violations / excluded paths / checkout changes;
* ``secrets`` — credential-shaped paths and high-confidence content markers
  (the same detectors the tool gateway uses), never secret values;
* ``dependencies`` — packaging changes and the declared install policy;
* ``network_policy`` — the contract policy and what command results observed;
* ``file_permissions`` — world-writable, setuid/setgid or symlinked changed
  files in the candidate tree.

An empty finding list means only "these deterministic checks found nothing",
not "the change is secure".
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from mayaworkbench.domain.enums import ArtifactKind
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.workspace.policy import contains_secret_material, is_secret_path

CATEGORIES = (
    "scope",
    "secrets",
    "dependencies",
    "network_policy",
    "file_permissions",
)
SEVERITIES = ("INFO", "WARNING", "ERROR")


@dataclass
class SecurityFinding:
    """One deterministic security observation with an evidence link."""

    code: str
    category: str
    severity: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "details": self.details,
            "evidence": self.evidence,
        }


def _info(code: str, category: str, message: str, **details: Any) -> SecurityFinding:
    return SecurityFinding(code, category, "INFO", message, details=details)


def _finding(
    code: str,
    category: str,
    severity: str,
    message: str,
    *,
    path: str | None = None,
    **details: Any,
) -> SecurityFinding:
    return SecurityFinding(code, category, severity, message, path=path, details=details)


def _severity_rank(value: str) -> int:
    return {name: index for index, name in enumerate(SEVERITIES)}.get(value, 0)


def _safe_relative(path: str) -> str | None:
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    parts = Path(path).parts
    if ".." in parts or "\x00" in path:
        return None
    return path


def _scope_findings(diff: Any, review: Any) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for path in diff.scope_violations or []:
        findings.append(
            _finding(
                "SCOPE_VIOLATION",
                "scope",
                "ERROR",
                "The captured patch changes a path outside the approved scope.",
                path=str(path)[:1000],
            )
        )
    if not diff.original_checkout_unchanged:
        findings.append(
            _finding(
                "ORIGINAL_CHECKOUT_CHANGED",
                "scope",
                "ERROR",
                "The original checkout changed while the task was running.",
            )
        )
    for item in review.findings if review is not None else []:
        if item.code in {"SCOPE_VIOLATION", "EXCLUDED_PATH", "SECRET_PATH"}:
            findings.append(
                _finding(
                    item.code,
                    "scope" if item.code != "SECRET_PATH" else "secrets",
                    "ERROR",
                    item.message,
                    path=item.path,
                )
            )
    if not findings:
        findings.append(
            _info(
                "SCOPE_OK",
                "scope",
                "The recorded diff stays inside the approved scope and the checkout is unchanged.",
                changed_files=len(diff.files or []),
            )
        )
    return findings


def _secret_findings(diff: Any, review: Any, workspace: Path | None) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    seen: set[str] = set()
    for item in review.findings if review is not None else []:
        if item.code not in {"SECRET_PATH", "SECRET_CONTENT"}:
            continue
        key = f"{item.code}:{item.path}"
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            _finding(
                item.code,
                "secrets",
                "ERROR",
                item.message,
                path=item.path,
                line=item.line,
            )
        )
    for file in diff.files or []:
        path = _safe_relative(file.path)
        if path is None or not is_secret_path(path):
            continue
        key = f"SECRET_PATH:{path}"
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            _finding(
                "SECRET_PATH",
                "secrets",
                "ERROR",
                "A changed file has a credential-shaped path.",
                path=path,
            )
        )
    if workspace is not None:
        for file in diff.files or []:
            path = _safe_relative(file.path)
            if path is None or is_secret_path(path) or file.binary:
                continue
            target = workspace / path
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if contains_secret_material(text):
                findings.append(
                    _finding(
                        "SECRET_CONTENT",
                        "secrets",
                        "ERROR",
                        "A changed file contains a high-confidence credential marker "
                        "(the value is not recorded here).",
                        path=path,
                    )
                )
    if not findings:
        findings.append(
            _info(
                "NO_SECRET_MARKERS",
                "secrets",
                "No credential-shaped path or high-confidence content marker was found "
                "in the recorded diff.",
            )
        )
    return findings


def _dependency_findings(contract: Any, review: Any) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for path in review.dependency_changes if review is not None else []:
        findings.append(
            _finding(
                "DEPENDENCY_CHANGE",
                "dependencies",
                "WARNING",
                "A dependency or packaging file changed; the change needs explicit justification.",
                path=str(path)[:1000],
            )
        )
    policy = getattr(getattr(contract, "constraints", None), "dependency_install", None)
    if policy and policy != "APPROVAL_REQUIRED":
        findings.append(
            _finding(
                "DEPENDENCY_POLICY_LOOSE",
                "dependencies",
                "WARNING",
                f"The contract declares dependency_install={policy}; installs are not "
                "approval-gated by this policy.",
                policy=str(policy),
            )
        )
    if not findings:
        findings.append(
            _info(
                "DEPENDENCIES_OK",
                "dependencies",
                "No dependency file changed and installs remain approval-gated.",
                policy=str(policy),
            )
        )
    return findings


def _network_findings(contract: Any, checks: Sequence[Any]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    policy = getattr(getattr(contract, "constraints", None), "network_access", None)
    if policy and policy != "DENY_BY_DEFAULT":
        findings.append(
            _finding(
                "NETWORK_POLICY_NOT_DENY_BY_DEFAULT",
                "network_policy",
                "WARNING",
                f"The contract declares network_access={policy}; egress is not denied by default.",
                policy=str(policy),
            )
        )
    observations = [
        str(check.details.get("network_enforcement"))
        for check in checks
        if isinstance(getattr(check, "details", None), Mapping)
        and check.details.get("network_enforcement")
    ]
    if policy == "DENY_BY_DEFAULT" and any(
        item in {"not_requested", "unavailable", "not_enforced"} for item in observations
    ):
        findings.append(
            _finding(
                "NETWORK_ENFORCEMENT_UNOBSERVED",
                "network_policy",
                "WARNING",
                "At least one executed check did not record verified network isolation "
                "under a DENY_BY_DEFAULT policy.",
                observations=sorted(set(observations)),
            )
        )
    if not findings:
        findings.append(
            _info(
                "NETWORK_POLICY_RECORDED",
                "network_policy",
                "The declared network policy and observed enforcement were recorded; "
                "this is a policy statement, not a network attack test.",
                policy=str(policy),
                observations=sorted(set(observations)),
            )
        )
    return findings


def _permission_findings(diff: Any, workspace: Path | None) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    if workspace is None:
        findings.append(
            _finding(
                "FILE_PERMISSIONS_UNCHECKED",
                "file_permissions",
                "WARNING",
                "The candidate workspace is unavailable, so changed-file permissions could not be inspected.",
            )
        )
        return findings
    for file in diff.files or []:
        path = _safe_relative(file.path)
        if path is None:
            continue
        target = workspace / path
        try:
            mode = target.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            findings.append(
                _finding(
                    "SYMLINK_CHANGED",
                    "file_permissions",
                    "WARNING",
                    "A changed path is a symbolic link; its target was not verified here.",
                    path=path,
                    mode=oct(stat.S_IMODE(mode)),
                )
            )
            continue
        if mode & 0o002:
            findings.append(
                _finding(
                    "WORLD_WRITABLE_FILE",
                    "file_permissions",
                    "ERROR",
                    "A changed file is world-writable.",
                    path=path,
                    mode=oct(stat.S_IMODE(mode)),
                )
            )
        if mode & 0o6000:
            findings.append(
                _finding(
                    "SETUID_OR_SETGID_FILE",
                    "file_permissions",
                    "ERROR",
                    "A changed file carries a setuid or setgid bit.",
                    path=path,
                    mode=oct(stat.S_IMODE(mode)),
                )
            )
    if not findings:
        findings.append(
            _info(
                "FILE_PERMISSIONS_OK",
                "file_permissions",
                "No world-writable, setuid/setgid or symlinked changed file was found.",
                changed_files=len(diff.files or []),
            )
        )
    return findings


def build_security_report(
    *,
    diff: Any,
    contract: Any,
    review: Any,
    artifacts: ArtifactStore,
    workspace: Path | str | None = None,
    checks: Iterable[Any] = (),
    run: Any = None,
) -> dict[str, Any]:
    """Build a structured, deterministic security report and store its evidence."""
    workspace_path = Path(workspace) if workspace is not None else None
    findings = [
        *_scope_findings(diff, review),
        *_secret_findings(diff, review, workspace_path),
        *_dependency_findings(contract, review),
        *_network_findings(contract, list(checks)),
        *_permission_findings(diff, workspace_path),
    ]
    findings.sort(key=lambda item: (CATEGORIES.index(item.category), -_severity_rank(item.severity), item.code))
    categories: dict[str, Any] = {}
    for category in CATEGORIES:
        items = [item for item in findings if item.category == category]
        categories[category] = {
            "findings": len(items),
            "max_severity": max(
                (item.severity for item in items), key=_severity_rank, default="INFO"
            ),
        }
    stored = {
        "schema_version": "1.0",
        "run_id": getattr(run, "id", None),
        "categories": categories,
        "findings": [item.as_dict() for item in findings],
        "note": (
            "Deterministic checks over recorded evidence only. No model is used. "
            "An empty result is not a security guarantee and is not a penetration test."
        ),
        "model_required": False,
    }
    artifact = artifacts.put_text(
        json.dumps(stored, indent=2, sort_keys=True),
        kind=ArtifactKind.OTHER,
        media_type="application/json",
        created_by="security-report",
    )
    evidence_links: list[dict[str, Any]] = []
    for item in findings:
        item.evidence = {
            "artifact_id": artifact.id,
            "from_kind": "SECURITY_FINDING",
            "from_ref": item.code,
            "relation": "RECORDED_IN",
            "to_kind": "ARTIFACT",
            "to_ref": artifact.id,
            "note": f"{item.category}:{item.severity}",
        }
        evidence_links.append(dict(item.evidence))
    evidence_links.append(
        {
            "from_kind": "CHECK",
            "from_ref": "security.report",
            "relation": "RECORDED_IN",
            "to_kind": "ARTIFACT",
            "to_ref": artifact.id,
            "note": "deterministic security report",
        }
    )
    stored["findings"] = [item.as_dict() for item in findings]
    return {
        **stored,
        "artifact_id": artifact.id,
        "evidence_links": evidence_links,
    }


__all__ = ["CATEGORIES", "SEVERITIES", "SecurityFinding", "build_security_report"]

"""Shared, typed contracts for advanced connectors (WP12).

This module contains no I/O of its own. It defines the permission contract every
advanced connector publishes, the provenance helpers used to hash requests and
responses, the approval grant that the registry injects for side-effecting
operations, and a small path guard used by every local-file connector.

Security model (Honest limits)
------------------------------
These connectors are *read-only by default*. The only side effect in this
package is a GitHub **draft PR proposal**, which is gated by a one-time,
spec-hash-bound approval issued by :mod:`mayaworkbench.models.connectors.registry`.

Local file connectors run with the host user's privileges (``LOCAL_TRUSTED``).
They are guardrails against accidental or model-proposed misuse, not a sandbox:
they do not contain a malicious local process, and no such claim is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult

#: Reserved parameter key through which the *registry* (never a model) injects a
#: verified approval grant into a side-effecting connector call.
APPROVAL_PARAM = "__approval_grant"

#: Existing workbench policy concept reused for read-only connectors.
CONNECTOR_READ_SCOPE = "connector.read"


class ConnectorContract(BaseModel):
    """Static, non-secret permission contract for one connector."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    permissions: list[str] = Field(default_factory=list)
    #: Existing policy/approval concept this connector routes through.
    policy_concept: str = CONNECTOR_READ_SCOPE
    approval_action: str = "WRITE_ACCESS"
    risk: str = "LOW"
    #: Operation names that are not read-only (empty for fully read-only connectors).
    side_effects: list[str] = Field(default_factory=list)
    #: Deterministic evidence artifacts a caller can expect from a call.
    evidence_outputs: list[str] = Field(default_factory=list)
    never_merges: bool = True
    never_applies: bool = True
    #: Maps an operation to the consent scope required to invoke it.
    operation_scopes: dict[str, str] = Field(default_factory=dict)


class ApprovalGrant(BaseModel):
    """One-time, tamper-evident proof that a human approved an exact action."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    connector: str
    operation: str
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str


def public_params(params: dict[str, Any]) -> dict[str, Any]:
    """Strip registry-internal keys before hashing or showing an action."""
    return {key: value for key, value in params.items() if not key.startswith("__")}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    """Deterministic SHA-256 of a JSON-serialisable value."""
    return hashlib.sha256(_canonical(value)).hexdigest()


def action_spec(
    *, connector: str, operation: str, target: str, params: dict[str, Any]
) -> dict[str, Any]:
    """The exact, hashable description of an action shown to an approver."""
    return {
        "connector": connector,
        "operation": operation,
        "target": target,
        "params": public_params(params),
    }


def spec_sha256(spec: dict[str, Any]) -> str:
    return sha256_json(spec)


def response_hash(data: Any) -> str:
    return hashlib.sha256(_canonical(data)).hexdigest()


def make_result(
    *,
    connector: str,
    operation: str,
    target: str,
    data: Any,
    request_payload: dict[str, Any],
    raw_response: bytes | None = None,
    read_only: bool = True,
) -> ConnectorResult:
    """Build a provenance-bearing result without ever including a token."""
    return ConnectorResult(
        connector=connector,
        operation=operation,
        target=target,
        data=data,
        request_hash=sha256_json(request_payload),
        response_hash=hashlib.sha256(raw_response).hexdigest()
        if raw_response is not None
        else response_hash(data),
        retrieved_at=to_iso(utcnow()),
        read_only=read_only,
    )


_INLINE_SECRET_KEYS = frozenset(
    {"token", "secret", "password", "passwd", "api_key", "apikey", "authorization", "credential"}
)


def reject_inline_secrets(params: dict[str, Any]) -> None:
    """Refuse credentials supplied in parameters; tokens come from the env only."""
    for key in params:
        if str(key).lower() in _INLINE_SECRET_KEYS or str(key).lower().endswith("_token"):
            raise PolicyError(
                f"Connector parameters must not contain credentials ({key!r})",
                hint="Credentials are read from the environment only, never from a call.",
            )


# --------------------------------------------------------------------- redaction
_BEARER_RE = re.compile(r"(?i)\b(?P<pre>Bearer\s+)(?P<val>[A-Za-z0-9._\-]{8,})")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>api[_-]?key|token|password|secret|authorization)"
    r"(?P<sep>\s*[:=]\s*[\"']?)(?P<val>[A-Za-z0-9._\-/+]{6,})"
)
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{8,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")


def redact_text(text: str) -> tuple[str, int]:
    """Return redacted text and the number of redactions applied."""
    redactions = 0

    def replace_bearer(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group('pre')}[REDACTED]"

    def replace_kv(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group('key')}{match.group('sep')}[REDACTED]"

    text, n = _BEARER_RE.subn(replace_bearer, text)
    redactions += n
    text, n = _KV_SECRET_RE.subn(replace_kv, text)
    redactions += n
    for pattern in (_GITHUB_TOKEN_RE, _OPENAI_KEY_RE, _AWS_KEY_RE, _JWT_RE):
        text, n = pattern.subn("[REDACTED]", text)
        redactions += n
    return text, redactions


# ---------------------------------------------------------------------- path guard
Resolver = Callable[[str], str]
ExistenceCheck = Callable[[str], bool]
Opener = Callable[..., Any]


def _default_resolver(path: str) -> str:
    return str(Path(path).expanduser().resolve())


class PathGuard:
    """Canonical-path containment against an explicit allowlist of directories.

    Symlinks are resolved before comparison (``Path.resolve`` / an injected
    resolver), so a link inside an allowed directory that points outside it is
    refused. This is a host-user guardrail, not a sandbox.
    """

    def __init__(
        self,
        allowed_dirs: Any,
        *,
        resolver: Resolver | None = None,
        exists: ExistenceCheck | None = None,
        is_file: ExistenceCheck | None = None,
    ):
        self._roots = tuple(str(directory) for directory in (allowed_dirs or ()))
        if not self._roots:
            raise PolicyError(
                "No filesystem directory is allowlisted for this connector",
                hint="Pass allowed_dirs explicitly; the connector fails closed by default.",
            )
        self._resolver = resolver or _default_resolver
        self._exists = exists or (lambda path: Path(path).exists())
        self._is_file = is_file or (lambda path: Path(path).is_file())

    @property
    def allowed_dirs(self) -> tuple[str, ...]:
        return self._roots

    def check(
        self,
        path: Any,
        *,
        must_exist: bool = True,
        must_be_file: bool = True,
        label: str = "path",
    ) -> str:
        raw = str(path or "")
        if not raw:
            raise ConnectorError(f"A {label} is required")
        resolved = self._resolver(raw)
        for root in self._roots:
            root_resolved = self._resolver(root)
            if resolved == root_resolved or resolved.startswith(root_resolved + os.sep):
                if must_exist and not self._exists(resolved):
                    raise ConnectorError(f"{label.capitalize()} does not exist: {raw[:120]!r}")
                if must_exist and must_be_file and not self._is_file(resolved):
                    raise ConnectorError(f"{label.capitalize()} is not a regular file: {raw[:120]!r}")
                return resolved
        raise PolicyError(
            f"{label.capitalize()} is outside the connector filesystem allowlist",
            hint=f"Allowed directories: {', '.join(self._roots)}",
        )

    def display(self, resolved: str) -> str:
        """Stable, relative display name for provenance (never an absolute secret path)."""
        for root in self._roots:
            root_resolved = self._resolver(root)
            if resolved == root_resolved:
                return "."
            if resolved.startswith(root_resolved + os.sep):
                return resolved[len(root_resolved) + 1 :]
        return resolved


__all__ = [
    "APPROVAL_PARAM",
    "CONNECTOR_READ_SCOPE",
    "ConnectorContract",
    "ApprovalGrant",
    "PathGuard",
    "public_params",
    "sha256_json",
    "action_spec",
    "spec_sha256",
    "response_hash",
    "make_result",
    "reject_inline_secrets",
    "redact_text",
    "Resolver",
    "ExistenceCheck",
    "Opener",
]

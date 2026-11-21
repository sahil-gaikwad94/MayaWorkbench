"""Read-only log/tail connector over an allowlisted directory (WP12).

Reads only a bounded number of trailing bytes from a path-contained file,
drops a partial first line, caps the number of lines, and redacts credentials
before returning any content. The file opener and path checks are injectable,
so tests never touch a real log directory.

This is a host-user (``LOCAL_TRUSTED``) read guardrail, not a sandbox.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult
from mayaworkbench.models.connectors.contracts import (
    CONNECTOR_READ_SCOPE,
    ConnectorContract,
    PathGuard,
    make_result,
    redact_text,
    reject_inline_secrets,
)

CONTRACT = ConnectorContract(
    id="observability",
    version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "logs:read"],
    risk="MEDIUM",
    side_effects=[],
    evidence_outputs=["lines", "bytes_read", "redactions", "truncated", "request_hash", "response_hash"],
    operation_scopes={"tail": CONNECTOR_READ_SCOPE, "list_files": CONNECTOR_READ_SCOPE},
)


class ObservabilityConnector:
    """Tail/list log files within an explicit directory allowlist."""

    name = "observability"
    mode = "READ_ONLY"
    contract = CONTRACT

    def __init__(
        self,
        *,
        allowed_dirs: Any = None,
        open_file: Callable[..., Any] | None = None,
        resolver: Callable[[str], str] | None = None,
        exists: Callable[[str], bool] | None = None,
        is_file: Callable[[str], bool] | None = None,
        list_dir: Callable[[str], Any] | None = None,
        token_env: str = "MAYA_OBSERVABILITY_TOKEN",
        max_bytes: int = 65_536,
        max_lines: int = 200,
    ):
        self._guard = PathGuard(allowed_dirs, resolver=resolver, exists=exists, is_file=is_file)
        self._open = open_file or open
        self._list_dir = list_dir or os.listdir
        self.token_env = token_env
        self.max_bytes = int(max_bytes)
        self.max_lines = int(max_lines)
        if self.max_bytes <= 0 or self.max_lines <= 0:
            raise ConnectorError("max_bytes and max_lines must be positive")
        self._state: dict[str, Any] = {}
        self._revoked = False

    def operations(self) -> tuple[str, ...]:
        return ("tail", "list_files")

    def supports(self, operation: str) -> bool:
        return operation in self.operations()

    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        resolved = self._guard.check(
            params.get("path"), label="log path", must_be_file=(operation == "tail")
        )
        return "file:" + self._guard.display(resolved)

    def revoke(self) -> None:
        self._state.clear()
        self._revoked = True

    def is_revoked(self) -> bool:
        return self._revoked

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "operations": list(self.operations()),
            "allowed_dirs": list(self._guard.allowed_dirs),
            "token_env": self.token_env,
            "writes_supported": False,
            "max_bytes": self.max_bytes,
            "max_lines": self.max_lines,
            "never_merges": True,
            "contract": self.contract.model_dump(mode="json"),
            "revoked": self._revoked,
            "note": (
                "Bounded trailing read with credential redaction. No writes, no "
                "process execution, no network. Host-user privilege (LOCAL_TRUSTED)."
            ),
        }

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError("Observability connector has been revoked; re-create it before use")
        if not self.supports(operation):
            raise ConnectorError(
                f"Observability connector does not support {operation!r}",
                hint=f"Allowed operations: {', '.join(self.operations())}",
            )
        if operation == "tail":
            result = self._tail(params)
        else:
            result = self._list(params)
        self._state[operation] = result
        return result

    def _tail(self, params: dict[str, Any]) -> ConnectorResult:
        resolved = self._guard.check(params.get("path"), label="log path")
        display = self._guard.display(resolved)
        byte_cap = self._bounded_int(params.get("max_bytes"), self.max_bytes, "max_bytes")
        line_cap = self._bounded_int(params.get("lines"), 50, "lines", upper=self.max_lines)

        with self._open(resolved, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - byte_cap)
            handle.seek(start)
            chunk = handle.read(size - start)
        if isinstance(chunk, str):  # tolerate injected text openers
            chunk = chunk.encode("utf-8", errors="replace")

        text = chunk.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # discard the partial first line
        selected = lines[-line_cap:]
        redacted: list[str] = []
        redactions = 0
        for line in selected:
            cleaned, count = redact_text(line)
            redacted.append(cleaned)
            redactions += count
        data = {
            "path": display,
            "lines": redacted,
            "line_count": len(redacted),
            "bytes_read": len(chunk),
            "redactions": redactions,
            "truncated": start > 0 or len(lines) > line_cap,
        }
        return make_result(
            connector=self.name,
            operation="tail",
            target="file:" + display,
            data=data,
            request_payload={"path": display, "lines": line_cap, "max_bytes": byte_cap},
            raw_response=chunk,
        )

    def _list(self, params: dict[str, Any]) -> ConnectorResult:
        resolved = self._guard.check(
            params.get("path"), label="log directory", must_exist=True, must_be_file=False
        )
        display = self._guard.display(resolved)
        try:
            names = sorted(str(name) for name in self._list_dir(resolved))
        except OSError as exc:
            raise ConnectorError(f"Cannot list the allowlisted log directory: {exc}") from exc
        files = [name for name in names if "." in name][: self.max_lines]
        data = {"path": display, "files": files, "truncated": len(names) > len(files)}
        return make_result(
            connector=self.name,
            operation="list_files",
            target="file:" + display,
            data=data,
            request_payload={"path": display, "operation": "list_files"},
        )

    @staticmethod
    def _bounded_int(value: Any, default: int, label: str, *, upper: int | None = None) -> int:
        if value in (None, ""):
            return default
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ConnectorError(f"{label} must be a positive integer") from exc
        if number <= 0:
            raise ConnectorError(f"{label} must be a positive integer")
        return min(number, upper) if upper is not None else min(number, default)


__all__ = ["CONTRACT", "ObservabilityConnector"]

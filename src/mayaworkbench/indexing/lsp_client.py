"""Optional, fail-closed Language Server Protocol client (FIX-06).

A real language server can resolve definitions, references and hover across a
pinned snapshot; syntax trees alone cannot. This adapter is deliberately
opt-in: without ``MAYA_LSP_ENABLE=1`` and an allowlisted server binary on PATH it
returns a typed ``unsupported`` reason and never starts a process.

LSP results are *advisory index evidence*. They are never used to change workflow
state, grant authority, or pass a verification gate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mayaworkbench.errors import MayaError

Kind = Literal["LSP_DEFINITION", "LSP_REFERENCE", "LSP_HOVER"]

#: Exact binaries and stdio argv. No shell, no arbitrary command from callers.
SERVER_PROFILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("pyright-langserver", ("--stdio",)),
    "typescript": ("typescript-language-server", ("--stdio",)),
    "javascript": ("typescript-language-server", ("--stdio",)),
    "tsx": ("typescript-language-server", ("--stdio",)),
    "jsx": ("typescript-language-server", ("--stdio",)),
    "rust": ("rust-analyzer", ()),
    "go": ("gopls", ()),
}
_SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONIOENCODING")
_TIMEOUT_SECONDS = 15.0
_MAX_MESSAGE_BYTES = 4_000_000
_MAX_RESULTS = 200


class LspError(MayaError):
    """A typed LSP transport/protocol failure."""

    def __init__(self, message: str, *, code: str = "LSP_ERROR", exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class LspQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    language: str
    snapshot_id: str | None = None

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if value.startswith("/") or "\x00" in value or ".." in Path(value).parts:
            raise ValueError("LSP query path must be snapshot-relative")
        return value


class LspResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ANSWERED"] = "ANSWERED"
    kind: Kind
    path: str
    line: int
    column: int
    language: str
    snapshot_id: str | None = None
    targets: list[dict[str, Any]] = Field(default_factory=list)
    hover: str | None = None
    server: str
    server_version: str | None = None
    provenance: str
    confidence: Literal["HIGH"] = "HIGH"
    note: str = "Advisory index evidence from a language server; not verification."


class LspUnsupported(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["UNSUPPORTED"] = "UNSUPPORTED"
    reason: str
    language: str
    snapshot_id: str | None = None


class LspErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ERROR"] = "ERROR"
    reason: str
    code: str
    language: str
    snapshot_id: str | None = None


@dataclass(frozen=True)
class _Profile:
    binary: str
    argv: tuple[str, ...]
    path: str


def _profile_for(language: str) -> _Profile | None:
    spec = SERVER_PROFILES.get(language.lower())
    if spec is None:
        return None
    binary, argv = spec
    path = shutil.which(binary)
    if path is None:
        return None
    return _Profile(binary, argv, path)


def lsp_unsupported_reason(language: str, *, snapshot_path: str | Path | None = None) -> str | None:
    """Return a concrete reason when an LSP query cannot be served."""
    if os.environ.get("MAYA_LSP_ENABLE", "").lower() not in {"1", "true", "yes"}:
        return "LSP is disabled: set MAYA_LSP_ENABLE=1 to allow a local language server"
    if language.lower() not in SERVER_PROFILES:
        return f"no allowlisted LSP server profile for language {language!r}"
    if _profile_for(language) is None:
        binary = SERVER_PROFILES[language.lower()][0]
        return f"allowlisted language server {binary!r} is not on PATH"
    if snapshot_path is not None and not Path(snapshot_path).is_dir():
        return "the pinned snapshot root is not available for an LSP query"
    return None


class LspClient:
    """A bounded stdio JSON-RPC 2.0 LSP session over one pinned snapshot."""

    def __init__(
        self,
        profile: _Profile,
        root: Path,
        snapshot_id: str | None = None,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        opener: Any = None,
    ):
        self.profile = profile
        self.root = Path(root).resolve()
        self.snapshot_id = snapshot_id
        self.timeout = float(timeout)
        self._opener = opener  # test-only injection; defaults to subprocess.Popen
        self._proc: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._opened: set[str] = set()
        self._version: str | None = None

    # ------------------------------------------------------------- lifecycle
    def _spawn(self) -> None:
        if self._proc is not None:
            return
        env = {key: os.environ[key] for key in _SAFE_ENV if key in os.environ}
        command = [self.profile.path, *self.profile.argv]
        try:
            factory = self._opener or subprocess.Popen
            self._proc = factory(
                command,
                cwd=str(self.root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                bufsize=0,
            )
        except OSError as exc:
            raise LspError(f"language server could not start: {exc}", code="LSP_SPAWN_FAILED") from exc

    def _send(self, method: str, params: dict[str, Any]) -> int:
        self._request_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
            separators=(",", ":"),
        ).encode("utf-8")
        data = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise LspError("language server is not running", code="LSP_NOT_RUNNING")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise LspError(f"language server stopped: {exc}", code="LSP_TRANSPORT") from exc
        return self._request_id

    def _read(self, expected_id: int) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise LspError("language server is not running", code="LSP_NOT_RUNNING")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                chunk = proc.stdout.readline()
                if not chunk:
                    raise LspError("language server closed the stream", code="LSP_EOF", exit_code=1)
                header += chunk
                if len(header) > 8192:
                    raise LspError("LSP header too large", code="LSP_PROTOCOL")
            length: int | None = None
            for line in header.decode("ascii", "replace").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError as exc:
                        raise LspError("invalid LSP Content-Length", code="LSP_PROTOCOL") from exc
            if length is None or length < 1 or length > _MAX_MESSAGE_BYTES:
                raise LspError("invalid LSP message length", code="LSP_PROTOCOL")
            body = proc.stdout.read(length)
            if len(body) != length:
                raise LspError("truncated LSP message", code="LSP_PROTOCOL")
            try:
                message = json.loads(body)
            except json.JSONDecodeError as exc:
                raise LspError("invalid LSP JSON", code="LSP_PROTOCOL") from exc
            if not isinstance(message, dict):
                continue
            if message.get("id") == expected_id:
                return message
            # Notifications and responses for other ids are advisory; ignore.
        raise LspError("language server timed out", code="LSP_TIMEOUT", exit_code=1)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._send(method, params)
        message = self._read(request_id)
        if "error" in message:
            detail = str(message["error"].get("message", "LSP error"))[:300]
            raise LspError(f"language server error: {detail}", code="LSP_ERROR")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _initialize(self) -> None:
        if self._version is not None:
            return
        result = self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root.as_uri(),
                "capabilities": {},
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": "snapshot"}],
            },
        )
        info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        self._version = str(info.get("version")) if info.get("version") else None
        self._send("initialized", {})

    def _did_open(self, path: str) -> None:
        if path in self._opened:
            return
        target = self._resolve(path)
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LspError(f"cannot read snapshot file {path!r}: {exc}", code="LSP_FILE") from exc
        self._send(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": target.as_uri(),
                    "languageId": self.profile.binary,
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._opened.add(path)

    # ---------------------------------------------------------------- queries
    def _resolve(self, path: str) -> Path:
        candidate = (self.root / path).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise LspError(f"LSP path escapes the pinned snapshot: {path}", code="LSP_PATH_ESCAPE")
        if candidate.is_symlink():
            raise LspError(f"symlinks are not allowed in LSP queries: {path}", code="LSP_PATH_ESCAPE")
        return candidate

    def query(self, query: LspQuery) -> LspResult:
        self._spawn()
        self._initialize()
        self._did_open(query.path)
        uri = (self.root / query.path).resolve(strict=False).as_uri()
        position = {"line": query.line - 1, "character": query.column - 1}
        params: dict[str, Any] = {"textDocument": {"uri": uri}, "position": position}
        targets: list[dict[str, Any]] = []
        hover: str | None = None
        if query.kind == "LSP_DEFINITION":
            result = self._request("textDocument/definition", params)
            targets = self._locations(result)
        elif query.kind == "LSP_REFERENCE":
            params["context"] = {"includeDeclaration": True}
            result = self._request("textDocument/references", params)
            targets = self._locations(result)
        else:
            result = self._request("textDocument/hover", params)
            contents = result.get("contents") if isinstance(result.get("contents"), dict) else None
            value = contents.get("value") if contents else result.get("value")
            hover = str(value) if value is not None else None
        return LspResult(
            kind=query.kind,
            path=query.path,
            line=query.line,
            column=query.column,
            language=query.language,
            snapshot_id=query.snapshot_id,
            targets=targets[:_MAX_RESULTS],
            hover=hover,
            server=self.profile.binary,
            server_version=self._version,
            provenance={"LSP_DEFINITION": "LSP_DEFINITION", "LSP_REFERENCE": "LSP_REFERENCE", "LSP_HOVER": "LSP_HOVER"}[query.kind],
        )

    @staticmethod
    def _locations(result: Any) -> list[dict[str, Any]]:
        rows = result if isinstance(result, list) else ([result] if isinstance(result, dict) else [])
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            uri = str(row.get("uri") or "")
            start = row.get("range", {}).get("start", {}) if isinstance(row.get("range"), dict) else {}
            out.append(
                {
                    "uri": uri,
                    "path": uri.rsplit("/", 1)[-1] if uri else "",
                    "line": int(start.get("line", 0)) + 1,
                    "column": int(start.get("character", 0)) + 1,
                }
            )
        return out

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            self._send("shutdown", {})
        except LspError:
            pass
        try:
            self._send("exit", {})
        except LspError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover - hostile server
                proc.kill()
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:  # pragma: no cover
                pass
        self._proc = None


def lsp_adapter_for(
    language: str,
    *,
    snapshot_path: str | Path | None = None,
    snapshot_id: str | None = None,
) -> LspClient | None:
    """Return a client only when a verified local server is explicitly enabled."""
    if lsp_unsupported_reason(language, snapshot_path=snapshot_path) is not None:
        return None
    profile = _profile_for(language)
    if profile is None or snapshot_path is None:
        return None
    return LspClient(profile, Path(snapshot_path), snapshot_id)


__all__ = [
    "LspClient",
    "LspError",
    "LspErrorPayload",
    "LspQuery",
    "LspResult",
    "LspUnsupported",
    "SERVER_PROFILES",
    "lsp_adapter_for",
    "lsp_unsupported_reason",
]

"""MCP interoperability adapter and stdio transport (audit §8.1, MW-10).

MCP is a tool-description **transport**, not a permission model. Every remote
tool list is untrusted data: the local gateway still decides scope, consent,
approval and audit. This module ships two layers:

* :class:`McpStdioTransport` — a real stdio JSON-RPC 2.0 client that spawns a
  locally allowlisted server with a sanitized environment and bounded reads;
* :class:`McpServerAdapter` — validates tool contracts and routes calls through
  either the original injected ``executor`` or the transport. It never grants
  authority itself.

:class:`McpConnector` is the ``ToolConnector`` view used by
``AdvancedConnectorRegistry``: discovered tools become registry operations and
writes are gated by the registry's one-time, spec-hash-bound approval grant.
No raw tool output, argument or secret is ever logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult
from mayaworkbench.models.connectors.contracts import (
    APPROVAL_PARAM,
    reject_inline_secrets,
    redact_text,
    spec_sha256,
)
from mayaworkbench.domain.enums import ToolCapability

Executor = Callable[[str, dict[str, Any]], dict[str, Any]]

# ---------------------------------------------------------------- risk tiers
RISK_READ_ONLY = "READ_ONLY"
RISK_LOCAL_MUTATION = "LOCAL_MUTATION"
RISK_EXTERNAL_WRITE = "EXTERNAL_WRITE"
VALID_RISKS: frozenset[str] = frozenset(
    {RISK_READ_ONLY, RISK_LOCAL_MUTATION, RISK_EXTERNAL_WRITE}
)

#: A declared tier always maps to a typed grant. Unknown tools deliberately
#: map to the broadest grant so they fail closed.
RISK_TO_CAPABILITY: dict[str, str] = {
    RISK_READ_ONLY: ToolCapability.CONNECTOR_READ.value,
    RISK_LOCAL_MUTATION: ToolCapability.FILE_WRITE.value,
    RISK_EXTERNAL_WRITE: ToolCapability.ADVANCED_TOOL.value,
}
HIGHEST_RISK_CAPABILITY: str = ToolCapability.ADVANCED_TOOL.value
DEFAULT_RISK: str = RISK_EXTERNAL_WRITE

_SECRET_KEY_RE = re.compile(
    r"(?i)(key|token|secret|password|passwd|credential|authorization|api[_-]?key)"
    r"|^((aws|azure|gcp|maya)_)"
)


def _safe_error(exc: BaseException) -> str:
    """Error detail for messages only; never include tool output or arguments."""
    return redact_text(str(exc))[0][:300]


@dataclass(frozen=True)
class McpToolDeclaration:
    """Local, operator-owned declaration of a remote tool's trust tier.

    The remote server never supplies its own risk: that would let untrusted data
    grant itself authority. Anything not declared here defaults to
    ``EXTERNAL_WRITE`` and the broadest capability.
    """

    risk: str
    tool_capability: str | None = None
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.risk not in VALID_RISKS:
            raise PolicyError(
                f"invalid MCP risk tier {self.risk!r}; expected one of {sorted(VALID_RISKS)}"
            )


@dataclass(frozen=True)
class McpToolContract:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    capabilities: tuple[str, ...] = ()
    risk: str = "UNKNOWN"
    tool_capability: str = HIGHEST_RISK_CAPABILITY

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description[:500],
            "capabilities": list(self.capabilities),
            "risk": self.risk,
            "tool_capability": self.tool_capability,
            "contract_sha256": hashlib.sha256(
                json.dumps(self.input_schema, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }


# ------------------------------------------------------------------- transport
class McpStdioTransport:
    """Real stdio JSON-RPC 2.0 client for a locally spawned MCP server.

    Security properties (all enforced here, none delegated to the server):

    * the server is spawned from an exact argv allowlist with ``shell=False``;
    * only a small, secret-free environment is forwarded;
    * stdout reads are bounded and guarded by a per-request overall timeout;
    * frames are matched by response id and JSON-RPC errors become
      :class:`ConnectorError`;
    * the child is terminated and its pipes closed on every exit path;
    * no raw tool output or argument is ever logged (errors are redacted).
    """

    PROTOCOL_VERSION = "2024-11-05"
    DEFAULT_SAFE_ENV: tuple[str, ...] = (
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "PYTHONIOENCODING",
    )

    def __init__(
        self,
        argv: Any,
        *,
        allowed_argv: Any = None,
        env_allowlist: Any = None,
        timeout_seconds: float = 10.0,
        max_line_bytes: int = 1_000_000,
    ):
        argv_tuple = tuple(str(item) for item in (argv or ()))
        if not argv_tuple or any("\x00" in item for item in argv_tuple):
            raise PolicyError("MCP argv must be a non-empty list of NUL-free arguments")
        if allowed_argv is not None:
            options = tuple(tuple(str(item) for item in option) for option in allowed_argv)
            if argv_tuple not in options:
                raise PolicyError("MCP argv is not in the operator allowlist")
        self.argv: tuple[str, ...] = argv_tuple
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise PolicyError("MCP timeout must be positive")
        self.max_line_bytes = int(max_line_bytes)
        if self.max_line_bytes <= 0:
            raise PolicyError("MCP max_line_bytes must be positive")
        self._env_allowlist: tuple[str, ...] = (
            tuple(str(key) for key in env_allowlist)
            if env_allowlist is not None
            else self.DEFAULT_SAFE_ENV
        )
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_sink: Any = None
        self._buffer = bytearray()
        self._request_id = 0
        self._initialized = False

    # ------------------------------------------------------------- public api
    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def describe(self) -> dict[str, Any]:
        """Non-secret configuration view (argv values are redacted)."""
        return {
            "mode": "STDIO_JSONRPC",
            "argv": [redact_text(item)[0] for item in self.argv],
            "timeout_seconds": self.timeout_seconds,
            "max_line_bytes": self.max_line_bytes,
            "environment_keys": sorted(self._sanitized_env()),
            "initialized": self._initialized,
        }

    def connect(self) -> None:
        """Spawn the server (if needed) and perform the MCP handshake once."""
        if self.is_running():
            if self._initialized:
                return
        else:
            if self._proc is not None:
                self.close()
            self._spawn()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        """Perform the handshake and return the untrusted ``tools/list`` data."""
        try:
            self.connect()
            result = self._request("tools/list", {})
            tools = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(tools, list):
                raise ConnectorError("MCP tools/list did not return a tool list")
            for tool in tools:
                if not isinstance(tool, dict):
                    raise ConnectorError("MCP tools/list returned a malformed tool entry")
            return tools
        except Exception:
            self.close()
            raise

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Validate arguments and perform a ``tools/call`` JSON-RPC request."""
        try:
            self.connect()
            if (
                not isinstance(name, str)
                or not name
                or "/" in name
                or len(name) > 120
            ):
                raise ConnectorError("Invalid MCP tool name")
            if not isinstance(arguments, dict):
                raise ConnectorError("MCP tool arguments must be a JSON object")
            try:
                reject_inline_secrets(arguments)
            except PolicyError as exc:
                raise ConnectorError(str(exc)) from exc
            try:
                json.dumps(arguments, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ConnectorError("MCP tool arguments are not valid JSON") from exc
            return self._request("tools/call", {"name": name, "arguments": arguments})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Always terminate the child and close its pipes, on every exit path."""
        proc = self._proc
        self._proc = None
        self._initialized = False
        self._buffer.clear()
        if proc is None:
            return
        # Signal before closing stdin: closing it first would let a server
        # exit on EOF without observing the termination request.
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
        try:
            if proc.poll() is not None:
                proc.wait(timeout=0.1)  # reap an already-exited child
            else:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except OSError:
            pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._stderr_sink is not None:
            try:
                self._stderr_sink.close()
            except OSError:
                pass
            self._stderr_sink = None

    def __enter__(self) -> "McpStdioTransport":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----------------------------------------------------------- internals
    def _sanitized_env(self) -> dict[str, str]:
        """Smallest possible environment; provider/cloud secrets are excluded."""
        env: dict[str, str] = {}
        for key in self._env_allowlist:
            value = os.environ.get(key)
            if value is None or _SECRET_KEY_RE.search(key):
                continue
            env[key] = value
        return env

    def _spawn(self) -> None:
        try:
            # stderr goes to a temp file (not a pipe) so a chatty server can
            # never block on an unread pipe while we wait on stdout.
            self._stderr_sink = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                list(self.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_sink,
                shell=False,
                env=self._sanitized_env(),
                bufsize=0,
            )
        except OSError as exc:
            if self._stderr_sink is not None:
                self._stderr_sink.close()
                self._stderr_sink = None
            raise ConnectorError(f"Cannot start MCP server: {_safe_error(exc)}") from exc
        self._buffer.clear()
        self._request_id = 0

    def _initialize(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mayaworkbench", "version": "0.1"},
            },
        )
        if not isinstance(result, dict):
            raise ConnectorError("MCP initialize returned a malformed result")
        self._notify("notifications/initialized", {})
        self._initialized = True

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise ConnectorError("MCP server is not running")
        try:
            raw = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, allow_nan=False
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ConnectorError("MCP request is not valid JSON") from exc
        if len(raw) > self.max_line_bytes:
            raise ConnectorError("MCP request exceeded the size limit")
        view = memoryview(raw)
        try:
            while view:
                written = proc.stdin.write(view)
                if not written:
                    raise ConnectorError("MCP server closed stdin")
                view = view[written:]
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ConnectorError("Cannot write to MCP server stdin") from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        if not self.is_running():
            raise ConnectorError("MCP server is not running")
        self._request_id += 1
        request_id = self._request_id
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + self.timeout_seconds
        unrelated = 0
        while True:
            message = self._read_message(deadline)
            if message.get("id") != request_id:
                # Notifications and stale responses are skipped; responses are
                # matched strictly by id, never by arrival order.
                unrelated += 1
                if unrelated > 1000:
                    raise ConnectorError("MCP server sent too many unrelated messages")
                continue
            if "error" in message:
                error = message["error"]
                text = error.get("message") if isinstance(error, dict) else str(error)
                raise ConnectorError(f"MCP {method} failed: {_safe_error(text)}")
            if "result" not in message:
                raise ConnectorError(f"MCP {method} response had no result")
            return message["result"]

    def _read_message(self, deadline: float) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise ConnectorError("MCP transport is not connected")
        fd = proc.stdout.fileno()
        while True:
            # A previously buffered frame may already be complete: consume it
            # before waiting for more bytes, or a skipped notification could
            # leave the real response stranded in the buffer.
            frame = self._pop_frame()
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectorError("MCP server did not respond within the timeout")
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise ConnectorError("MCP stdout pipe is unavailable") from exc
            if not ready:
                raise ConnectorError("MCP server did not respond within the timeout")
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                raise ConnectorError("Cannot read MCP server output") from exc
            if not chunk:
                raise ConnectorError("MCP server closed stdout before responding")
            self._buffer.extend(chunk)
            if len(self._buffer) > self.max_line_bytes * 4:
                raise ConnectorError("MCP response exceeded the buffer limit")

    def _pop_frame(self) -> dict[str, Any] | None:
        """Return one parsed frame from the buffer, or None when incomplete."""
        buffer = self._buffer
        if not buffer:
            return None
        head_end = buffer.find(b"\r\n\r\n")
        if head_end != -1 and buffer[:head_end].lower().startswith(b"content-length:"):
            length: int | None = None
            for line in buffer[:head_end].decode("ascii", "replace").split("\r\n"):
                key, _, value = line.partition(":")
                if key.strip().lower() == "content-length":
                    try:
                        length = int(value.strip())
                    except ValueError as exc:
                        raise ConnectorError("MCP Content-Length header is invalid") from exc
            if length is None or length <= 0 or length > self.max_line_bytes:
                raise ConnectorError("MCP Content-Length header is out of range")
            total = head_end + 4 + length
            if len(buffer) < total:
                return None
            frame = bytes(buffer[head_end + 4 : total])
            del buffer[:total]
            return self._decode_frame(frame)
        newline = buffer.find(b"\n")
        if newline == -1:
            if len(buffer) > self.max_line_bytes:
                raise ConnectorError("MCP response line exceeded the size limit")
            return None
        line = bytes(buffer[:newline]).strip()
        del buffer[: newline + 1]
        if not line:
            return None
        return self._decode_frame(line)

    @staticmethod
    def _decode_frame(raw: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("MCP server sent a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise ConnectorError("MCP server sent a non-object response")
        return parsed


# --------------------------------------------------------------------- adapter
class McpServerAdapter:
    """Validates remote tool contracts; never grants authority itself."""

    def __init__(
        self,
        *,
        name: str,
        allowed_capabilities: tuple[str, ...] = (),
        executor: Executor | None = None,
        transport: McpStdioTransport | None = None,
        tool_declarations: dict[str, McpToolDeclaration] | None = None,
    ):
        if executor is not None and transport is not None:
            raise PolicyError("MCP adapter accepts either an executor or a transport, not both")
        self.name = name
        self.allowed_capabilities = frozenset(allowed_capabilities)
        self.executor = executor
        self.transport = transport
        self._tool_declarations = dict(tool_declarations or {})
        self._tools: dict[str, McpToolContract] = {}

    def register_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        for raw in tools:
            name = str(raw.get("name") or "")
            if not name or "/" in name or len(name) > 120:
                raise PolicyError(f"invalid MCP tool name: {name!r}")
            capabilities = tuple(str(item) for item in raw.get("capabilities", []))
            unknown = sorted(set(capabilities) - self.allowed_capabilities)
            if unknown:
                raise PolicyError(
                    f"MCP tool {name!r} requests ungranted capabilities: {unknown}"
                )
            schema = raw.get("input_schema") or {}
            if not isinstance(schema, dict):
                raise PolicyError(f"MCP tool {name!r} has a non-object input schema")
            risk = str(raw.get("risk") or "UNKNOWN")
            tool_capability = str(
                raw.get("tool_capability")
                or RISK_TO_CAPABILITY.get(risk, HIGHEST_RISK_CAPABILITY)
            )
            contract = McpToolContract(
                name=name,
                version=str(raw.get("version") or "1.0"),
                description=str(raw.get("description") or ""),
                input_schema=schema,
                capabilities=capabilities,
                risk=risk,
                tool_capability=tool_capability,
            )
            self._tools[name] = contract
            accepted.append(contract.as_dict())
        return accepted

    def discover(self) -> list[dict[str, Any]]:
        """Fetch ``tools/list`` and map every tool to a local trust declaration.

        The remote server is untrusted: a risk or capability it claims is
        ignored. Only the operator's ``tool_declarations`` can lower a tool's
        tier; unknown tools become ``EXTERNAL_WRITE`` + the broadest capability
        and are never granted by default.
        """
        if self.transport is None:
            raise PolicyError("no MCP transport is configured; discovery is disabled")
        remote_tools = self.transport.list_tools()
        prepared: list[dict[str, Any]] = []
        for tool in remote_tools:
            name = str(tool.get("name") or "")
            if not name or "/" in name or len(name) > 120:
                raise PolicyError(f"invalid MCP tool name: {name!r}")
            declared = self._tool_declarations.get(name)
            risk = declared.risk if declared is not None else DEFAULT_RISK
            capability = (
                declared.tool_capability
                if declared is not None and declared.tool_capability
                else RISK_TO_CAPABILITY.get(risk, HIGHEST_RISK_CAPABILITY)
            )
            capabilities = tuple(declared.capabilities) if declared is not None else ()
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            prepared.append(
                {
                    "name": name,
                    "version": str(tool.get("version") or "1.0"),
                    "description": str(tool.get("description") or ""),
                    "input_schema": schema,
                    "capabilities": list(capabilities),
                    "risk": risk,
                    "tool_capability": capability,
                }
            )
        return self.register_tools(prepared)

    def invoke(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        contract = self._tools.get(tool)
        if contract is None:
            raise PolicyError(f"MCP tool {tool!r} is not registered by this adapter")
        if not set(contract.capabilities) <= self.allowed_capabilities:
            raise PolicyError(f"MCP tool {tool!r} exceeds granted capabilities")
        properties = contract.input_schema.get("properties", {})
        extra = sorted(set(arguments) - set(properties))
        if extra:
            raise PolicyError(f"MCP tool {tool!r} got undeclared arguments: {extra}")
        if self.executor is not None:
            result = self.executor(tool, dict(arguments))
        elif self.transport is not None:
            result = self.transport.call_tool(tool, dict(arguments))
        else:
            raise PolicyError("no MCP executor or transport is configured; calls are disabled")
        payload = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str).encode(
            "utf-8"
        )
        return {
            "server": self.name,
            "tool": tool,
            "contract_sha256": contract.as_dict()["contract_sha256"],
            "result_sha256": hashlib.sha256(payload).hexdigest(),
            "result": result,
            "policy_decision": "ALLOWED_BY_LOCAL_GATEWAY",
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [contract.as_dict() for contract in self._tools.values()]

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()


# --------------------------------------------------------------- ToolConnector
class McpConnector:
    """Registry-facing view of an MCP adapter. The registry remains the authority."""

    mode = "TRANSPORT_ONLY_REGISTRY_ENFORCED"

    def __init__(
        self, adapter: McpServerAdapter, *, read_only_allowlist: Any = ()
    ):
        self._adapter = adapter
        self.name = adapter.name or "mcp"
        self._revoked = False
        contracts = {item["name"]: item for item in adapter.list_tools()}
        self._risks = {name: item["risk"] for name, item in contracts.items()}
        self._capabilities = {
            name: item["tool_capability"] for name, item in contracts.items()
        }
        requested = {str(name) for name in read_only_allowlist}
        self._allowlist = frozenset(
            name for name in requested if self._risks.get(name) == RISK_READ_ONLY
        )

    # --------------------------------------------------------------- interface
    def operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._risks))

    def supports(self, operation: str) -> bool:
        return not self._revoked and operation in self._risks

    def is_revoked(self) -> bool:
        return self._revoked

    def allowlisted_operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowlist))

    def revoke(self) -> None:
        """Immediately disable every future call and stop the child server."""
        self._revoked = True
        self._adapter.close()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "operations": self.operations(),
            "risk_by_operation": dict(sorted(self._risks.items())),
            "tool_capability_by_operation": dict(sorted(self._capabilities.items())),
            "allowlisted_read_operations": self.allowlisted_operations(),
            "requires_grant": sorted(set(self._risks) - set(self._allowlist)),
            "revoked": self._revoked,
            "note": (
                "MCP is transport only. Discovery is untrusted data; writes require "
                "a one-time, spec-hash-bound registry grant. No tool output or "
                "argument is logged."
            ),
        }

    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        """Stable, non-secret approval target (params are bound in the spec hash)."""
        return f"mcp:{self.name}/{operation}"

    # ----------------------------------------------------------------- invoking
    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError(
                "MCP connector has been revoked; re-create it before use",
                hint="A revoked MCP transport can no longer call any tool.",
            )
        if operation not in self._risks:
            raise ConnectorError(
                f"MCP connector does not support operation {operation!r}",
                hint=f"Allowed: {', '.join(self.operations()) or '(none)'}",
            )
        risk = self._risks[operation]
        public = dict(params)
        grant_raw = public.pop(APPROVAL_PARAM, None)
        if risk != RISK_READ_ONLY or operation not in self._allowlist:
            self._verify_grant(operation, public, grant_raw)
        try:
            adapter_result = self._adapter.invoke(operation, public)
        except PolicyError as exc:
            raise ConnectorError(str(exc)) from exc
        data = {
            "result": adapter_result["result"],
            "risk": risk,
            "tool_capability": self._capabilities.get(operation),
            "contract_sha256": adapter_result["contract_sha256"],
            "result_sha256": adapter_result["result_sha256"],
            "policy_decision": adapter_result["policy_decision"],
        }
        return ConnectorResult(
            connector=self.name,
            operation=operation,
            target=self.describe_target(operation, public),
            data=data,
            request_hash=spec_sha256(
                {"connector": self.name, "operation": operation, "params": public}
            ),
            response_hash=adapter_result["result_sha256"],
            retrieved_at=to_iso(utcnow()),
            read_only=risk == RISK_READ_ONLY,
        )

    def _verify_grant(
        self, operation: str, public: dict[str, Any], grant_raw: Any
    ) -> None:
        if not isinstance(grant_raw, dict):
            raise ConnectorError(
                f"MCP tool {operation!r} requires a one-time approval grant",
                hint="Approve the exact action, then call invoke_with_consent().",
            )
        if (
            grant_raw.get("connector") != self.name
            or grant_raw.get("operation") != operation
        ):
            raise ConnectorError("Approval grant does not match this connector and operation")
        expected = spec_sha256(
            {
                "connector": self.name,
                "operation": operation,
                "target": self.describe_target(operation, public),
                "params": public,
            }
        )
        if grant_raw.get("spec_sha256") != expected:
            raise ConnectorError("Approval grant does not match this exact MCP call")
        if not grant_raw.get("approval_id") or not grant_raw.get("actor"):
            raise ConnectorError("Approval grant is missing its identity fields")


__all__ = [
    "RISK_READ_ONLY",
    "RISK_LOCAL_MUTATION",
    "RISK_EXTERNAL_WRITE",
    "VALID_RISKS",
    "RISK_TO_CAPABILITY",
    "HIGHEST_RISK_CAPABILITY",
    "McpToolDeclaration",
    "McpToolContract",
    "McpStdioTransport",
    "McpServerAdapter",
    "McpConnector",
]

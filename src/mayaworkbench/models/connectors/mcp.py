"""MCP interoperability adapter (audit §8.1).

MCP is a tool-description protocol, **not** a permission model. Every remote
tool list is untrusted data; the local gateway decides scope, consent, approval
and audit. This adapter validates tool contracts and routes calls through an
injected executor with an explicit capability allowlist. No network transport
is enabled by default.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from mayaworkbench.errors import PolicyError

Executor = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class McpToolContract:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    capabilities: tuple[str, ...] = ()
    risk: str = "UNKNOWN"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description[:500],
            "capabilities": list(self.capabilities),
            "risk": self.risk,
            "contract_sha256": hashlib.sha256(
                json.dumps(self.input_schema, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }


class McpServerAdapter:
    """Validates remote tool contracts; never grants authority itself."""

    def __init__(
        self,
        *,
        name: str,
        allowed_capabilities: tuple[str, ...] = (),
        executor: Executor | None = None,
    ):
        self.name = name
        self.allowed_capabilities = frozenset(allowed_capabilities)
        self.executor = executor
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
            contract = McpToolContract(
                name=name,
                version=str(raw.get("version") or "1.0"),
                description=str(raw.get("description") or ""),
                input_schema=schema,
                capabilities=capabilities,
                risk=str(raw.get("risk") or "UNKNOWN"),
            )
            self._tools[name] = contract
            accepted.append(contract.as_dict())
        return accepted

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
        if self.executor is None:
            raise PolicyError("no MCP executor is configured; calls are disabled")
        result = self.executor(tool, dict(arguments))
        payload = json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")
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


__all__ = ["McpToolContract", "McpServerAdapter"]

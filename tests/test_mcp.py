"""MCP is an interoperability protocol, never a permission boundary."""

from __future__ import annotations

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.models.connectors.mcp import McpServerAdapter


def _adapter(calls):
    return McpServerAdapter(
        name="docs",
        allowed_capabilities=("read",),
        executor=lambda tool, args: (calls.append((tool, args)), {"ok": True})[1],
    )


def test_remote_tool_contracts_are_validated():
    calls: list[tuple[str, dict]] = []
    adapter = _adapter(calls)
    accepted = adapter.register_tools(
        [
            {
                "name": "search_docs",
                "version": "1.0",
                "description": "Search documentation",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                "capabilities": ["read"],
            }
        ]
    )
    assert accepted[0]["capabilities"] == ["read"]
    result = adapter.invoke("search_docs", {"query": "auth"})
    assert result["policy_decision"] == "ALLOWED_BY_LOCAL_GATEWAY"
    assert len(result["result_sha256"]) == 64


def test_ungranted_capability_is_refused():
    calls: list[tuple[str, dict]] = []
    adapter = _adapter(calls)
    with pytest.raises(PolicyError, match="ungranted capabilities"):
        adapter.register_tools(
            [
                {
                    "name": "delete",
                    "input_schema": {},
                    "capabilities": ["write"],
                }
            ]
        )
    assert calls == []


def test_undeclared_arguments_are_refused():
    calls: list[tuple[str, dict]] = []
    adapter = _adapter(calls)
    adapter.register_tools(
        [{"name": "read", "input_schema": {"properties": {"path": {}}}, "capabilities": ["read"]}]
    )
    with pytest.raises(PolicyError, match="undeclared arguments"):
        adapter.invoke("read", {"path": "x", "secret": "y"})
    with pytest.raises(PolicyError, match="not registered"):
        adapter.invoke("unknown", {})

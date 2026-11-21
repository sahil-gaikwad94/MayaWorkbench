"""Advanced tools end-to-end: typed, bounded, and fail-closed without explicit grants."""

from __future__ import annotations

import json
import sqlite3

from mayaworkbench.domain.enums import ToolCallStatus
from mayaworkbench.skills.service import SkillsService

from .test_tool_gateway_runner import build_gateway


def test_api_request_is_host_allowlisted(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    result = gateway.invoke(
        "api_request", {"url": "https://evil.example.com/x", "method": "GET"}
    )
    assert result.status == ToolCallStatus.BLOCKED
    assert "allowlist" in result.summary


def test_api_request_works_with_injected_http_and_provenance(services, indexed, tmp_path):
    calls = []

    def http(method, url, headers, body):
        calls.append((method, url, headers, body))
        return {"status": 200, "body": b'{"ok": true}'}

    gateway, *_ = build_gateway(services, indexed, tmp_path)
    gateway.allowed_api_hosts = ("api.example.com",)
    gateway.http = http

    # Avoid real DNS by patching getaddrinfo to a public address.
    import socket

    original = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    try:
        result = gateway.invoke(
            "api_request", {"url": "https://api.example.com/v1/items", "method": "GET"}
        )
    finally:
        socket.getaddrinfo = original
    assert result.status == ToolCallStatus.SUCCEEDED
    assert result.data["status_code"] == 200
    assert result.data["data"] == {"ok": True}
    assert len(result.data["request_sha256"]) == 64
    assert len(result.data["response_sha256"]) == 64
    assert calls[0][0] == "GET"
    assert "Authorization" not in calls[0][2]


def test_api_request_blocks_private_address_and_missing_executor(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    gateway.allowed_api_hosts = ("api.example.com",)
    # No executor configured: must block before any DNS/network work.
    result = gateway.invoke("api_request", {"url": "https://api.example.com/x"})
    assert result.status == ToolCallStatus.BLOCKED
    assert "no configured HTTP executor" in result.summary

    gateway.http = lambda *a, **k: {"status": 200, "body": b"{}"}
    import socket

    original = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    try:
        blocked = gateway.invoke("api_request", {"url": "https://api.example.com/x"})
    finally:
        socket.getaddrinfo = original
    assert blocked.status == ToolCallStatus.BLOCKED
    assert "non-public" in blocked.summary


def test_db_query_is_read_only_and_directory_gated(services, indexed, tmp_path):
    db_path = tmp_path / "data.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    db.execute("INSERT INTO items VALUES (1, 'one')")
    db.commit()
    db.close()

    gateway, *_ = build_gateway(services, indexed, tmp_path)
    gateway.allowed_db_dirs = (str(tmp_path),)
    ok = gateway.invoke(
        "db_query", {"path": str(db_path), "sql": "SELECT name FROM items", "max_rows": 5}
    )
    assert ok.status == ToolCallStatus.SUCCEEDED
    assert "one" in ok.output
    bad = gateway.invoke(
        "db_query", {"path": str(db_path), "sql": "DELETE FROM items", "max_rows": 5}
    )
    assert bad.status == ToolCallStatus.BLOCKED


def test_env_detect_and_notebook_inspect(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    (workspace / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (workspace / "demo.ipynb").write_text(
        json.dumps(
            {
                "cells": [{"cell_type": "code", "outputs": [], "source": ["x=1"]}],
                "metadata": {"kernelspec": {"name": "python3"}},
            }
        ),
        encoding="utf-8",
    )
    detected = gateway.invoke("env_detect", {"path": "."})
    assert detected.status == ToolCallStatus.SUCCEEDED
    assert any(item["ecosystem"] == "node" for item in detected.data["detected"])
    notebook = gateway.invoke("notebook_inspect", {"path": "demo.ipynb"})
    assert notebook.status == ToolCallStatus.SUCCEEDED
    assert notebook.data["cells"] == 1


def test_logs_read_redacts_secrets(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("token sk-abcdefghijklmnopqrstuvwxyz0123456789\n")
    gateway.allowed_log_dirs = (str(log_dir),)
    result = gateway.invoke("logs_read", {"path": str(log_dir / "app.log"), "lines": 10})
    assert result.status == ToolCallStatus.SUCCEEDED
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in result.output
    assert "[REDACTED]" in result.output


def test_skill_tools_use_the_skills_service(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    SkillsService(services.db).upsert(
        gateway.run.project_id, "tool-skill", "Tool skill", body="1. Read only."
    )
    listing = gateway.invoke("skill_list", {})
    assert listing.status == ToolCallStatus.SUCCEEDED
    skill = next(item for item in listing.data["skills"] if item["key"] == "tool-skill")
    run = gateway.invoke("skill_run", {"skill_id": skill["id"], "context_json": '{"goal":"x"}'})
    assert run.status == ToolCallStatus.SUCCEEDED
    assert "Read only" in run.output


def test_capability_gate_blocks_advanced_tools(services, indexed, tmp_path):
    gateway, *_ = build_gateway(services, indexed, tmp_path)
    gateway.contract.constraints.capabilities = ["file.read"]
    result = gateway.invoke("env_detect", {"path": "."})
    assert result.status == ToolCallStatus.BLOCKED
    assert "tool.advanced" in result.summary

"""WP6: notebook, scholarly, service and schema connectors keep their boundary."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError
from mayaworkbench.models.connectors.extended import (
    ContainerServicesConnector,
    DatabaseSchemaConnector,
    JupyterConnector,
    ScholarlyConnector,
)


class _Runtime:
    def __init__(self):
        self.scripts = []

    def exec_script(self, script, *, timeout_seconds):
        self.scripts.append(script)
        return SimpleNamespace(
            stdout='MAYA_CELL={"ok": true, "value": "42"}\n', stderr="",
            exit_code=0, duration_ms=1,
        )


def test_jupyter_connector_runs_cells_in_the_injected_runtime():
    runtime = _Runtime()
    connector = JupyterConnector(runtime=runtime)
    result = connector.invoke("execute", {
        "notebook": {"cells": [{"source": "6 * 7"}]}, "runtime_id": "run-1",
    })
    assert result.data["cells"][0]["result"]["value"] == "42"
    assert runtime.scripts and "6 * 7" in runtime.scripts[0]
    assert result.read_only is False
    with pytest.raises(ConnectorError):
        connector.invoke("execute", {"notebook": {"cells": []}})
    connector.revoke()
    with pytest.raises(ConnectorError):
        connector.invoke("execute", {"notebook": {"cells": [{"source": "1"}]}})


def test_scholarly_connector_enforces_host_allowlist_and_redaction():
    class Response:
        def __init__(self, body):
            self._body = body

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return self._body[:n]

    connector = ScholarlyConnector(opener=lambda url, timeout: Response(b'{"title":"ok"}'))
    result = connector.invoke("search", {"url": "https://api.crossref.org/works?query=x"})
    assert result.data["provenance"] == "IMPORTED/EXTERNAL"
    for bad in ("http://api.crossref.org/x", "https://evil.example/x", "https://api.crossref.org.evil/x"):
        with pytest.raises(PolicyError):
            connector.invoke("search", {"url": bad})


def test_container_services_require_grant_for_writes_and_loopback():
    class Client:
        def __init__(self): self.calls = []
        def status(self, name): self.calls.append(("status", name)); return "stopped"
        def start(self, name, port, image): self.calls.append(("start", name, port)); return "running"
        def stop(self, name): self.calls.append(("stop", name)); return "stopped"

    client = Client()
    connector = ContainerServicesConnector(client=client)
    plan = connector.invoke("plan", {"service": "api", "port": 8000, "image": "img"})
    assert plan.data["plan_hash"]
    assert connector.invoke("status", {"service": "api"}).data["status"] == "stopped"
    with pytest.raises(ConnectorError, match="approval"):
        connector.invoke("start", {"service": "api", "port": 8000, "image": "img"})
    with pytest.raises(PolicyError, match="loopback"):
        connector.invoke("plan", {"service": "api", "port": 8000, "host": "0.0.0.0"})
    connector.invoke("start", {"service": "api", "port": 8000, "image": "img"}, grant=object())
    assert ("start", "api", 8000) in client.calls


def test_schema_connector_is_read_only_and_migration_is_a_plan(tmp_path):
    db = tmp_path / "app.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE INDEX idx_users_name ON users(name)")
    conn.commit()
    conn.close()

    connector = DatabaseSchemaConnector(allowed_dirs=(str(tmp_path),))
    result = connector.invoke("schema", {"path": str(db)})
    assert "users" in result.data["tables"]
    assert result.data["columns"]["users"][1]["name"] == "name"
    assert len(result.data["schema_hash"]) == 64
    plan = connector.invoke("migration_plan", {"path": str(db)})
    assert plan.data["applied"] is False
    assert "disposable" in " ".join(plan.data["plan"])
    outside = tmp_path.parent / "outside.sqlite"
    outside.write_bytes(b"x")
    with pytest.raises(PolicyError):
        connector.invoke("schema", {"path": str(outside)})


def test_advanced_registry_wires_optional_connectors(tmp_path):
    from mayaworkbench.models.connectors.registry import build_advanced_connector_registry

    registry = build_advanced_connector_registry(
        notebook_runtime=_Runtime(),
        container_client=SimpleNamespace(status=lambda name: "stopped"),
        schema_dirs=(str(tmp_path),),
        http=object(),
    )
    names = set(registry.names())
    assert {"jupyter", "scholarly", "container-services", "db-schema"} <= names

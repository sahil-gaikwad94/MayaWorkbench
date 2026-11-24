"""Additional WP6 connectors: notebooks, scholarly APIs, services, DB schema.

All connectors follow the same boundary as the existing ones: read-only by
default, side effects require an explicit hash-bound grant, credentials come
from the environment, and external responses are untrusted data that can only
downgrade a claim. Nothing here launches a host shell.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable
from urllib.parse import urlsplit

from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError
from mayaworkbench.models.connectors.contracts import (
    CONNECTOR_READ_SCOPE,
    ConnectorContract,
    PathGuard,
    make_result,
    reject_inline_secrets,
)

MAX_NOTEBOOK_CELLS = 64
MAX_CELL_CHARS = 20_000
MAX_RESPONSE_BYTES = 2_000_000
SCHOLARLY_HOSTS = frozenset({"api.crossref.org", "api.openalex.org", "export.arxiv.org"})


JUPYTER_CONTRACT = ConnectorContract(
    id="jupyter", version="1.0",
    permissions=["notebook:execute"], risk="HIGH",
    side_effects=["execute_cells_in_task_runtime"],
    evidence_outputs=["cell_index", "outputs", "execution_count", "runtime_id"],
    operation_scopes={"execute": "notebook:execute"},
)
SCHOLARLY_CONTRACT = ConnectorContract(
    id="scholarly", version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "research:read"], risk="LOW",
    side_effects=[], evidence_outputs=["metadata", "source_url", "retrieved_at"],
    operation_scopes={"search": CONNECTOR_READ_SCOPE},
)
SERVICES_CONTRACT = ConnectorContract(
    id="container-services", version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "services:lifecycle"], risk="MEDIUM",
    side_effects=["start_service", "stop_service"],
    evidence_outputs=["service", "status", "port", "plan_hash"],
    operation_scopes={"status": CONNECTOR_READ_SCOPE, "plan": CONNECTOR_READ_SCOPE,
                      "start": "services:lifecycle", "stop": "services:lifecycle"},
)
SCHEMA_CONTRACT = ConnectorContract(
    id="db-schema", version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "db:read"], risk="LOW",
    side_effects=[], evidence_outputs=["tables", "columns", "indexes", "schema_hash"],
    operation_scopes={"schema": CONNECTOR_READ_SCOPE, "migration_plan": CONNECTOR_READ_SCOPE},
)


class JupyterConnector:
    """Execute notebook cells through an injected task runtime, never a host shell."""

    name = "jupyter"
    _OPERATIONS = ("execute",)

    def __init__(self, *, runtime: Any):
        self.runtime = runtime
        self._revoked = False

    def operations(self): return self._OPERATIONS
    def supports(self, op): return op in self._OPERATIONS
    def is_revoked(self): return self._revoked
    def revoke(self): self._revoked = True

    def describe(self):
        return {"name": self.name, "operations": list(self._OPERATIONS),
                "boundary": "injected task runtime; no host shell", "revoked": self._revoked}

    def invoke(self, operation, params=None, *, grant=None):
        if self._revoked:
            raise ConnectorError("jupyter connector is revoked")
        if operation not in self._OPERATIONS:
            raise ConnectorError(f"unsupported operation: {operation}")
        params = dict(params or {})
        reject_inline_secrets(params)
        notebook = params.get("notebook") or {}
        cells = notebook.get("cells") if isinstance(notebook, dict) else None
        if not isinstance(cells, list) or not cells:
            raise ConnectorError("a notebook with a non-empty cells list is required")
        if len(cells) > MAX_NOTEBOOK_CELLS:
            raise ConnectorError(f"notebook exceeds {MAX_NOTEBOOK_CELLS} cells")
        outputs = []
        for index, cell in enumerate(cells[:MAX_NOTEBOOK_CELLS]):
            source = cell.get("source") if isinstance(cell, dict) else str(cell)
            if not isinstance(source, str) or len(source) > MAX_CELL_CHARS:
                raise ConnectorError(f"cell {index} has invalid or oversized source")
            script = (
                "import json\n"
                "try:\n"
                "    result = eval(compile(" + repr(source) + ", '<cell>', 'eval'))\n"
                "    print('MAYA_CELL=' + json.dumps({'ok': True, 'value': str(result)[:2000]}))\n"
                "except Exception as exc:\n"
                "    print('MAYA_CELL=' + json.dumps({'ok': False, 'error': type(exc).__name__}))\n"
            )
            result = self.runtime.exec_script(
                script, timeout_seconds=int(params.get("timeout_seconds", 120))
            )
            marker = None
            for line in (result.stdout or "").splitlines():
                if line.startswith("MAYA_CELL="):
                    try:
                        marker = json.loads(line.split("=", 1)[1])
                    except json.JSONDecodeError:
                        pass
            outputs.append({"cell_index": index, "exit_code": result.exit_code,
                            "result": marker or {"ok": False, "error": "no verdict marker"},
                            "stdout_excerpt": (result.stdout or "")[:2000]})
        return make_result(
            connector="jupyter", operation=operation,
            target="notebook", request_payload={"cells": len(cells)},
            data={"cells": outputs, "runtime_id": str(params.get("runtime_id", ""))},
            read_only=False,
        )


class ScholarlyConnector:
    """Read-only metadata search against a strict host allowlist."""

    name = "scholarly"
    _OPERATIONS = ("search",)

    def __init__(self, *, opener: Callable[..., Any] | None = None):
        self._opener = opener
        self._revoked = False

    def operations(self): return self._OPERATIONS
    def supports(self, op): return op in self._OPERATIONS
    def is_revoked(self): return self._revoked
    def revoke(self): self._revoked = True
    def describe(self):
        return {"name": self.name, "operations": list(self._OPERATIONS),
                "allowed_hosts": sorted(SCHOLARLY_HOSTS), "read_only": True}

    def invoke(self, operation, params=None, *, grant=None):
        if self._revoked or operation != "search":
            raise ConnectorError("scholarly connector unavailable for this operation")
        params = dict(params or {})
        reject_inline_secrets(params)
        url = str(params.get("url", ""))
        parsed = urlsplit(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in SCHOLARLY_HOSTS:
            raise PolicyError("scholarly URL must use https and an allowlisted host")
        if self._opener is None:
            raise ConnectorError("no HTTP opener configured for the scholarly connector")
        with self._opener(url, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConnectorError("scholarly response exceeded the byte budget")
        payload = json.loads(raw.decode("utf-8"))
        return make_result(
            connector=self.name, operation=operation, target=parsed.hostname or "",
            request_payload={"url": url},
            data={"metadata": payload, "source_url": url, "provenance": "IMPORTED/EXTERNAL",
                  "note": "external metadata is untrusted data, not a verified claim"},
            raw_response=raw, read_only=True,
        )


class ContainerServicesConnector:
    """Plan and control task-owned services through an injected OCI client."""

    name = "container-services"
    _OPERATIONS = ("status", "plan", "start", "stop")

    def __init__(self, *, client: Any):
        self.client = client
        self._revoked = False

    def operations(self): return self._OPERATIONS
    def supports(self, op): return op in self._OPERATIONS
    def is_revoked(self): return self._revoked
    def revoke(self): self._revoked = True
    def describe(self):
        return {"name": self.name, "operations": list(self._OPERATIONS),
                "write_operations": ["start", "stop"], "loopback_only": True}

    def invoke(self, operation, params=None, *, grant=None):
        if self._revoked or operation not in self._OPERATIONS:
            raise ConnectorError("container-services connector unavailable for this operation")
        params = dict(params or {})
        reject_inline_secrets(params)
        service = str(params.get("service", ""))
        if not service or len(service) > 60:
            raise ConnectorError("a service name of 1-60 characters is required")
        port = int(params.get("port", 0))
        if params.get("host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
            raise PolicyError("container services may bind loopback only")
        plan = {"service": service, "port": port, "image": str(params.get("image", "")),
                "network": "loopback", "restart": "no"}
        plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        if operation == "status":
            status = self.client.status(service)
            return make_result(connector=self.name, operation=operation, target=service,
                               request_payload={"service": service},
                               data={"service": service, "status": status}, read_only=True)
        if not 1 <= port <= 65535:
            raise ConnectorError("a service port between 1 and 65535 is required")
        if operation == "plan":
            return make_result(connector=self.name, operation=operation, target=service,
                               request_payload=plan,
                               data={"plan": plan, "plan_hash": plan_hash}, read_only=True)
        if not grant:
            raise ConnectorError(f"{operation} requires an explicit hash-bound approval grant")
        if operation == "start":
            state = self.client.start(service, port=port, image=plan["image"])
        else:
            state = self.client.stop(service)
        return make_result(connector=self.name, operation=operation, target=service,
                           request_payload=plan,
                           data={"service": service, "status": state, "plan_hash": plan_hash},
                           read_only=False)


class DatabaseSchemaConnector:
    """Read-only schema inspection with a migration dry-run plan."""

    name = "db-schema"
    _OPERATIONS = ("schema", "migration_plan")

    def __init__(self, *, allowed_dirs: tuple[str, ...]):
        self.guard = PathGuard(allowed_dirs=allowed_dirs)
        self._revoked = False

    def operations(self): return self._OPERATIONS
    def supports(self, op): return op in self._OPERATIONS
    def is_revoked(self): return self._revoked
    def revoke(self): self._revoked = True
    def describe(self):
        return {"name": self.name, "operations": list(self._OPERATIONS), "read_only": True}

    def invoke(self, operation, params=None, *, grant=None):
        if self._revoked or operation not in self._OPERATIONS:
            raise ConnectorError("db-schema connector unavailable for this operation")
        params = dict(params or {})
        reject_inline_secrets(params)
        target = self.guard.check(str(params.get("path", "")))
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            columns = {}
            indexes = {}
            for table in tables:
                columns[table] = [
                    {"name": row[1], "type": row[2], "notnull": bool(row[3])}
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                indexes[table] = [row[1] for row in connection.execute(
                    f"PRAGMA index_list({table})")]
            digest = hashlib.sha256(json.dumps(
                {"tables": tables, "columns": columns, "indexes": indexes},
                sort_keys=True).encode()).hexdigest()
        finally:
            connection.close()
        if operation == "migration_plan":
            return make_result(connector=self.name, operation=operation, target=target,
                    request_payload={"path": target}, data={
                "plan": [
                    "copy the database to a disposable path",
                    "apply migrations inside a task-owned runtime",
                    "compare the resulting schema hash with the live schema",
                    "discard the copy; never migrate the connected database",
                ],
                "schema_hash": digest, "applied": False,
            }, read_only=True)
        return make_result(connector=self.name, operation=operation, target=target,
                    request_payload={"path": target}, data={
            "tables": tables, "columns": columns, "indexes": indexes,
            "schema_hash": digest,
        }, read_only=True)


__all__ = [
    "JUPYTER_CONTRACT", "SCHOLARLY_CONTRACT", "SERVICES_CONTRACT", "SCHEMA_CONTRACT",
    "JupyterConnector", "ScholarlyConnector", "ContainerServicesConnector",
    "DatabaseSchemaConnector",
]

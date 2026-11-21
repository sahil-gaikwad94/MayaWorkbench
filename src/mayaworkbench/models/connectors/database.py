"""Read-only SQLite query connector (WP12).

Only a single allowlisted ``SELECT`` statement may run, against a file whose
canonical path is contained in an explicit directory allowlist. Writes, DDL,
``ATTACH``/``DETACH``, pragmas, multi-statement payloads and comments are
refused by a static check *before* any SQL reaches SQLite, and the default
connection is additionally opened with ``mode=ro`` plus a deny-write authorizer.

The connection factory is injectable so tests run entirely in memory: no
external database, network or real credential is involved.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult
from mayaworkbench.models.connectors.contracts import (
    CONNECTOR_READ_SCOPE,
    ConnectorContract,
    PathGuard,
    make_result,
    reject_inline_secrets,
)

CONTRACT = ConnectorContract(
    id="database",
    version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "db:read"],
    risk="MEDIUM",
    side_effects=[],
    evidence_outputs=["columns", "rows", "row_count", "truncated", "request_hash", "response_hash"],
    operation_scopes={"query": CONNECTOR_READ_SCOPE},
)

_FORBIDDEN_WORDS: tuple[str, ...] = (
    "ATTACH", "DETACH", "PRAGMA", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE",
    "ALTER", "REPLACE", "VACUUM", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
    "REINDEX", "ANALYZE", "TRIGGER", "GRANT", "REVOKE", "LOAD_EXTENSION",
    "EXECUTE", "EXEC", "CALL", "DO", "INTO",
)

_DENY_AUTHORIZER_ACTIONS: tuple[int, ...] = tuple(
    value
    for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_DROP_TABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_ALTER_TABLE",
        "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_REPLACE", "SQLITE_TRANSACTION",
    )
    if (value := getattr(sqlite3, name, None)) is not None
)


def validate_select(sql: Any) -> str:
    """Return a single safe SELECT statement or refuse it with ConnectorError."""
    text = str(sql or "").strip()
    if not text:
        raise ConnectorError("A SQL query is required")
    if "--" in text or "/*" in text or "*/" in text:
        raise ConnectorError("SQL comments are not permitted in this connector")
    if text.count(";") > 1 or (text.count(";") == 1 and not text.rstrip().endswith(";")):
        raise ConnectorError("Only one SQL statement is permitted")
    statement = text.rstrip(";").strip()
    match = re.match(r"^([A-Za-z_]+)", statement)
    if match is None or match.group(1).upper() != "SELECT":
        raise ConnectorError("Only allowlisted SELECT statements are permitted")
    for word in _FORBIDDEN_WORDS:
        if re.search(rf"\b{word}\b", statement, re.IGNORECASE):
            raise ConnectorError(f"SQL keyword {word!r} is not permitted by the read-only connector")
    return statement


class DatabaseConnector:
    """Allowlisted, read-only SQL against a path-contained local SQLite file."""

    name = "database"
    mode = "READ_ONLY"
    contract = CONTRACT

    def __init__(
        self,
        *,
        allowed_dirs: Any = None,
        connect: Callable[[str], Any] | None = None,
        resolver: Callable[[str], str] | None = None,
        exists: Callable[[str], bool] | None = None,
        is_file: Callable[[str], bool] | None = None,
        token_env: str = "MAYA_DATABASE_TOKEN",
        max_rows: int = 100,
    ):
        self._guard = PathGuard(allowed_dirs, resolver=resolver, exists=exists, is_file=is_file)
        self._connect_factory = connect
        self.token_env = token_env
        self.max_rows = int(max_rows)
        if self.max_rows <= 0:
            raise PolicyError("max_rows must be positive")
        self._state: dict[str, Any] = {}
        self._revoked = False

    def operations(self) -> tuple[str, ...]:
        return ("query",)

    def supports(self, operation: str) -> bool:
        return operation in self.operations()

    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        resolved = self._guard.check(params.get("path"), label="database path")
        return "sqlite:" + self._guard.display(resolved)

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
            "max_rows": self.max_rows,
            "never_merges": True,
            "contract": self.contract.model_dump(mode="json"),
            "revoked": self._revoked,
            "note": (
                "Single SELECT only; writes, ATTACH, pragmas and multi-statements are "
                "refused. Local file access runs at host-user privilege (LOCAL_TRUSTED)."
            ),
        }

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError("Database connector has been revoked; re-create it before use")
        if not self.supports(operation):
            raise ConnectorError(
                f"Database connector does not support {operation!r}",
                hint=f"Allowed operations: {', '.join(self.operations())}",
            )
        if operation == "query":
            result = self._query(params)
        else:  # pragma: no cover - supports() guards this
            raise ConnectorError(f"Unsupported database operation {operation!r}")
        self._state[operation] = result
        return result

    def _query(self, params: dict[str, Any]) -> ConnectorResult:
        resolved = self._guard.check(params.get("path"), label="database path")
        display = self._guard.display(resolved)
        statement = validate_select(params.get("sql"))
        limit = self._row_limit(params.get("max_rows"))
        connection = self._open(resolved)
        try:
            cursor = connection.execute(statement)
            fetched = cursor.fetchmany(limit + 1)
            truncated = len(fetched) > limit
            rows = [tuple(row) for row in fetched[:limit]]
            columns = [str(column[0]) for column in (getattr(cursor, "description", None) or [])]
        finally:
            if self._connect_factory is None:
                try:
                    connection.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
        data = {
            "path": display,
            "sql": statement,
            "columns": columns,
            "rows": [list(row) for row in rows],
            "row_count": len(rows),
            "max_rows": limit,
            "truncated": truncated,
        }
        return make_result(
            connector=self.name,
            operation="query",
            target="sqlite:" + display,
            data=data,
            request_payload={"path": display, "sql": statement, "max_rows": limit},
        )

    def _row_limit(self, value: Any) -> int:
        if value in (None, ""):
            return self.max_rows
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("max_rows must be a positive integer") from exc
        if limit <= 0:
            raise ConnectorError("max_rows must be a positive integer")
        return min(limit, self.max_rows)

    def _open(self, resolved: str) -> Any:
        if self._connect_factory is not None:
            return self._connect_factory(resolved)
        uri = Path(resolved).as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise ConnectorError(f"Cannot open the allowlisted SQLite file: {exc}") from exc
        try:
            connection.set_authorizer(self._authorizer)
        except (AttributeError, sqlite3.Error):  # pragma: no cover - old SQLite
            pass
        return connection

    @staticmethod
    def _authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, source: Any) -> int:
        if action in _DENY_AUTHORIZER_ACTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


__all__ = ["CONTRACT", "validate_select", "DatabaseConnector"]

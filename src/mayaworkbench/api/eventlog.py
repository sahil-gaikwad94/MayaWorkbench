"""Read-only, cursor-aware projection of the durable ``workflow_events`` log.

``EventStore.list`` already filters by ``after_seq`` but intentionally does not
expose ``seq`` as a model field.  Streaming and reconnect need that cursor, so
this module performs one narrow, parameterised SELECT over the existing table
and maps rows back to :class:`WorkflowEvent` before serialising them.  It never
writes: the append path stays in ``storage.stores.EventStore``.
"""

from __future__ import annotations

import json
from typing import Any

from mayaworkbench.app.container import Services
from mayaworkbench.domain.models import WorkflowEvent

_COLUMNS = (
    "seq",
    "event_id",
    "occurred_at",
    "event_type",
    "project_id",
    "repository_id",
    "snapshot_id",
    "task_id",
    "run_id",
    "entity_type",
    "entity_id",
    "actor_kind",
    "actor_name",
    "correlation_id",
    "schema_version",
    "payload_json",
)


def _to_item(row: Any) -> dict[str, Any]:
    data = {column: row[column] for column in _COLUMNS}
    seq = int(data.pop("seq"))
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    event = WorkflowEvent.model_validate(data)
    return {"seq": seq, "event": event.model_dump(mode="json")}


def read_events(
    services: Services,
    *,
    project_id: str | None = None,
    after: int = 0,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Return ``(items, next_cursor, has_more)`` ordered by durable ``seq``."""

    clauses = ["seq > ?"]
    params: list[Any] = [after]
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    params.append(limit + 1)
    sql = (
        "SELECT " + ", ".join(_COLUMNS) + " FROM workflow_events "
        f"WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?"
    )
    rows = services.db.fetchall(sql, params)
    has_more = len(rows) > limit
    items = [_to_item(row) for row in rows[:limit]]
    next_cursor = items[-1]["seq"] if items else after
    return items, next_cursor, has_more


__all__ = ["read_events"]

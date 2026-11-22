"""Routing and thin handlers for the local JSON control plane.

Each handler maps a validated HTTP request onto an existing application service
(or a presentation helper such as ``TaskService.task_view``).  No handler
executes repository code, shells out, or reimplements a business rule.  Long
work stays in the separate worker: ``POST .../run`` starts it detached.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from mayaworkbench.api.errors import ApiError
from mayaworkbench.api.eventlog import read_events
from mayaworkbench.api.validation import (
    ID_RE,
    PROJECT_REF_RE,
    SEARCH_MODES,
    param,
)
from mayaworkbench.app.container import Services
from mayaworkbench.errors import NotFoundError


@dataclass
class StreamSpec:
    """A newline-delimited JSON response produced lazily by ``lines``."""

    lines: Iterator[str]
    content_type: str = "application/x-ndjson"


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _enum_value(value: Any) -> Any:
    """Serialise a StrEnum to its value without changing plain values."""

    return getattr(value, "value", value)


# Only these payload keys may reach a timeline item's free-text detail. This
# keeps approval nonces, HMACs, full tool arguments and other secrets out of the
# read-only timeline even though the underlying event payload is unrestricted.
_SAFE_EVENT_DETAIL_KEYS = (
    "state",
    "previous_state",
    "reason",
    "action",
    "summary",
    "title",
    "name",
    "status",
    "outcome",
    "decision",
    "phase",
    "note",
    "message",
)


def _event_detail(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    parts = [
        f"{key}={str(payload[key])[:200]}"
        for key in _SAFE_EVENT_DETAIL_KEYS
        if key in payload and payload[key] not in (None, "")
    ]
    if not parts:
        parts.append(f"{event.get('entity_type', 'entity')}={event.get('entity_id', '')}")
    return "; ".join(parts)[:1000]


def _tool_call_detail(call: dict[str, Any]) -> str:
    """The only tool-call fields exposed: no arguments, approvals or secrets."""

    fields = {
        "tool_name": call.get("tool_name"),
        "status": call.get("status"),
        "duration_ms": call.get("duration_ms"),
        "exit_code": call.get("exit_code"),
        "error": call.get("error"),
    }
    return json.dumps(fields, sort_keys=True, default=str)[:2000]


def health_payload(services: Services) -> dict[str, Any]:
    """A cheap liveness/readiness probe; no host paths or secrets are exposed."""

    return {
        "status": "ok",
        "service": "maya-control-plane",
        "api_version": "1.0",
        "project_count": services.projects.count(),
        "event_count": services.events.count(),
    }


def _temp_root() -> str | None:
    candidate = Path("/tmp/opencode")
    return str(candidate) if candidate.is_dir() else None


class ControlPlane:
    """The immutable application object shared by all request threads."""

    def __init__(self, services: Services) -> None:
        self.svc = services

    # ------------------------------------------------------------------ router
    def route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> Any:
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        if method == "GET":
            return self._get(path, query)
        if method == "POST":
            return self._post(path, body)
        raise ApiError(405, "METHOD_NOT_ALLOWED", f"Method {method} is not supported.")

    # -------------------------------------------------------------------- gets
    def _get(self, path: str, query: dict[str, list[str]]) -> Any:
        svc = self.svc

        if path == "/health":
            return health_payload(svc)

        if path == "/projects":
            rows = []
            for project in svc.projects.list():
                repositories = svc.repositories.list_by_project(project.id)
                snapshots = svc.snapshots.list_for_project(project.id, limit=1000)
                rows.append(
                    {
                        "id": project.id,
                        "slug": project.slug,
                        "name": project.name,
                        "repositories": len(repositories),
                        "snapshots": len(snapshots),
                        "indexed": sum(1 for item in snapshots if item.indexed_at is not None),
                        "created": project.created_at.isoformat(),
                    }
                )
            return {"projects": rows}

        if path.startswith("/projects/"):
            identifier = self._id_segment(path, "/projects/")
            project = svc.projects.get(identifier)
            repositories = svc.repositories.list_by_project(project.id)
            snapshots = svc.snapshots.list_for_project(project.id, limit=10)
            return {
                "project": _dump(project),
                "repositories": [_dump(item) for item in repositories],
                "recent_snapshots": [_dump(item) for item in snapshots],
            }

        if path == "/repositories":
            project = self._project(query)
            return {
                "project": project.slug,
                "repositories": [
                    _dump(item) for item in svc.repositories.list_by_project(project.id)
                ],
            }

        if path == "/snapshots":
            project = self._project(query)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=1000, default=100)
            return {
                "project": project.slug,
                "snapshots": [
                    _dump(item)
                    for item in svc.snapshots.list_for_project(project.id, limit=limit)
                ],
            }

        if path.startswith("/index/"):
            snapshot_id = self._id_segment(path, "/index/")
            return {"map": _dump(svc.maps.build(snapshot_id))}

        if path == "/search":
            query_text = param(query, "q", required=True, max_len=256)
            mode = param(query, "mode", default="auto", allow=SEARCH_MODES)
            snapshot_id = param(query, "snapshot", pattern=ID_RE, default=None)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=200, default=25)
            _project, _repository, snapshot = svc.resolve_snapshot_context(
                project_identifier=param(query, "project", pattern=PROJECT_REF_RE, default=None),
                snapshot_id=snapshot_id,
                require_indexed=True,
            )
            hits, expression = svc.search.search_text(
                snapshot.id, query_text, mode=mode, limit=limit
            )
            return {
                "snapshot_id": snapshot.id,
                "query": query_text,
                "mode": mode,
                "fts_expression": expression,
                "hits": [_dump(item) for item in hits],
            }

        if path == "/symbols":
            query_text = param(query, "q", required=True, max_len=256)
            snapshot_id = param(query, "snapshot", pattern=ID_RE, default=None)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=200, default=25)
            _project, _repository, snapshot = svc.resolve_snapshot_context(
                project_identifier=param(query, "project", pattern=PROJECT_REF_RE, default=None),
                snapshot_id=snapshot_id,
                require_indexed=True,
            )
            hits = svc.search.search_symbols(snapshot.id, query_text, limit=limit)
            return {
                "snapshot_id": snapshot.id,
                "query": query_text,
                "symbols": [_dump(item) for item in hits],
            }

        if path.startswith("/research/reproduction/"):
            run_id = self._id_segment(path, "/research/reproduction/")
            svc.runs.get(run_id)  # 404 when the run does not exist
            # A read must stay a read: no new artifact or event per GET.
            report, _ = svc.reproduction.build(run_id, persist=False)
            return {"report": _dump(report)}

        if path == "/research/documents":
            project = self._project(query)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=1000, default=50)
            documents = svc.documents.list_by_project(project.id, limit=limit)
            return {"documents": [_dump(item) for item in documents]}

        if path == "/research/claims":
            project = self._project(query)
            document_id = param(query, "document", pattern=ID_RE, default=None)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=1000, default=100)
            claims = svc.claim_service.list_claims(
                project_id=project.id, document_id=document_id, limit=limit
            )
            return {"claims": [_dump(item) for item in claims]}

        if path == "/tasks":
            project = self._project(query)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=1000, default=100)
            return {
                "tasks": [
                    _dump(item)
                    for item in svc.task_service.list_tasks(project_id=project.id, limit=limit)
                ]
            }

        if path.startswith("/tasks/"):
            task_id = self._id_segment(path, "/tasks/")
            return svc.task_service.task_view(task_id)

        if path.startswith("/runs/"):
            rest = path[len("/runs/") :]
            if rest.endswith("/diff"):
                run_id = self._id_segment(rest, "", suffix="/diff")
                svc.runs.get(run_id)  # 404 when the run does not exist
                diff = svc.diffs.get_by_run(run_id)
                if diff is None:
                    raise ApiError(
                        404,
                        "NOT_FOUND",
                        "This run has no captured candidate patch",
                        hint="Let the worker finish or call `maya task diff <run>` first.",
                    )
                return {
                    "run_id": run_id,
                    "patch": svc.artifacts.read_text(diff.patch_artifact_id),
                    "patch_sha256": diff.patch_sha256,
                    "base_commit_sha": diff.base_commit_sha,
                    "files": [_dump(item) for item in diff.files],
                    "additions": diff.additions,
                    "deletions": diff.deletions,
                }
            if rest.endswith("/events"):
                run_id = self._id_segment(rest, "", suffix="/events")
                svc.runs.get(run_id)  # 404 when the run does not exist
                limit = param(query, "limit", as_int=True, minimum=1, maximum=1000, default=200)
                events = svc.events.list(run_id=run_id, limit=limit)
                return {
                    "run_id": run_id,
                    "events": [_dump(item) for item in events],
                }
            if rest.endswith("/timeline"):
                run_id = self._id_segment(rest, "", suffix="/timeline")
                return self._timeline(run_id)
            if rest.endswith("/plan"):
                run_id = self._id_segment(rest, "", suffix="/plan")
                return self._plan(run_id)
            if rest.endswith("/evidence"):
                run_id = self._id_segment(rest, "", suffix="/evidence")
                return self._evidence(run_id)
            run_id = self._id_segment(path, "/runs/")
            return svc.task_service.run_view(run_id)

        if path == "/workspaces":
            return {"workspaces": [_dump(item) for item in svc.workspace_service.list()]}

        if path.startswith("/workspaces/") and path.endswith("/graph"):
            workspace_id = self._id_segment(path, "/workspaces/", suffix="/graph")
            return svc.workspace_service.graph(workspace_id)

        if path.startswith("/verification/") and path.endswith("/report"):
            run_id = self._id_segment(path, "/verification/", suffix="/report")
            svc.runs.get(run_id)  # 404 when the run does not exist
            report = svc.verification.latest_report(run_id)
            if report is None:
                raise ApiError(
                    404,
                    "NOT_FOUND",
                    f"Run {run_id} has no verification report.",
                    hint="Run verification for the task before requesting its report.",
                )
            checks = svc.verification.report_checks(report)
            return {"report": _dump(report), "checks": [_dump(item) for item in checks]}

        if path == "/experiments":
            project_id = None
            project_raw = param(query, "project", pattern=PROJECT_REF_RE, default=None)
            if project_raw is not None:
                project_id = svc.projects.get(project_raw).id
            limit = param(query, "limit", as_int=True, minimum=1, maximum=1000, default=100)
            definitions = svc.experiment_store.list_definitions(project_id=project_id, limit=limit)
            return {
                "experiments": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "role": item.role.value,
                        "profile": item.profile.value,
                        "status": item.status.value,
                        "seeds": len(item.seeds),
                        "runs": len(svc.experiment_store.list_runs(item.id)),
                        "task": item.task_id,
                    }
                    for item in definitions
                ]
            }

        if path.startswith("/experiments/") and path.endswith("/comparison"):
            identifier = self._id_segment(path, "/experiments/", suffix="/comparison")
            return self._comparison(identifier)

        if path == "/events/stream":
            return self._stream_events(query)

        if path == "/events":
            after = param(query, "after", as_int=True, minimum=0, default=0)
            limit = param(query, "limit", as_int=True, minimum=1, maximum=500, default=100)
            project_raw = param(query, "project", pattern=PROJECT_REF_RE, default=None)
            project_id = svc.projects.get(project_raw).id if project_raw else None
            items, next_cursor, has_more = read_events(
                svc, project_id=project_id, after=after, limit=limit
            )
            return {
                "events": items,
                "after": after,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }

        if path.startswith("/evidence/") and path.endswith("/bundle"):
            run_id = self._id_segment(path, "/evidence/", suffix="/bundle")
            return self._bundle(run_id)

        raise ApiError(404, "NOT_FOUND", f"No route for GET {path}.", hint="Check docs/api-control-plane.md.")

    # ------------------------------------------------------------------- posts
    def _post(self, path: str, body: dict[str, Any]) -> Any:
        svc = self.svc
        if path.startswith("/tasks/") and path.endswith("/approve"):
            task_id = self._id_segment(path, "/tasks/", suffix="/approve")
            approval_id = self._body_str(body, "approval_id", max_len=128, pattern=ID_RE)
            svc.tasks.get(task_id)  # 404 when the task does not exist
            approval = svc.task_service.approve(approval_id)
            return {"task_id": task_id, "approval": _dump(approval)}

        if path.startswith("/tasks/") and path.endswith("/run"):
            task_id = self._id_segment(path, "/tasks/", suffix="/run")
            svc.tasks.get(task_id)
            result = svc.task_service.run_task(task_id, detached=True)
            return {
                "task_id": task_id,
                "run": _dump(result.run),
                "job": _dump(result.job),
            }

        for action in ("pause", "resume", "cancel"):
            if path.startswith("/tasks/") and path.endswith("/" + action):
                task_id = self._id_segment(path, "/tasks/", suffix="/" + action)
                svc.tasks.get(task_id)
                run = svc.task_service.latest_run_for_task(task_id)
                if run is None:
                    raise ApiError(404, "NOT_FOUND", f"Task {task_id} has no run.")
                if action == "pause":
                    updated = svc.task_service.pause_run(run.id)
                elif action == "resume":
                    updated = svc.task_service.resume_run(run.id)
                else:
                    reason = self._body_str(body, "reason", max_len=500, required=False) or (
                        "cancelled via the local control plane"
                    )
                    updated = svc.task_service.cancel_run(run.id, reason=reason)
                return {"task_id": task_id, "run": _dump(updated)}

        raise ApiError(404, "NOT_FOUND", f"No route for POST {path}.", hint="Check docs/api-control-plane.md.")

    # --------------------------------------------------------------- streaming
    def _stream_events(self, query: dict[str, list[str]]) -> StreamSpec:
        svc = self.svc
        after = param(query, "after", as_int=True, minimum=0, default=0)
        limit = param(query, "limit", as_int=True, minimum=1, maximum=500, default=100)
        seconds = param(query, "seconds", as_int=True, minimum=1, maximum=30, default=10)
        project_raw = param(query, "project", pattern=PROJECT_REF_RE, default=None)
        project_id = svc.projects.get(project_raw).id if project_raw else None

        def generate() -> Iterator[str]:
            cursor = after
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                items, new_cursor, _has_more = read_events(
                    svc, project_id=project_id, after=cursor, limit=limit
                )
                for item in items:
                    yield json.dumps({"type": "event", **item}, sort_keys=True) + "\n"
                    cursor = item["seq"]
                if not items:
                    yield json.dumps({"type": "heartbeat", "seq": cursor}, sort_keys=True) + "\n"
                time.sleep(0.25)
            yield json.dumps({"type": "end", "seq": cursor}, sort_keys=True) + "\n"

        return StreamSpec(generate())

    # ------------------------------------------------------------------ bundle
    def _bundle(self, run_id: str) -> dict[str, Any]:
        svc = self.svc
        with tempfile.TemporaryDirectory(prefix="maya-api-bundle-", dir=_temp_root()) as tmp:
            destination = Path(tmp) / "bundle"
            result = svc.verification.export_bundle(run_id, destination)
            files: dict[str, dict[str, str]] = {}
            for path in sorted(destination.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(destination).as_posix()
                data = path.read_bytes()
                try:
                    files[relative] = {
                        "encoding": "utf-8",
                        "content": data.decode("utf-8"),
                    }
                except UnicodeDecodeError:
                    files[relative] = {
                        "encoding": "base64",
                        "content": base64.b64encode(data).decode("ascii"),
                    }
            return {"manifest": result.manifest, "files": files}

    # --------------------------------------------------- workbench read views
    def _timeline(self, run_id: str) -> dict[str, Any]:
        """Ordered, read-only event + tool-call stream for one run."""

        svc = self.svc
        run = svc.runs.get(run_id)  # 404 when the run does not exist
        raw_events, next_cursor, has_more = read_events(
            svc, project_id=run.project_id, after=0, limit=500
        )
        calls = svc.calls.list_by_run(run_id, limit=200)

        items: list[dict[str, Any]] = []
        for row in raw_events:
            event = row["event"]
            if event.get("run_id") != run_id:
                continue
            items.append(
                {
                    "seq": row["seq"],
                    "at": event.get("occurred_at"),
                    "kind": "event",
                    "label": _enum_value(event.get("event_type")),
                    "detail": _event_detail(event),
                    "ref": event.get("entity_id") or None,
                }
            )
        for call_dump in (_dump(call) for call in calls):
            if call_dump.get("run_id") != run_id:
                continue
            items.append(
                {
                    "seq": None,
                    "at": call_dump.get("started_at"),
                    "kind": "tool_call",
                    "label": call_dump.get("tool_name"),
                    "detail": _tool_call_detail(call_dump),
                    "ref": call_dump.get("id"),
                }
            )

        items.sort(
            key=lambda item: (
                item["at"] or "",
                item["seq"] if item["seq"] is not None else -1,
                item["kind"],
            )
        )
        truncated = bool(has_more) or len(raw_events) >= 500 or len(calls) >= 200
        return {"run_id": run_id, "items": items, "truncated": truncated}

    def _plan(self, run_id: str) -> dict[str, Any]:
        """Approved plan, contract projection, scoped approvals and workspace."""

        svc = self.svc
        run = svc.runs.get(run_id)  # 404 when the run does not exist
        contract = svc.contracts.get(run.contract_id)

        plan = None
        try:
            plan = svc.plans.latest_for_run(run_id)
        except NotFoundError:
            if run.plan_id:
                try:
                    plan = svc.plans.get(run.plan_id)
                except NotFoundError:
                    plan = None

        approvals = [
            {
                "id": item.id,
                "action": _enum_value(item.action),
                "status": _enum_value(item.status),
                "resource_scope": item.resource_scope,
                "spec_sha256": item.spec_sha256,
                "expires_at": item.expires_at.isoformat(),
                "reason": item.reason,
            }
            for item in svc.approval_service.list_for_run(run_id)
        ]

        workspace = svc.workspaces.get_by_run(run_id)
        workspace_view = None
        if workspace is not None:
            workspace_view = {
                "id": workspace.id,
                "path": workspace.path,
                "base_commit_sha": workspace.base_commit_sha,
                "status": _enum_value(workspace.status),
                "file_count": workspace.file_count,
            }

        contract_view = {
            "goal": contract.goal,
            "acceptance_criteria": list(contract.acceptance_criteria),
            "constraints": _dump(contract.constraints),
            "scope": _dump(contract.scope),
            "required_approvals": [_enum_value(item) for item in contract.required_approvals],
            "execution_trust": contract.constraints.execution_trust,
            "content_sha256": contract.content_sha256,
        }
        return {
            "run_id": run_id,
            "contract": contract_view,
            "plan": _dump(plan) if plan is not None else None,
            "steps": [_dump(step) for step in plan.steps] if plan is not None else [],
            "approvals": approvals,
            "workspace": workspace_view,
        }

    def _evidence(self, run_id: str) -> dict[str, Any]:
        """Read-only evidence graph for one run; degraded sources are named."""

        svc = self.svc
        run = svc.runs.get(run_id)  # 404 when the run does not exist
        warnings: list[str] = []

        report = svc.verification.latest_report(run_id)
        checks = svc.verification.report_checks(report) if report is not None else []
        links = svc.verification.store.list_links(report.id) if report is not None else []

        mappings_view: list[dict[str, Any]] = []
        try:
            for mapping in svc.mappings.list_by_snapshot(run.snapshot_id):
                mappings_view.append(
                    {
                        "claim_id": mapping.claim_id,
                        "status": _enum_value(mapping.status),
                        "rule_status": _enum_value((mapping.details or {}).get("rule_status")),
                        "evidence_count": len(svc.mappings.list_evidence(mapping.id)),
                        "gaps": list(mapping.gaps),
                    }
                )
        except Exception:  # noqa: BLE001 - a degraded read must not hide the rest
            warnings.append("claim mappings unavailable for this snapshot")

        reproduction = None
        try:
            reproduction, _artifact_id = svc.reproduction.build(run_id, persist=False)
        except Exception:  # noqa: BLE001 - degrade honestly, never fabricate a tier
            reproduction = None
            warnings.append("reproduction report unavailable for this run")

        comparison = svc.experiments.latest_comparison_for_task(run.task_id)

        return {
            "run_id": run_id,
            "report": _dump(report) if report is not None else None,
            "checks": [_dump(item) for item in checks],
            "links": [_dump(item) for item in links],
            "mappings": mappings_view,
            "reproduction": _dump(reproduction) if reproduction is not None else None,
            "comparison": _dump(comparison) if comparison is not None else None,
            "warnings": warnings,
        }

    def _comparison(self, identifier: str) -> dict[str, Any]:
        """Latest experiment comparison by comparison id or definition id."""

        svc = self.svc
        if identifier.startswith("xcmp_"):
            comparison = svc.experiment_store.get_comparison(identifier)  # 404 if unknown
        else:
            definition = svc.experiment_store.get_definition(identifier)  # 404 if unknown
            comparison = svc.experiments.latest_comparison_for_task(definition.task_id)
            if comparison is None:
                raise ApiError(
                    404,
                    "NOT_FOUND",
                    f"Experiment {identifier} has no comparison yet.",
                    hint="Run and compare the experiment before requesting its comparison.",
                )

        report_markdown = None
        try:
            report_markdown = svc.experiments.comparison_markdown(comparison.id)
        except Exception:  # noqa: BLE001 - Markdown is best-effort, not a verdict
            report_markdown = None
        return {"comparison": _dump(comparison), "report_markdown": report_markdown}

    # ----------------------------------------------------------------- helpers
    def _project(self, query: dict[str, list[str]]):  # type: ignore[no-untyped-def]
        raw = param(query, "project", pattern=PROJECT_REF_RE, default=None)
        return self.svc.resolve_project(raw)

    @staticmethod
    def _id_segment(
        path: str,
        prefix: str,
        *,
        suffix: str = "",
    ) -> str:
        value = path[len(prefix) :]
        if suffix and value.endswith(suffix):
            value = value[: -len(suffix)]
        value = value.strip("/")
        if not ID_RE.fullmatch(value):
            raise ApiError(400, "INVALID_ID", "The path identifier has an invalid format.")
        return value

    @staticmethod
    def _body_str(
        body: dict[str, Any],
        name: str,
        *,
        max_len: int = 256,
        pattern: Any = None,
        required: bool = True,
    ) -> str | None:
        value = body.get(name)
        if value is None or value == "":
            if required:
                raise ApiError(
                    400,
                    "MISSING_FIELD",
                    f"Missing required JSON field '{name}'.",
                )
            return None
        if not isinstance(value, str):
            raise ApiError(400, "INVALID_FIELD", f"JSON field '{name}' must be a string.")
        if len(value) > max_len:
            raise ApiError(
                400,
                "FIELD_TOO_LONG",
                f"JSON field '{name}' exceeds the {max_len}-character limit.",
            )
        if pattern is not None and not pattern.fullmatch(value):
            raise ApiError(400, "INVALID_FIELD", f"JSON field '{name}' has an invalid format.")
        return value


__all__ = ["ControlPlane", "StreamSpec", "health_payload"]

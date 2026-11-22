"""End-to-end tests for the local JSON control plane.

A real ``ThreadingHTTPServer`` runs on an ephemeral loopback port in a thread,
backed by a real ``Services`` object over a temporary home.  Requests use
``urllib`` (no third-party HTTP client) and never leave localhost.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from mayaworkbench.api import create_server
from mayaworkbench.app.container import Services
from mayaworkbench.domain.enums import TaskType
from mayaworkbench.verification.registry import command_check

# Never route loopback probes through a host proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep 3xx responses visible so redirect headers can be asserted."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
)


def _served_html(base: str) -> str:
    with _OPENER.open(base + "/", timeout=5) as response:
        return response.read().decode("utf-8")


@pytest.fixture
def api_server(services: Services):
    server = create_server(services, host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(base: str, path: str) -> tuple[int, Any]:
    return _request(base, "GET", path)


def _post(base: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    return _request(base, "POST", path, body if body is not None else {})


def _make_task(services: Services, indexed) -> Any:
    task, _contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Fix utils",
        goal="Change count_tokens to a simple split",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["count_tokens returns the number of words"],
        verification_checks=[
            command_check(
                "python -c \"from utils import count_tokens; assert count_tokens('a b') == 2\"",
                criteria=["AC1"],
                timeout_seconds=30,
            )
        ],
        constraints={
            "max_steps": 8,
            "max_tool_calls": 20,
            "max_model_tokens": 20_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
        },
    )
    return task


def test_root_serves_truth_surface_ui(api_server: str) -> None:
    response = _OPENER.open(api_server + "/", timeout=5)
    html = response.read().decode("utf-8")
    assert response.status == 200
    assert "text/html" in response.headers.get("Content-Type", "")
    assert "events/stream" in html and "approvals" in html
    assert "no auth in this local alpha" in html


def test_source_diff_panel_and_route(api_server: str) -> None:
    """The UI has a real diff panel backed by the stored candidate patch."""
    html = _served_html(api_server)
    assert 'id="diff"' in html
    assert "'/runs/'" in html and "'/diff'" in html
    status, payload = _get(api_server, "/runs/not-a-real-run/diff")
    assert status == 404
    assert payload["code"] == "NOT_FOUND"


def test_browser_client_uses_fetch_ndjson_reader_not_sse(api_server: str) -> None:
    """The browser client must read the NDJSON stream with fetch, not EventSource.

    A future regression to ``new EventSource(...)`` (SSE) would break the stream
    contract, so this test pins the client technology directly. A DOM-free check
    is appropriate here: the served asset is a single inline HTML document.
    """
    html = _served_html(api_server)
    assert "new EventSource" not in html
    assert "EventSource" not in html
    assert "fetch(" in html
    # The three pieces of a fetch-based NDJSON reader.
    assert "getReader" in html
    assert "TextDecoder" in html
    assert "JSON.parse" in html
    assert "application/x-ndjson" in html
    assert "heartbeat" in html and "'end'" in html


def test_browser_client_wires_approvals_to_task_view(api_server: str) -> None:
    """Approvals must be rendered from the task view and granted via the API."""
    html = _served_html(api_server)
    assert "getElementById('approvals')" in html
    # The task view (GET /tasks/{id}) shape is consumed by the panel.
    assert "view.contract" in html and "view.runs" in html
    # The Approve action posts to the real route with the required field.
    assert "/approve" in html
    assert "approval_id" in html
    assert "method: 'POST'" in html


def test_ui_alias_redirects_to_root(api_server: str) -> None:
    request = urllib.request.Request(api_server + "/ui", method="GET")
    try:
        _NO_REDIRECT_OPENER.open(request, timeout=5)
        pytest.fail("GET /ui should not be a 2xx response")
    except urllib.error.HTTPError as exc:
        assert 300 <= exc.code < 400
        assert exc.code == 302
        assert exc.headers.get("Location") == "/"

    # Following the redirect with a normal opener serves the truth-surface HTML.
    with _OPENER.open(api_server + "/ui", timeout=5) as response:
        html = response.read().decode("utf-8")
        assert response.status == 200
        assert "text/html" in response.headers.get("Content-Type", "")
    assert "events/stream" in html and "approvals" in html


def test_health_and_project_reads(api_server: str, services: Services, project) -> None:
    status, payload = _get(api_server, "/health")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["service"] == "maya-control-plane"
    assert payload["project_count"] >= 1

    status, payload = _get(api_server, "/projects")
    assert status == 200
    assert any(row["slug"] == project.slug for row in payload["projects"])

    status, payload = _get(api_server, f"/projects/{project.id}")
    assert status == 200
    assert payload["project"]["id"] == project.id
    assert payload["repositories"] == []

    # Project query params accept a slug or a prefixed ULID id (uppercase).
    status, payload = _get(api_server, f"/events?project={project.id}&limit=5")
    assert status == 200
    assert payload["events"]

    status, payload = _get(api_server, "/projects/not-a-project")
    assert status == 404
    assert set(payload) == {"error", "code", "hint"}
    assert payload["code"] == "NOT_FOUND"


def test_repository_snapshot_index_search_reads(
    api_server: str, services: Services, indexed
) -> None:
    project = indexed.project
    slug = project.slug

    status, payload = _get(api_server, f"/repositories?project={slug}")
    assert status == 200
    assert payload["repositories"][0]["id"] == indexed.repository.id

    status, payload = _get(api_server, f"/snapshots?project={slug}")
    assert status == 200
    assert any(row["id"] == indexed.snapshot.id for row in payload["snapshots"])

    status, payload = _get(api_server, f"/index/{indexed.snapshot.id}")
    assert status == 200
    assert payload["map"]["snapshot_id"] == indexed.snapshot.id
    assert payload["map"]["files"] >= 1

    query = urllib.parse.quote("CausalAttention")
    status, payload = _get(
        api_server,
        f"/search?q={query}&snapshot={indexed.snapshot.id}&mode=auto&limit=5",
    )
    assert status == 200
    assert payload["mode"] == "auto"
    assert payload["hits"]

    status, payload = _get(
        api_server, f"/symbols?q={query}&snapshot={indexed.snapshot.id}"
    )
    assert status == 200
    assert any("CausalAttention" in item["qualified_name"] for item in payload["symbols"])


def test_research_workspace_verification_experiment_reads(
    api_server: str, services: Services, project, paper
) -> None:
    status, payload = _get(api_server, f"/research/documents?project={project.slug}")
    assert status == 200
    assert any(item["id"] == paper.document.id for item in payload["documents"])

    status, payload = _get(api_server, f"/research/claims?document={paper.document.id}")
    assert status == 200
    assert "claims" in payload

    workspace = services.workspace_service.create(project.id, "overview", title="Overview")
    status, payload = _get(api_server, "/workspaces")
    assert status == 200
    assert any(item["id"] == workspace.id for item in payload["workspaces"])

    status, payload = _get(api_server, f"/workspaces/{workspace.id}/graph")
    assert status == 200
    assert payload["workspace"]["id"] == workspace.id

    status, payload = _get(api_server, "/experiments")
    assert status == 200
    assert payload["experiments"] == []

    status, payload = _get(api_server, "/verification/run_missing/report")
    assert status == 404
    assert payload["code"] == "NOT_FOUND"

    status, payload = _get(api_server, "/evidence/run_missing/bundle")
    assert status == 404
    assert payload["code"] == "NOT_FOUND"


def test_task_approve_run_cancel_lifecycle(
    api_server: str,
    services: Services,
    indexed,
    task_model,
    monkeypatch,
) -> None:
    # The HTTP handler must not launch a long-running worker process; the real
    # service is still used for every state change and approval check.
    monkeypatch.setattr(services.task_service, "spawn_worker", lambda run_id: 0)
    task = _make_task(services, indexed)
    start = services.task_service.start_task(task.id)
    run_id = start.run.id
    assert start.approvals

    status, payload = _get(api_server, f"/tasks?project={indexed.project.slug}")
    assert status == 200
    assert any(item["id"] == task.id for item in payload["tasks"])

    status, payload = _get(api_server, f"/tasks/{task.id}")
    assert status == 200
    assert payload["task"]["id"] == task.id
    assert payload["contract"]["task_id"] == task.id

    for approval in start.approvals:
        status, payload = _post(
            api_server,
            f"/tasks/{task.id}/approve",
            {"approval_id": approval.id},
        )
        assert status == 200
        assert payload["approval"]["status"] == "APPROVED"

    status, payload = _post(api_server, f"/tasks/{task.id}/run")
    assert status == 200
    assert payload["run"]["id"] == run_id
    assert payload["run"]["state"] == "IMPLEMENTING"

    status, payload = _get(api_server, f"/runs/{run_id}")
    assert status == 200
    assert payload["run"]["id"] == run_id
    assert payload["approvals"]

    status, payload = _get(api_server, f"/runs/{run_id}/events")
    assert status == 200
    assert any(item["event_type"] == "TASK_STATE_CHANGED" for item in payload["events"])

    status, payload = _post(api_server, f"/tasks/{task.id}/cancel", {"reason": "api test"})
    assert status == 200
    assert payload["run"]["state"] == "CANCELLED"


def test_error_shapes_400_403_404(
    api_server: str,
    services: Services,
    indexed,
    task_model,
) -> None:
    # 400 — validation
    for path in ("/search", "/search?q=attention&mode=not-a-mode", "/symbols"):
        status, payload = _get(api_server, path)
        assert status == 400
        assert set(payload) == {"error", "code", "hint"}
        assert payload["code"] in {"MISSING_PARAMETER", "INVALID_PARAMETER"}

    # 403 — a policy/approval refusal, not a malformed request
    task = _make_task(services, indexed)
    services.task_service.start_task(task.id)
    status, payload = _post(api_server, f"/tasks/{task.id}/run")
    assert status == 403
    assert set(payload) == {"error", "code", "hint"}
    assert payload["code"] == "APPROVAL_ERROR"

    # 404 — unknown entity
    status, payload = _get(api_server, "/tasks/task_does_not_exist")
    assert status == 404
    assert payload["code"] == "NOT_FOUND"

    # 400 — malformed approve body
    status, payload = _post(api_server, f"/tasks/{task.id}/approve", {})
    assert status == 400
    assert payload["code"] == "MISSING_FIELD"


def test_event_cursor_reconnect_and_ndjson_stream(
    api_server: str, services: Services, project
) -> None:
    status, payload = _get(api_server, f"/events?project={project.slug}&limit=10")
    assert status == 200
    assert payload["events"]
    first_cursor = payload["next_cursor"]
    assert all(item["seq"] <= first_cursor for item in payload["events"])

    workspace = services.workspace_service.create(project.id, "timeline", title="Timeline")
    assert workspace.id

    status, payload = _get(
        api_server, f"/events?project={project.slug}&after={first_cursor}&limit=10"
    )
    assert status == 200
    assert payload["after"] == first_cursor
    assert payload["events"]
    assert all(item["seq"] > first_cursor for item in payload["events"])
    assert any(item["event"]["event_type"] == "WORKSPACE_CREATED" for item in payload["events"])

    # NDJSON is read raw: it is a stream of independent JSON lines, not one
    # object. Pin the server's content type to the browser client's fetch-based
    # NDJSON reader so the two cannot drift apart unnoticed.
    url = api_server + f"/events/stream?after={first_cursor}&seconds=1&limit=10"
    with _OPENER.open(url, timeout=10) as response:
        assert response.status == 200
        assert "application/x-ndjson" in response.headers.get("Content-Type", "")
        raw = response.read().decode("utf-8")
    html = _served_html(api_server)
    assert "new EventSource" not in html
    assert "application/x-ndjson" in html and "getReader" in html
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert lines
    assert lines[-1]["type"] == "end"
    assert any(line["type"] == "heartbeat" for line in lines)
    event_lines = [line for line in lines if line["type"] == "event"]
    assert event_lines
    assert all(item["seq"] > 0 for item in event_lines)
    assert all("event" in item for item in event_lines)


def test_cli_api_commands_register_and_real_health_probe(
    api_server: str, tmp_path
) -> None:
    import argparse

    from mayaworkbench.cli.api_commands import cmd_api_health, register_api_commands
    from mayaworkbench.cli.output import Console

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_api_commands(sub)

    args = parser.parse_args(["api", "serve", "--port", "9123", "--home", str(tmp_path / "h")])
    assert args.func.__name__ == "cmd_api_serve"
    assert args.port == 9123
    assert args.host == "127.0.0.1"
    assert args.home == str(tmp_path / "h")

    port = int(api_server.rsplit(":", 1)[1])
    health_args = argparse.Namespace(
        host="127.0.0.1",
        port=port,
        console=Console(json_mode=True),
    )
    assert cmd_api_health(health_args) == 0


def test_security_path_and_query_limits(api_server: str) -> None:
    for path in ("/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/evidence/../secrets/bundle"):
        status, payload = _get(api_server, path)
        assert status == 400
        assert payload["code"] == "INVALID_PATH"

    status, payload = _get(api_server, "/search?q=" + "x" * 6000)
    assert status == 400
    assert payload["code"] in {"REQUEST_TOO_LARGE", "PARAMETER_TOO_LONG"}

    oversized_body = {"approval_id": "y" * 5000}
    status, payload = _post(api_server, "/tasks/task_1/approve", oversized_body)
    assert status == 400
    assert payload["code"] == "FIELD_TOO_LONG"


def test_workbench_read_endpoints_with_real_data(
    api_server: str,
    services: Services,
    indexed,
    task_model,
) -> None:
    """The four WP-UI-1 read routes project real run/experiment state."""

    from tests.test_verification import (
        CHECK_OK,
        attach_external_comparison,
        make_reviewing_run,
    )

    reviewed = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    run = reviewed.run
    report, _checks = services.task_service.verify_run(run.id, include_review=False)
    assert report is not None

    comparison = attach_external_comparison(
        services,
        indexed,
        reviewed.task.id,
        baseline_values={1: 0.9, 2: 0.85},
        candidate_values={1: 0.7, 2: 0.6},
    )

    # 1. timeline — ordered event/tool-call items with the exact item contract.
    status, payload = _get(api_server, f"/runs/{run.id}/timeline")
    assert status == 200
    assert set(payload) == {"run_id", "items", "truncated"}
    assert payload["run_id"] == run.id
    assert isinstance(payload["truncated"], bool)
    items = payload["items"]
    assert items
    assert all(
        {"seq", "at", "kind", "label", "detail", "ref"} <= set(item) for item in items
    )
    assert any(item["kind"] == "event" for item in items)
    order_keys = [
        (item["at"], item["seq"] if item["seq"] is not None else -1, item["kind"])
        for item in items
    ]
    assert order_keys == sorted(order_keys)
    # Tool-call details never carry raw arguments or secrets.
    for item in items:
        if item["kind"] == "tool_call":
            assert "arguments" not in item["detail"]

    # 2. plan — contract projection, plan steps, scoped approvals, workspace.
    status, payload = _get(api_server, f"/runs/{run.id}/plan")
    assert status == 200
    assert set(payload) == {
        "run_id",
        "contract",
        "plan",
        "steps",
        "approvals",
        "workspace",
    }
    assert payload["run_id"] == run.id
    assert set(payload["contract"]) == {
        "goal",
        "acceptance_criteria",
        "constraints",
        "scope",
        "required_approvals",
        "execution_trust",
        "content_sha256",
    }
    assert payload["contract"]["goal"] == reviewed.contract.goal
    assert payload["contract"]["content_sha256"] == reviewed.contract.content_sha256
    assert payload["plan"] is not None
    assert payload["plan"]["id"] == run.plan_id
    assert payload["steps"]
    assert payload["approvals"]
    for approval in payload["approvals"]:
        assert set(approval) == {
            "id",
            "action",
            "status",
            "resource_scope",
            "spec_sha256",
            "expires_at",
            "reason",
        }
    assert payload["workspace"] is not None
    assert set(payload["workspace"]) == {
        "id",
        "path",
        "base_commit_sha",
        "status",
        "file_count",
    }
    assert payload["workspace"]["id"] == reviewed.workspace.id
    # No approval nonces, HMACs or fixture secrets leak through /plan.
    plan_blob = json.dumps(payload)
    for secret in ("nonce", "approval_hmac", "hmac", "sk-not-a-real-key"):
        assert secret not in plan_blob

    # 3. evidence — report, checks, links, mappings, reproduction, comparison.
    status, payload = _get(api_server, f"/runs/{run.id}/evidence")
    assert status == 200
    assert set(payload) == {
        "run_id",
        "report",
        "checks",
        "links",
        "mappings",
        "reproduction",
        "comparison",
        "warnings",
    }
    assert payload["run_id"] == run.id
    assert payload["report"] is not None
    assert payload["report"]["id"] == report.id
    assert payload["checks"]
    assert isinstance(payload["links"], list)
    assert payload["links"]
    assert isinstance(payload["mappings"], list)
    for mapping in payload["mappings"]:
        assert set(mapping) == {
            "claim_id",
            "status",
            "rule_status",
            "evidence_count",
            "gaps",
        }
    assert payload["reproduction"] is not None
    assert "tiers" in payload["reproduction"]
    assert payload["comparison"] is not None
    assert payload["comparison"]["id"] == comparison.id
    assert isinstance(payload["warnings"], list)

    # 4. comparison — by comparison id and by definition id.
    status, payload = _get(api_server, f"/experiments/{comparison.id}/comparison")
    assert status == 200
    assert set(payload) == {"comparison", "report_markdown"}
    assert payload["comparison"]["id"] == comparison.id
    assert isinstance(payload["report_markdown"], str)
    assert payload["report_markdown"]

    status, payload = _get(
        api_server, f"/experiments/{comparison.candidate_experiment_id}/comparison"
    )
    assert status == 200
    assert payload["comparison"]["id"] == comparison.id


def test_workbench_read_routes_404_for_unknown_ids(api_server: str) -> None:
    """Missing runs and experiments yield typed NOT_FOUND responses."""

    for suffix in ("timeline", "plan", "evidence"):
        status, payload = _get(api_server, f"/runs/run_does_not_exist/{suffix}")
        assert status == 404
        assert payload["code"] == "NOT_FOUND"

    for identifier in ("xcmp_does_not_exist", "exp_does_not_exist"):
        status, payload = _get(api_server, f"/experiments/{identifier}/comparison")
        assert status == 404
        assert payload["code"] == "NOT_FOUND"

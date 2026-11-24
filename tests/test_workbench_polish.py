"""WP7: X-ray, DAG, session trace, project report and accessible UI."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from mayaworkbench.api import create_server
from mayaworkbench.app.container import Services

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def api_server(services: Services):
    server = create_server(services, host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, dict]:
    try:
        with _OPENER.open(base + path, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def indexed_project(services, indexed):
    return indexed.project.id, indexed.snapshot.id


def test_project_xray_reports_coverage_and_symbols(api_server, indexed_project):
    project_id, snapshot_id = indexed_project
    status, payload = _get(api_server, f"/projects/{project_id}/xray")
    assert status == 200
    assert payload["snapshot_id"] == snapshot_id
    assert isinstance(payload["files"], list)
    assert isinstance(payload["symbols"], list)
    assert payload["coverage"]
    assert "not semantic parsing" in payload["note"]


def test_project_report_is_exportable_and_content_addressed(api_server, indexed_project):
    project_id, _ = indexed_project
    status, payload = _get(api_server, f"/projects/{project_id}/report")
    assert status == 200
    assert payload["project"]["id"] == project_id
    assert len(payload["canonical_sha256"]) == 64
    assert "not a scientific proof" in payload["note"]


def test_run_dag_and_session_trace_are_read_only(api_server, services, indexed, task_model):
    # A run that exists in the graph yields a DAG with a state node.
    from mayaworkbench.domain.enums import TaskType

    task, contract = services.task_service.create_task(
        project_id=indexed.project.id, title="DAG task", goal="test dag",
        task_type=TaskType.ENGINEERING_CHANGE, snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"], acceptance_criteria=["ok"],
        constraints={"allow_unknown_cost": True},
    )
    started = services.task_service.start_task(task.id)
    run_id = started.run.id
    status, dag = _get(api_server, f"/runs/{run_id}/dag")
    assert status == 200
    assert any(node["type"] == "state" for node in dag["nodes"])
    assert "facts" in dag["note"]

    status, trace = _get(api_server, f"/runs/{run_id}/sessions")
    assert status == 200
    assert isinstance(trace["events"], list)
    assert "no live process" in trace["note"]


def test_r2i_graph_route_is_read_only(api_server, indexed_project):
    project_id, _ = indexed_project
    status, payload = _get(api_server, f"/projects/{project_id}/r2i-graph")
    assert status == 200
    assert payload["counts"]["claims"] == 0
    assert payload["edges"] == []


def test_ui_has_keyboard_navigation_and_new_panels(api_server):
    with _OPENER.open(api_server + "/", timeout=5) as response:
        html = response.read().decode("utf-8")
    for marker in (
        'id="xrayPanel"', 'id="dagPanel"', 'id="tracePanel"', 'id="reportPanel"',
        'loadDag(', 'loadTrace(', 'renderTrace(', 'keydown', ':focus-visible',
        'max-width:720px', 'aria-label',
    ):
        assert marker in html, marker

"""Browser-half acceptance tests for WP-UI-1 (workbench trust surfaces).

A real ``ThreadingHTTPServer`` on an ephemeral loopback port serves the inline
HTML asset, mirroring the fixture in ``tests/test_api_control_plane.py``.

Playwright is not installed in this environment, so the served document (a
single self-contained HTML file) is inspected directly with DOM-free, offline
assertions. If a Playwright package is available, one optional test exercises
the page in a real browser; it is skipped otherwise. No request leaves
localhost and no browser/model service is required for the core assertions.
"""

from __future__ import annotations

import threading
import urllib.request

import pytest

from mayaworkbench.api import create_server
from mayaworkbench.app.container import Services

# Never route loopback probes through a host proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _served_html(base: str) -> str:
    with _OPENER.open(base + "/", timeout=5) as response:
        assert response.status == 200
        assert "text/html" in response.headers.get("Content-Type", "")
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


def test_workbench_ui_has_all_new_panels(api_server: str) -> None:
    """Every new supervisable panel and the single Load run control exist."""
    html = _served_html(api_server)
    new_ids = [
        "runLoadId",
        "workspacePanel",
        "taskOverviewPanel",
        "planPanel",
        "timelinePanel",
        "evidenceGraphPanel",
        "comparisonPanel",
    ]
    for panel_id in new_ids:
        assert 'id="' + panel_id + '"' in html, panel_id

    for heading in (
        "Load run",
        "Workspace",
        "Task overview",
        "Plan &amp; approvals",
        "Execution timeline",
        "Evidence graph",
        "Experiment comparator",
    ):
        assert heading in html, heading

    # Existing panels are retained, not replaced.
    for existing in (
        "Health",
        "Projects / workspaces",
        "Tasks",
        "Approvals",
        "Live events (durable cursor)",
        "Source diff",
        "Verification / evidence",
        "Task controls",
    ):
        assert existing in html, existing
    for existing_id in (
        "health",
        "projects",
        "task",
        "approvals",
        "events",
        "diff",
        "evidence",
        "controlStatus",
    ):
        assert 'id="' + existing_id + '"' in html, existing_id


def test_workbench_ui_loads_four_run_endpoints(api_server: str) -> None:
    """One Load run action fetches the run view, plan, timeline and evidence."""
    html = _served_html(api_server)
    assert "async function loadRun()" in html
    assert "Promise.all([" in html
    # The four parallel fetch calls, by their literal path fragments.
    assert "'/runs/'" in html
    assert "'/plan'" in html
    assert "'/timeline'" in html
    assert "'/evidence'" in html
    # The comparator has its own id input and comparison route.
    assert "/experiments/" in html
    assert "'/comparison'" in html


def test_workbench_ui_renders_structured_data_not_json_dumps(api_server: str) -> None:
    """New panels use DOM rendering helpers, status colours and honest empties."""
    html = _served_html(api_server)
    for name in (
        "renderWorkspace",
        "renderTaskOverview",
        "renderPlanApprovals",
        "renderTimeline",
        "renderEvidenceGraph",
        "loadComparison",
        "approvePlan",
    ):
        assert ("function " + name) in html, name
    assert "createElement" in html
    assert "textContent" in html

    # Criteria / tier rendering vocabulary is present.
    for token in (
        "VERIFIED",
        "PARTIAL",
        "FAILED",
        "UNVERIFIED",
        "METHOD_CODE_FOUND",
        "METHOD_IMPLEMENTED",
        "METHOD_VALIDATED",
        "RESULT_REPRODUCED",
    ):
        assert token in html, token

    # Timeline is bounded, reconnect-safe and reports backend truncation.
    assert "MAX_TIMELINE_ITEMS" in html
    assert "truncated" in html
    assert "capped at" in html

    # Honest empty states, never a fabricated success.
    for empty in ("not available", "(not recorded)", "(none recorded)", "(no "):
        assert empty in html, empty


def test_workbench_ui_text_content_only_and_stream_intact(api_server: str) -> None:
    """No markup injection sink; the NDJSON fetch reader is preserved."""
    html = _served_html(api_server)
    assert "innerHTML" not in html
    assert "EventSource" not in html
    assert "eval(" not in html
    # Fetch-based NDJSON reader (MW-04) is still present.
    assert "fetch(" in html
    assert "getReader" in html
    assert "TextDecoder" in html
    assert "JSON.parse" in html
    assert "application/x-ndjson" in html
    assert "heartbeat" in html and "'end'" in html


def test_workbench_ui_approval_post_and_no_secret_dump(api_server: str) -> None:
    """Approve posts the exact approval id and no secret fields appear."""
    html = _served_html(api_server)
    assert "/approve" in html
    assert "approval_id" in html
    assert "method: 'POST'" in html
    assert "PENDING" in html
    # Approval nonce / HMAC must never be part of the served asset.
    assert "nonce" not in html
    assert "approval_hmac" not in html


def test_browser_render_if_playwright_available(api_server: str) -> None:
    """Optional real-browser smoke test; skipped when Playwright is absent."""
    pytest.importorskip(
        "playwright.sync_api",
        reason="Playwright not installed; DOM-free assertions above are authoritative",
    )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser binary here
            pytest.skip(f"Playwright browser unavailable: {exc}")
        try:
            errors: list[str] = []
            page = browser.new_page()
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(api_server + "/", timeout=10_000)
            page.fill("#runLoadId", "run_does_not_exist")
            page.click("button:has-text('Load run')")
            page.wait_for_function(
                "document.getElementById('taskOverviewPanel')"
                ".textContent.indexOf('not available') >= 0",
                timeout=10_000,
            )
            assert "not available" in page.text_content("#taskOverviewPanel") or (
                "not available" in (page.text_content("#evidenceGraphPanel") or "")
            )
            assert errors == []
        finally:
            browser.close()

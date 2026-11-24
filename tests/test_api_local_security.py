"""MW-NEW-P1-09: the local control plane's request security contract.

The server is a single-user local service with no authentication, so its
protection is the browser's own request metadata plus a loopback bind:

* a foreign ``Host`` header (DNS rebinding) is refused;
* a cross-origin or cross-site mutation is refused;
* a same-origin mutation reaches the router;
* a non-loopback bind is refused unless the operator explicitly opts in;
* every response carries the no-store / no-frame / no-referrer headers.

Read-only GETs stay usable from a local page because no CORS headers are ever
sent, so a browser cannot read their bodies cross-origin.
"""

from __future__ import annotations

import http.client
import json
import threading
from urllib.parse import urlsplit

import pytest

from mayaworkbench.api import create_server
from mayaworkbench.app.container import Services


@pytest.fixture
def api(services: Services):
    server = create_server(services, host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    base = f"http://{host}:{port}"
    try:
        yield Simple(base, host, port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Simple:
    def __init__(self, base: str, host: str, port: int):
        self.base = base
        self.host = host
        self.port = port


def _request(
    api: Simple,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> tuple[int, dict, dict[str, str]]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    outgoing = {"Content-Type": "application/json"} if payload else {}
    outgoing.update(headers or {})
    connection = http.client.HTTPConnection(api.host, api.port, timeout=5)
    try:
        connection.request(method, path, body=payload, headers=outgoing)
        response = connection.getresponse()
        raw = response.read()
        return (
            response.status,
            json.loads(raw) if raw else {},
            {key.lower(): value for key, value in response.getheaders()},
        )
    finally:
        connection.close()


def test_foreign_host_header_is_refused(api):
    status, payload, _headers = _request(
        api, "POST", "/tasks/task_x/cancel", headers={"Host": "evil.example"}
    )
    assert status == 403
    assert payload["code"] == "HOST_NOT_LOOPBACK"


def test_cross_origin_mutation_is_refused(api):
    status, payload, _headers = _request(
        api,
        "POST",
        "/tasks/task_x/cancel",
        headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin"},
    )
    assert status == 403
    assert payload["code"] == "CROSS_ORIGIN_REQUEST"


def test_cross_site_fetch_metadata_is_refused(api):
    status, payload, _headers = _request(
        api, "POST", "/tasks/task_x/cancel", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert status == 403
    assert payload["code"] == "CROSS_SITE_REQUEST"


def test_same_origin_mutation_reaches_the_router(api):
    status, payload, headers = _request(
        api,
        "POST",
        "/tasks/task_x/cancel",
        headers={"Origin": api.base, "Sec-Fetch-Site": "same-origin"},
    )
    # The guard passed; the unknown task is the router's 404, not a 403.
    assert status == 404
    assert payload["code"] == "NOT_FOUND"
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"].lower() == "deny"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_read_only_get_carries_security_headers(api):
    status, payload, headers = _request(api, "GET", "/health")
    assert status == 200 and payload["status"] == "ok"
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"


def test_remote_bind_requires_an_explicit_opt_in(services, monkeypatch):
    monkeypatch.delenv("MAYA_API_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError, match="refusing to bind"):
        create_server(services, host="0.0.0.0", port=0)
    monkeypatch.setenv("MAYA_API_ALLOW_REMOTE", "1")
    server = create_server(services, host="0.0.0.0", port=0)
    server.server_close()

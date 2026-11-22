"""A stdlib-only threaded HTTP/JSON server for the local control plane.

The server binds to 127.0.0.1 by default and refuses to become a remote service
unless a caller explicitly passes a different host.  It owns no business
logic: it parses the request, delegates to :class:`ControlPlane`, and serialises
the result (or an NDJSON stream) back to the caller.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from mayaworkbench.api.app import ControlPlane, StreamSpec
from mayaworkbench.api.errors import ApiError, internal_error, map_maya_error
from mayaworkbench.api.validation import (
    MAX_BODY_BYTES,
    ensure_safe_path,
    guard_request_path,
    parse_body,
)
from mayaworkbench.app.container import Services
from mayaworkbench.errors import MayaError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_UI_PATH = Path(__file__).with_name("ui") / "index.html"
UI_HTML = _UI_PATH.read_text(encoding="utf-8") if _UI_PATH.is_file() else ""


class ControlPlaneHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MayaControlPlane/1.0"
    sys_version = ""

    @property
    def app(self) -> ControlPlane:
        return self.server.app  # type: ignore[attr-defined,no-any-return]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Access lines are truncated so a hostile query string cannot flood the
        # local log; full tracebacks (logged via log_error) are always kept.
        message = fmt % args
        if "Unhandled" in message or "Traceback" in message:
            sys.stderr.write(message + "\n")
            return
        sys.stderr.write(message[:300] + ("…" if len(message) > 300 else "") + "\n")

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            guard_request_path(parsed)
            path = ensure_safe_path(unquote(parsed.path))
            if method == "GET" and path == "/":
                self._send_html(UI_HTML)
                return
            if method == "GET" and path == "/ui":
                # Stable, user-friendly alias for the truth-surface UI. The
                # target is a fixed literal, so no request data reaches a header.
                self._send_redirect("/")
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            body = self._read_body() if method == "POST" else {}
            result = self.app.route(method, path, query, body)
            if isinstance(result, StreamSpec):
                self._send_stream(result)
            else:
                self._send_json(200, result)
        except ApiError as exc:
            self._send_json(exc.status, exc.to_payload())
        except MayaError as exc:
            mapped = map_maya_error(exc)
            self._send_mapped(mapped)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:  # noqa: BLE001 - top-level HTTP boundary
            # Keep details in the local server log only; never in the response.
            self.log_error("Unhandled control-plane error:\n%s", traceback.format_exc())
            self._send_json(500, internal_error())

    def _send_mapped(self, mapped: ApiError) -> None:
        self._send_json(mapped.status, mapped.to_payload())

    def _read_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(400, "INVALID_LENGTH", "Content-Length must be an integer.")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(400, "REQUEST_TOO_LARGE", "The request body is too large.")
        return parse_body(self.rfile.read(length))

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _send_redirect(self, location: str) -> None:
        data = b""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, spec: StreamSpec) -> None:
        self.send_response(200)
        self.send_header("Content-Type", spec.content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        # With Connection: close the client reads until EOF; no Content-Length
        # and no chunk framing are needed for this bounded NDJSON response.
        self.close_connection = True
        try:
            for line in spec.lines:
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def create_server(
    services: Services,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Build (but do not start) a server bound to ``host:port``.

    ``port=0`` asks the OS for an ephemeral port, which is what the tests use.
    The returned server carries ``app`` for request dispatch.
    """

    server = ThreadingHTTPServer((host, port), ControlPlaneHandler)
    server.app = ControlPlane(services)  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def run_server(server: ThreadingHTTPServer) -> None:
    """Block on ``serve_forever`` and tear the listener down cleanly."""

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = ["create_server", "run_server", "DEFAULT_HOST", "DEFAULT_PORT"]

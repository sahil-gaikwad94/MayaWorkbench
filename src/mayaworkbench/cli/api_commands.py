"""CLI front end for the local HTTP/JSON control plane.

Registration lives here (rather than in ``cli/main.py``) so the parent agent can
wire it into the parser without merging this module with other work packages.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any

from mayaworkbench.errors import UsageError


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def register_api_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    api = sub.add_parser("api", help="Serve and probe the local JSON control plane")
    api_sub = api.add_subparsers(dest="api_command", metavar="<action>")

    serve = api_sub.add_parser("serve", help="Serve the JSON API on localhost")
    serve.add_argument("--port", type=int, default=8765, help="TCP port (default: 8765)")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address; 127.0.0.1 by default (local-only)",
    )
    # SUPPRESS keeps the global `--home` value when this option is omitted.
    serve.add_argument(
        "--home",
        default=argparse.SUPPRESS,
        help="Data directory (default: $MAYA_HOME or ~/.mayaworkbench)",
    )
    serve.set_defaults(func=cmd_api_serve)

    health = api_sub.add_parser("health", help="GET /health from a running server")
    health.add_argument("--port", type=int, default=8765)
    health.add_argument("--host", default="127.0.0.1")
    health.set_defaults(func=cmd_api_health)


def cmd_api_serve(args: argparse.Namespace) -> int:
    from mayaworkbench.api import create_server, run_server

    host = args.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        args.console.warning(
            f"Binding to {host!r} exposes the control plane beyond loopback; "
            "it has no authentication and is intended for a single local user."
        )
    if not (0 <= args.port <= 65535):
        raise UsageError("--port must be between 0 and 65535.")

    services = _services(args)
    server = create_server(services, host=host, port=args.port)
    actual_port = server.server_address[1]
    args.console.print(f"maya api listening on http://{host}:{actual_port}")
    args.console.print("Local-only, single-user, no authentication. Press Ctrl-C to stop.")
    run_server(server)
    return 0


def cmd_api_health(args: argparse.Namespace) -> int:
    console = args.console
    url = f"http://{args.host}:{args.port}/health"
    # An empty ProxyHandler keeps this probe strictly local even if the host has
    # HTTP_PROXY set; the control plane has no authentication and must stay local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UsageError(
            f"Cannot reach {url}: {exc}",
            hint="Start `maya api serve` in another terminal first.",
        )
    if console.json_mode:
        console.json(payload)
        return 0
    console.print(f"maya api: {payload.get('status', 'unknown')} at {url}")
    console.kv(
        [
            ("service", payload.get("service")),
            ("api_version", payload.get("api_version")),
            ("projects", payload.get("project_count")),
            ("events", payload.get("event_count")),
        ]
    )
    return 0


__all__ = ["register_api_commands", "cmd_api_serve", "cmd_api_health"]

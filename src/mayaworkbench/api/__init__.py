"""Local HTTP/JSON control plane over the existing application services.

This package is a thin transport layer: routing, validation and durable event
streaming sit on top of the same ``Services`` composition root the CLI uses.
It adds no dependency beyond the Python standard library.
"""

from mayaworkbench.api.app import ControlPlane, StreamSpec, health_payload
from mayaworkbench.api.errors import ApiError
from mayaworkbench.api.server import DEFAULT_HOST, DEFAULT_PORT, create_server, run_server

__all__ = [
    "ApiError",
    "ControlPlane",
    "StreamSpec",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "create_server",
    "health_payload",
    "run_server",
]

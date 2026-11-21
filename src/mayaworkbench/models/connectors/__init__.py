"""External tool connectors. All calls pass through the registry policy gate.

The original WP7 read-only GitHub connector and its ``build_default_connector_registry``
remain available unchanged. WP12 adds four connectors (GitHub with an approval-gated
draft-PR proposal, read-only SQLite, read-only log tail, and static infrastructure
validate/plan/diff) plus a registry that owns consent, scopes, one-time approvals
and an append-only audit log.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.models.capabilities import ConnectorRegistry as _LegacyConnectorRegistry
from mayaworkbench.models.connectors.github import (
    ADVANCED_CONTRACT,
    READ_CONTRACT,
    ALLOWED_HOSTS,
    HttpReply,
    GitHubConnector,
    GitHubReadOnlyConnector,
)

LegacyConnectorRegistry = _LegacyConnectorRegistry


def build_default_connector_registry(
    *, http: Any = None, token: str | None = None
) -> _LegacyConnectorRegistry:
    """Build the WP7 in-process registry with the one shipped read-only connector.

    ``http`` is injectable so tests and offline CLI commands never use the real
    network. A missing token does not prevent listing connectors; calling one
    fails closed at invoke time.
    """
    registry = _LegacyConnectorRegistry()
    registry.register(GitHubReadOnlyConnector(http=http, token=token))
    return registry


def build_advanced_connector_registry(**kwargs: Any):  # type: ignore[no-untyped-def]
    """Build the WP12 registry with all four connectors and explicit consent."""
    from mayaworkbench.models.connectors.registry import build_advanced_connector_registry as build

    return build(**kwargs)


__all__ = [
    "ALLOWED_HOSTS",
    "ADVANCED_CONTRACT",
    "READ_CONTRACT",
    "HttpReply",
    "GitHubReadOnlyConnector",
    "GitHubConnector",
    "LegacyConnectorRegistry",
    "build_default_connector_registry",
    "build_advanced_connector_registry",
]

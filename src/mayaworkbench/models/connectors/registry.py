"""Advanced connector registry with consent, scope and provenance (WP12).

The registry is the authority, not any connector instance and not the model:

* every call is hash-bound to its connector, operation and public params;
* side-effecting operations require an explicit, one-time, spec-hash-bound
  approval grant issued by ``register_approved_action``;
* ``revoke(name)`` drops every grant and removes the connector;
* tokens come from the environment only; they are never written into the
  ``ConnectorResult`` payload or a ``describe()`` record.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from mayaworkbench.models.capabilities import ConnectorError, ConnectorRegistry
from mayaworkbench.models.connectors.contracts import (
    APPROVAL_PARAM,
    ApprovalGrant,
    spec_sha256,
)

#: A grant is valid until this time; set expiry here or via the grant record.
GRANT_TTL_SECONDS = 7 * 24 * 60 * 60

PolicyGate = Callable[[str, str, dict[str, Any]], None]


class AdvancedConnectorRegistry:
    """Builds a policy-gated, audit-shaped advanced registry (WP12)."""

    def __init__(self, *, policy_gate: PolicyGate | None = None):
        self._registry = ConnectorRegistry(policy_gate=policy_gate)
        self._grants: dict[tuple[str, str], ApprovalGrant] = {}
        self._events: list[dict[str, Any]] = []

    def build(self) -> ConnectorRegistry:
        return self._registry

    # ------------------------------------------------------------- lifecycle
    def add(self, connector: Any) -> None:
        self._registry.register(connector)

    def register_mcp(
        self, adapter: Any, *, read_only_allowlist: tuple[str, ...] = ()
    ) -> Any:
        """Discover an MCP adapter's tools and register them as operations.

        Discovery is untrusted data: each tool is mapped to a local risk tier
        by the adapter (unknown tools default to ``EXTERNAL_WRITE``). The
        returned ``ToolConnector`` routes every call through this registry's
        consent and one-time grant path; only explicitly allowlisted
        ``READ_ONLY`` tools may be called without a grant.
        """
        from mayaworkbench.models.connectors.mcp import McpConnector

        adapter.discover()
        connector = McpConnector(adapter, read_only_allowlist=read_only_allowlist)
        self.add(connector)
        return connector

    def names(self) -> tuple[str, ...]:
        return self._registry.names()

    def get(self, name: str) -> Any:
        return self._registry.get(name)

    def describe(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self._registry.describe():
            out.append(self._redact_description(item))
        return out

    @staticmethod
    def _redact_description(item: dict[str, Any]) -> dict[str, Any]:
        text = str(item.get("contract_summary", ""))
        token = os.environ.get("MAYA_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            text = text.replace(token, "[REDACTED]")
        return {**item, "contract_summary": text}

    def is_revoked(self, name: str) -> bool:
        return name not in self._registry._connectors

    def revoke(self, name: str) -> None:
        for key in [key for key in self._grants if key[0] == name]:
            del self._grants[key]
        self._registry.revoke(name)
        self._events.append({"type": "connector_revoked", "connector": name})

    def revoke_all(self) -> None:
        self._grants.clear()
        self._registry.revoke_all()

    # ------------------------------------------------------------ approve+run
    def approve(self, name: str, operation: str, params: dict[str, Any], *, actor: str = "user") -> ApprovalGrant:
        connector = self._registry.get(name)
        public = dict(params)
        public.pop(APPROVAL_PARAM, None)
        spec = spec_sha256(
            {"connector": name, "operation": operation, "target": connector.describe_target(operation, public), "params": public}
        )
        grant = ApprovalGrant(
            approval_id=f"appr_{len(self._grants) + 1}_{name}_{operation}",
            connector=name,
            operation=operation,
            spec_sha256=spec,
            actor=actor,
        )
        self._grants[(name, operation)] = grant
        self._events.append(
            {
                "type": "approval_granted",
                "connector": name,
                "operation": operation,
                "spec_sha256": spec,
                "actor": actor,
                "grant_id": grant.approval_id,
            }
        )
        return grant

    def invoke_with_consent(
        self, name: str, operation: str, params: dict[str, Any] | None = None
    ) -> Any:
        connector = self._registry.get(name)
        public = dict(params or {})
        public.pop(APPROVAL_PARAM, None)
        if not connector.supports(operation):
            raise ConnectorError(
                f"Connector {name!r} does not permit operation {operation!r}",
                hint=f"Allowed: {', '.join(connector.operations()) or '(none)'}",
            )
        spec = spec_sha256(
            {
                "connector": name,
                "operation": operation,
                "target": connector.describe_target(operation, public),
                "params": public,
            }
        )
        grant = self._grants.get((name, operation))
        if grant is None or grant.spec_sha256 != spec or connector.is_revoked():
            raise ConnectorError(
                f"Operation {operation!r} on connector {name!r} is not approved for this call",
                hint="Call the approval flow first; grants are scoped by connector, operation and params.",
            )
        result = self._registry.invoke(
            name, operation, {**public, APPROVAL_PARAM: grant.model_dump()}
        )
        del self._grants[(name, operation)]  # one-time
        self._events.append(
            {
                "type": "connector_invoked",
                "connector": name,
                "operation": operation,
                "target": connector.describe_target(operation, public),
                "request_hash": result.request_hash,
                "response_hash": result.response_hash,
                "read_only": result.read_only,
            }
        )
        return result

    def invoke_allowlisted(
        self, name: str, operation: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Run an explicitly allowlisted READ_ONLY operation without a grant.

        This is additive: it does not touch ``_grants`` and does not relax
        ``invoke_with_consent``. Only operations a connector publishes through
        ``allowlisted_operations()`` (the MCP adapter's READ_ONLY tools) qualify;
        everything else still needs a one-time approval grant.
        """
        connector = self._registry.get(name)
        published = getattr(connector, "allowlisted_operations", None)
        allowed = set(published() if callable(published) else ())
        if operation not in allowed:
            raise ConnectorError(
                f"Operation {operation!r} on connector {name!r} is not allowlisted for unapproved calls",
                hint="Only declared READ_ONLY tools can be called without a grant.",
            )
        public = dict(params or {})
        result = self._registry.invoke(name, operation, public)
        self._events.append(
            {
                "type": "connector_allowlisted_invoked",
                "connector": name,
                "operation": operation,
                "target": connector.describe_target(operation, public),
                "request_hash": result.request_hash,
                "response_hash": result.response_hash,
                "read_only": result.read_only,
            }
        )
        return result

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


def build_advanced_connector_registry(
    *,
    policy_gate: PolicyGate | None = None,
    http: Any = None,
    github_token: str | None = None,
    db_path: str | None = None,
    log_paths: tuple[str, ...] = (),
    infra_paths: tuple[str, ...] = (),
) -> AdvancedConnectorRegistry:
    """Build the advanced registry with github (read + draft-PR), database, logs, infra."""
    from mayaworkbench.models.connectors.database import DatabaseConnector
    from mayaworkbench.models.connectors.github import GitHubConnector, GitHubReadOnlyConnector
    from mayaworkbench.models.connectors.infrastructure import InfrastructureConnector
    from mayaworkbench.models.connectors.observability import ObservabilityConnector

    registry = AdvancedConnectorRegistry(policy_gate=policy_gate)
    if github_token is None:
        github_token = os.environ.get("MAYA_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    del GitHubReadOnlyConnector  # the advanced connector supports all read operations too
    registry.add(GitHubConnector(http=http, token=github_token))
    from pathlib import Path

    if db_path is not None:
        registry.add(DatabaseConnector(allowed_dirs=(str(Path(db_path).parent),)))
    if log_paths:
        registry.add(ObservabilityConnector(allowed_dirs=tuple(log_paths)))
    if infra_paths:
        registry.add(InfrastructureConnector(allowed_dirs=tuple(infra_paths)))
    if os.environ.get("MAYA_MLFLOW_TRACKING_URL"):
        from mayaworkbench.models.connectors.mlflow import MlflowConnector

        registry.add(MlflowConnector(opener=http))
    return registry


__all__ = ["AdvancedConnectorRegistry", "build_advanced_connector_registry", "ApprovalGrant"]

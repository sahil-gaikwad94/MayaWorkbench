"""WP12 end-to-end: advanced connectors are consent-gated, read-only by default, and audited."""

from __future__ import annotations

import json
import sqlite3

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.models.capabilities import ConnectorError
from mayaworkbench.models.connectors.contracts import APPROVAL_PARAM, spec_sha256
from mayaworkbench.models.connectors.database import DatabaseConnector
from mayaworkbench.models.connectors.github import HttpReply
from mayaworkbench.models.connectors.infrastructure import InfrastructureConnector
from mayaworkbench.models.connectors.observability import ObservabilityConnector
from mayaworkbench.models.connectors.registry import (
    AdvancedConnectorRegistry,
    build_advanced_connector_registry,
)


def _github_connector(http=None):
    from mayaworkbench.models.connectors.github import GitHubConnector

    return GitHubConnector(token="test-token", http=http or (lambda *a, **k: HttpReply(200, b"[]")))


def _db_connector(tmp_path):
    db_path = tmp_path / "data.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    db.execute("INSERT INTO items VALUES (1, 'one')")
    db.commit()
    db.close()
    return DatabaseConnector(allowed_dirs=(str(tmp_path),))


def test_registry_lists_connectors_and_redacts_tokens(monkeypatch):
    monkeypatch.setenv("MAYA_GITHUB_TOKEN", "super-secret-token")
    registry = build_advanced_connector_registry(db_path="/tmp/x.db", log_paths=("/tmp",))
    assert set(registry.names()) >= {"github", "database", "observability"}
    assert "super-secret-token" not in json.dumps(registry.describe())


def test_read_operations_require_no_grant_but_side_effects_do():
    registry = AdvancedConnectorRegistry()
    registry.add(_github_connector())
    result = registry.build().invoke("github", "list_issues", {"owner": "o", "repo": "r"})
    assert result.request_hash and result.response_hash
    with pytest.raises(ConnectorError, match="not approved"):
        registry.invoke_with_consent("github", "create_draft_pr", {"owner": "o", "repo": "r"})


def test_consent_grant_is_spec_hash_bound_and_one_time():
    registry = AdvancedConnectorRegistry()
    registry.add(_github_connector())
    params = {"owner": "o", "repo": "r", "head": "feature", "base": "main", "title": "Draft"}
    grant = registry.approve("github", "create_draft_pr", params)
    changed = {**params, "title": "Changed after approval"}
    with pytest.raises(ConnectorError, match="not approved"):
        registry.invoke_with_consent("github", "create_draft_pr", changed)
    result = registry.invoke_with_consent("github", "create_draft_pr", params)
    assert result.connector == "github"
    assert registry.events()[-1]["type"] == "connector_invoked"
    with pytest.raises(ConnectorError, match="not approved"):
        registry.invoke_with_consent("github", "create_draft_pr", params)


def test_database_connector_is_read_only(tmp_path):
    connector = _db_connector(tmp_path)
    target = str(tmp_path / "data.db")
    result = connector.invoke("query", {"path": target, "sql": "SELECT name FROM items"})
    assert "one" in json.dumps(result.data)
    with pytest.raises(Exception):
        connector.invoke("query", {"path": target, "sql": "DELETE FROM items"})
    with pytest.raises(Exception):
        connector.invoke(
            "query", {"path": target, "sql": "ATTACH DATABASE '/tmp/x.db' AS other"}
        )


def test_observability_connector_redacts_and_caps(tmp_path):
    (tmp_path / "app.log").write_text("token sk-abcdefghijklmnopqrstuvwxyz0123456789\n")
    connector = ObservabilityConnector(allowed_dirs=(str(tmp_path),))
    result = connector.invoke("tail", {"path": str(tmp_path / "app.log"), "lines": 10})
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(result.data)
    with pytest.raises(Exception):
        connector.invoke("tail", {"path": "/etc/passwd"})


def test_infrastructure_connector_never_applies(tmp_path):
    (tmp_path / "main.tf").write_text('resource "null_resource" "x" {}\n')
    connector = InfrastructureConnector(allowed_dirs=(str(tmp_path),))
    plan = connector.invoke("plan", {"path": str(tmp_path / "main.tf")})
    assert plan.data
    assert plan.read_only is True
    assert "apply" not in connector.operations()


def test_revocation_clears_grants_and_blocks_calls():
    registry = AdvancedConnectorRegistry()
    registry.add(_github_connector())
    params = {"owner": "o", "repo": "r", "head": "h", "base": "b", "title": "t"}
    registry.approve("github", "create_draft_pr", params)
    registry.revoke("github")
    assert registry.is_revoked("github")
    with pytest.raises(ConnectorError):
        registry.invoke_with_consent("github", "create_draft_pr", params)


def test_connector_cli_lists_and_approves(tmp_path, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    run(["init", "--project", "connectors"])
    code, payload = run(["connector", "list"])
    assert code == 0
    assert isinstance(payload["connectors"], list)
    code, payload = run(
        [
            "connector",
            "approve",
            "github",
            "create_draft_pr",
            "--param",
            "owner=o",
            "--param",
            "repo=r",
        ]
    )
    assert code == 0
    assert len(payload["spec_sha256"]) == 64

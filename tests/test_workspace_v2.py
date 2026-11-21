"""WP4: the Engineering Workspace is a durable multi-resource scope."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import EvidenceStatus, ResourceKind, WorkspaceSessionState
from mayaworkbench.errors import NotFoundError, UsageError


def test_workspace_spans_repositories_datasets_and_environments(services, project, repo):
    service = services.workspace_service
    workspace = service.create(project.id, "full-stack", title="Frontend + API")
    assert workspace.slug == "full-stack"

    attached_repo = services.repository_service.attach(project, repo)
    resource = service.attach_resource(
        workspace.id,
        ResourceKind.REPOSITORY.value,
        "backend",
        attached_repo.repository.id,
    )
    dataset = service.attach_resource(
        workspace.id,
        ResourceKind.DATASET.value,
        "training-data",
        "dataset://local/train-v2",
        hash_sha256="a" * 64,
    )
    spec = service.add_environment(
        workspace.id,
        "python-dev",
        "python",
        descriptor_path="pyproject.toml",
        lockfile_path="uv.lock",
    )
    evidence = service.attach_evidence(
        workspace.id,
        "The backend reproduces the paper baseline",
        resource_id=resource.id,
        status=EvidenceStatus.UNVERIFIED.value,
    )

    graph = service.graph(workspace.id)
    assert graph["workspace"]["id"] == workspace.id
    assert {item["kind"] for item in graph["resources"]} == {"REPOSITORY", "DATASET"}
    assert graph["environments"][0]["hash_sha256"] == spec.hash_sha256
    assert graph["evidence"][0]["id"] == evidence.id
    assert dataset.hash_sha256 == "a" * 64


def test_workspace_rejects_unknown_resource_kind(services, project):
    workspace = services.workspace_service.create(project.id, "scope")
    try:
        services.workspace_service.attach_resource(workspace.id, "MAGIC", "x", "y")
    except UsageError as exc:
        assert "unknown resource kind" in str(exc)
    else:
        raise AssertionError("expected UsageError")


def test_workspace_session_record_lifecycle(services, project):
    service = services.workspace_service
    workspace = service.create(project.id, "session-scope")
    session = service.record_session(workspace.id, ["python", "-m", "pytest"])
    assert session.state is WorkspaceSessionState.OPEN
    closed = service.close_session(workspace.id, session.id, reason="task finished")
    assert closed.state is WorkspaceSessionState.STOPPED
    graph = service.graph(workspace.id)
    assert graph["sessions"][0]["details"]["close_reason"] == "task finished"


def test_workspace_cli_create_attach_and_graph(tmp_path, repo, capsys):
    home = tmp_path / "cli-home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    code, payload = run(["init", "--project", "workspace-project"])
    assert code == 0
    code, payload = run(["repo", "add", str(repo)])
    assert code == 0
    repository_id = payload["repository"]["id"]

    code, payload = run(["workspace", "create", "--project", "workspace-project", "--slug", "cli-scope"])
    assert code == 0
    workspace_id = payload["workspace"]["id"]

    code, payload = run(
        [
            "workspace",
            "attach",
            workspace_id,
            "--kind",
            "REPOSITORY",
            "--name",
            "backend",
            "--ref",
            repository_id,
        ]
    )
    assert code == 0
    code, payload = run(
        [
            "workspace",
            "environment",
            workspace_id,
            "--name",
            "python",
            "--ecosystem",
            "python",
            "--descriptor",
            "pyproject.toml",
        ]
    )
    assert code == 0
    code, payload = run(["workspace", "graph", workspace_id])
    assert code == 0
    assert len(payload["resources"]) == 1
    assert len(payload["environments"]) == 1

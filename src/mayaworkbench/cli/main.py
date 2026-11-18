"""MayaWorkbench command line interface.

The CLI is a thin presentation layer. Every command calls the same application
services the future HTTP API will call, so behaviour cannot drift between
surfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Sequence

from mayaworkbench.app.container import Services, slugify
from mayaworkbench.clock import utcnow
from mayaworkbench.config import (
    AppPaths,
    Settings,
    load_settings,
    redacted_settings_view,
    save_settings,
)
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    IssueKind,
    IssueSeverity,
    SnapshotStatus,
)
from mayaworkbench.domain.models import RepositorySnapshot
from mayaworkbench.errors import ConflictError, MayaError, NotFoundError, UsageError
from mayaworkbench.indexing.map import RepositoryMap
from mayaworkbench.observability.logging import configure_logging
from mayaworkbench.storage.db import Database
from mayaworkbench.version import (
    CONTRACT_VERSION,
    INDEXER_VERSION,
    MAYA_VERSION,
    QA_PROTOCOL_VERSION,
    SCHEMA_BUNDLE_VERSION,
)

VERSION_LINE = f"MayaWorkbench {MAYA_VERSION}"


# ------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maya",
        description=(
            "MayaWorkbench: repository intelligence with pinned snapshots, "
            "evidence-backed answers, and explicit limitations."
        ),
        epilog=(
            "Typical first run:\n"
            "  maya init --project my-research\n"
            "  maya repo add /path/to/repository\n"
            "  maya snapshot create\n"
            "  maya index\n"
            "  maya inspect\n"
            "  maya ask \"how is attention implemented?\"\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--home", help="Data directory (default: $MAYA_HOME or ~/.mayaworkbench)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="Verbose diagnostics on stderr")
    parser.add_argument("--quiet", action="store_true", help="Suppress warnings on stderr")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour")
    parser.add_argument("--version", action="version", version=VERSION_LINE)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # init ------------------------------------------------------------------
    init = sub.add_parser("init", help="Initialise the local workbench and a first project")
    init.add_argument("--project", help="Project name (default: current directory name)")
    init.add_argument("--slug", help="Explicit project slug")
    init.add_argument("--description", default="", help="Project description")
    init.set_defaults(func=cmd_init)

    # doctor ----------------------------------------------------------------
    doctor = sub.add_parser("doctor", help="Check the local environment and configuration")
    doctor.set_defaults(func=cmd_doctor, no_migrate=True)

    # project ---------------------------------------------------------------
    project = sub.add_parser("project", help="Manage projects")
    project_sub = project.add_subparsers(dest="project_command", metavar="<action>")
    p_create = project_sub.add_parser("create", help="Create a project")
    p_create.add_argument("name")
    p_create.add_argument("--slug")
    p_create.add_argument("--description", default="")
    p_create.set_defaults(func=cmd_project_create)
    p_list = project_sub.add_parser("list", help="List projects")
    p_list.set_defaults(func=cmd_project_list)
    p_show = project_sub.add_parser("show", help="Show one project")
    p_show.add_argument("identifier", nargs="?")
    p_show.set_defaults(func=cmd_project_show)

    # repo ------------------------------------------------------------------
    repo = sub.add_parser("repo", help="Manage attached repositories")
    repo_sub = repo.add_subparsers(dest="repo_command", metavar="<action>")
    r_add = repo_sub.add_parser("add", help="Attach a local Git repository")
    r_add.add_argument("path")
    r_add.add_argument("--project")
    r_add.add_argument("--name")
    r_add.set_defaults(func=cmd_repo_add)
    r_list = repo_sub.add_parser("list", help="List repositories")
    r_list.add_argument("--project")
    r_list.set_defaults(func=cmd_repo_list)

    # snapshot --------------------------------------------------------------
    snapshot = sub.add_parser("snapshot", help="Pin and inspect repository snapshots")
    snap_sub = snapshot.add_subparsers(dest="snapshot_command", metavar="<action>")
    s_create = snap_sub.add_parser("create", help="Pin a new snapshot")
    s_create.add_argument("--project")
    s_create.add_argument("--repo")
    s_create.add_argument(
        "--include-working-tree",
        action="store_true",
        help="Capture uncommitted changes as a patch snapshot (never mixed silently)",
    )
    s_create.set_defaults(func=cmd_snapshot_create)
    s_list = snap_sub.add_parser("list", help="List snapshots")
    s_list.add_argument("--project")
    s_list.add_argument("--repo")
    s_list.add_argument("--limit", type=int, default=20)
    s_list.set_defaults(func=cmd_snapshot_list)
    s_show = snap_sub.add_parser("show", help="Show one snapshot")
    s_show.add_argument("snapshot_id")
    s_show.set_defaults(func=cmd_snapshot_show)

    # index -----------------------------------------------------------------
    index = sub.add_parser("index", help="Index a pinned snapshot")
    index.add_argument("--project")
    index.add_argument("--repo")
    index.add_argument("--snapshot")
    index.set_defaults(func=cmd_index)

    # inspect ---------------------------------------------------------------
    inspect = sub.add_parser("inspect", help="Show the deterministic repository map")
    inspect.add_argument("--project")
    inspect.add_argument("--repo")
    inspect.add_argument("--snapshot")
    inspect.add_argument("--save", action="store_true", help="Store the map as an evidence artifact")
    inspect.set_defaults(func=cmd_inspect)

    # search ----------------------------------------------------------------
    search = sub.add_parser("search", help="Search text or symbols in an indexed snapshot")
    search.add_argument("query")
    search.add_argument("--project")
    search.add_argument("--repo")
    search.add_argument("--snapshot")
    search.add_argument(
        "--mode", choices=["auto", "lexical", "exact", "regex"], default="auto"
    )
    search.add_argument("--path", help="Restrict to a path prefix")
    search.add_argument("--case-sensitive", action="store_true")
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--symbols", action="store_true", help="Search symbols only")
    search.set_defaults(func=cmd_search)

    # symbol ----------------------------------------------------------------
    symbol = sub.add_parser("symbol", help="Inspect a symbol and its relationships")
    symbol_sub = symbol.add_subparsers(dest="symbol_command", metavar="<action>")
    sym_show = symbol_sub.add_parser("show", help="Show a symbol by id or qualified name")
    sym_show.add_argument("identifier")
    sym_show.add_argument("--project")
    sym_show.add_argument("--repo")
    sym_show.add_argument("--snapshot")
    sym_show.set_defaults(func=cmd_symbol_show)

    # ask -------------------------------------------------------------------
    ask = sub.add_parser("ask", help="Ask a grounded question about an indexed snapshot")
    ask.add_argument("question")
    ask.add_argument("--project")
    ask.add_argument("--repo")
    ask.add_argument("--snapshot")
    ask.add_argument("--budget", type=int, help="Context token budget")
    ask.add_argument("--no-model", action="store_true", help="Return retrieval evidence only")
    ask.set_defaults(func=cmd_ask)

    # qa --------------------------------------------------------------------
    qa = sub.add_parser("qa", help="Inspect stored question/answer records")
    qa_sub = qa.add_subparsers(dest="qa_command", metavar="<action>")
    qa_list = qa_sub.add_parser("list", help="List QA records")
    qa_list.add_argument("--project")
    qa_list.add_argument("--snapshot")
    qa_list.add_argument("--limit", type=int, default=20)
    qa_list.set_defaults(func=cmd_qa_list)
    qa_show = qa_sub.add_parser("show", help="Show one QA record")
    qa_show.add_argument("record_id")
    qa_show.set_defaults(func=cmd_qa_show)

    # issues ----------------------------------------------------------------
    issues = sub.add_parser("issues", help="Show indexing issues for a snapshot")
    issues.add_argument("--project")
    issues.add_argument("--repo")
    issues.add_argument("--snapshot")
    issues.add_argument("--kind", choices=[kind.value for kind in IssueKind])
    issues.add_argument("--severity", choices=[severity.value for severity in IssueSeverity])
    issues.add_argument("--limit", type=int, default=50)
    issues.set_defaults(func=cmd_issues)

    # events ----------------------------------------------------------------
    events = sub.add_parser("events", help="Show the durable workflow event log")
    events.add_argument("--project")
    events.add_argument("--snapshot")
    events.add_argument("--entity")
    events.add_argument("--type")
    events.add_argument("--limit", type=int, default=40)
    events.set_defaults(func=cmd_events)

    # config ----------------------------------------------------------------
    config = sub.add_parser("config", help="Inspect or change local configuration")
    config_sub = config.add_subparsers(dest="config_command", metavar="<action>")
    config_sub.add_parser("show", help="Show the effective configuration").set_defaults(
        func=cmd_config_show
    )
    config_sub.add_parser("defaults", help="Show built-in default configuration").set_defaults(
        func=cmd_config_defaults
    )
    c_set = config_sub.add_parser("set", help="Set a dotted key, e.g. model.provider=openai_compatible")
    c_set.add_argument("assignment")
    c_set.set_defaults(func=cmd_config_set)

    # model -----------------------------------------------------------------
    model = sub.add_parser("model", help="Model provider status")
    model_sub = model.add_subparsers(dest="model_command", metavar="<action>")
    model_sub.add_parser("status", help="Show provider, policy and callability").set_defaults(
        func=cmd_model_status
    )

    # schemas ---------------------------------------------------------------
    schemas = sub.add_parser("schemas", help="Export versioned JSON schemas")
    schemas_sub = schemas.add_subparsers(dest="schemas_command", metavar="<action>")
    schemas_export = schemas_sub.add_parser("export", help="Write schemas to a directory")
    schemas_export.add_argument("directory")
    schemas_export.set_defaults(func=cmd_schemas_export)

    # db --------------------------------------------------------------------
    db = sub.add_parser("db", help="Database inspection and migrations")
    db_sub = db.add_subparsers(dest="db_command", metavar="<action>")
    db_sub.add_parser("migrate", help="Apply pending schema migrations").set_defaults(
        func=cmd_db_migrate, no_migrate=True
    )
    db_sub.add_parser("info", help="Show database path, schema and row counts").set_defaults(
        func=cmd_db_info
    )

    # Phase 2/3 command areas are registered by their own modules so a missing
    # area is caught by a parser test rather than by a user (MWB-001).
    from mayaworkbench.cli.research_commands import register_research_commands
    from mayaworkbench.cli.task_commands import register_task_commands

    register_research_commands(sub)
    register_task_commands(sub)

    worker = sub.add_parser("worker", help="Run the durable task worker process")
    worker.add_argument("--once", action="store_true", help="Process one job and exit")
    worker.add_argument("--run-id", help="Claim a specific run's job")
    worker.set_defaults(func=cmd_worker)

    return parser


# ------------------------------------------------------------------- services
_SERVICES: Services | None = None


def services(args: argparse.Namespace) -> Services:
    global _SERVICES
    if _SERVICES is None:
        _SERVICES = Services.open(
            str(args.home) if args.home else None,
            migrate=not getattr(args, "no_migrate", False),
        )
    return _SERVICES


# ---------------------------------------------------------------------- utils
def snapshot_payload(snapshot: RepositorySnapshot) -> dict[str, Any]:
    return snapshot.model_dump(mode="json")


def resolve_target_snapshot(args: argparse.Namespace, svc: Services):  # type: ignore[no-untyped-def]
    return svc.resolve_snapshot_context(
        project_identifier=args.project,
        repository_identifier=getattr(args, "repo", None),
        snapshot_id=getattr(args, "snapshot", None),
    )


# ---------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    created_project = False
    if args.project:
        slug = args.slug or slugify(args.project)
        project = svc.projects.find(slug)
        if project is None:
            project = svc.create_project(args.project, args.description, slug)
            created_project = True
    else:
        projects = svc.projects.list()
        if projects:
            project = projects[0]
        else:
            default_name = Path.cwd().name or "project"
            project = svc.create_project(default_name, args.description)
            created_project = True

    payload = {
        "home": str(svc.paths.home),
        "database": str(svc.paths.db_path),
        "config": str(svc.paths.config_path),
        "artifacts": str(svc.paths.artifacts_dir),
        "project": {"id": project.id, "name": project.name, "slug": project.slug},
        "created_project": created_project,
        "schema_version": svc.db.schema_version(),
        "next_steps": [
            f"maya repo add <path> --project {project.slug}",
            "maya snapshot create",
            "maya index",
            "maya inspect",
            'maya ask "what does this repository do?"',
        ],
    }
    if console.json_mode:
        console.json(payload)
        return 0

    console.heading("MayaWorkbench initialised")
    console.kv(
        [
            ("data directory", str(svc.paths.home)),
            ("database", str(svc.paths.db_path)),
            ("artifacts", str(svc.paths.artifacts_dir)),
            ("schema version", svc.db.schema_version()),
            ("project", f"{project.name} ({project.slug})"),
            ("data policy", svc.settings.data_policy),
        ]
    )
    console.section("Next")
    for step in payload["next_steps"]:
        console.bullet(step)
    console.note(
        "\nRemote model calls are disabled by default (LOCAL_ONLY). "
        "See `maya config set data_policy=BYOK_REMOTE_MODEL` and docs/threat-model.md."
    )
    return 0


# -------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    console = args.console
    paths = AppPaths.resolve(args.home)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | str, detail: str) -> None:
        status = ok if isinstance(ok, str) else ("ok" if ok else "fail")
        checks.append({"check": name, "status": status, "detail": detail})

    add(
        "python",
        sys.version_info >= (3, 11),
        f"{platform.python_version()} ({'ok' if sys.version_info >= (3, 11) else 'needs >= 3.11'})",
    )
    git_path = shutil.which("git")
    if git_path:
        try:
            import subprocess

            version = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=10, check=False
            ).stdout.strip()
        except Exception as exc:  # pragma: no cover - defensive
            version = f"error: {exc}"
        add("git", True, version or git_path)
    else:
        add("git", False, "git executable not found on PATH")

    add("sqlite", f"{sqlite3.sqlite_version}", f"SQLite {sqlite3.sqlite_version}")
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        probe.close()
        add("fts5", True, "available in this SQLite build")
    except sqlite3.Error as exc:
        add("fts5", False, f"FTS5 unavailable: {exc}")

    try:
        paths.ensure_dirs()
        with tempfile.NamedTemporaryFile(dir=paths.home, prefix=".doctor-", delete=True) as handle:
            handle.write(b"ok")
        add("data directory", True, f"writable: {paths.home}")
    except OSError as exc:
        add("data directory", False, f"not writable: {exc}")

    config_ok = True
    config_detail = str(paths.config_path)
    try:
        settings = load_settings(paths)
        config_detail = f"{paths.config_path} ({settings.data_policy})"
    except MayaError as exc:
        config_ok = False
        config_detail = str(exc)
        settings = None
    add("configuration", config_ok, config_detail)

    schema_version = 0
    try:
        db = Database(paths.db_path)
        schema_version = db.schema_version()
        health = db.health()
        add(
            "database",
            "warn" if schema_version == 0 else True,
            f"{paths.db_path} (schema {schema_version}, journal {health.get('journal_mode')})",
        )
        counts = {}
        if schema_version:
            row = db.fetchone(
                "SELECT (SELECT COUNT(*) FROM projects) AS projects, "
                "(SELECT COUNT(*) FROM repository_snapshots) AS snapshots, "
                "(SELECT COUNT(*) FROM indexed_files) AS files"
            )
            counts = dict(row)
        db.close()
    except MayaError as exc:
        counts = {}
        add("database", False, str(exc))

    if settings is not None:
        from mayaworkbench.models.gateway import ModelGateway
        from mayaworkbench.storage.stores import EventStore, InvocationStore

        db2 = Database(paths.db_path)
        try:
            status = ModelGateway(
                settings, InvocationStore(db2), EventStore(db2)
            ).status()
            add(
                "model",
                "ok" if status.callable else "warn",
                f"{status.provider}/{status.model or '<unset>'}: "
                + ("callable" if status.callable else (status.reason or "unavailable")),
            )
        finally:
            db2.close()

    payload = {
        "home": str(paths.home),
        "checks": checks,
        "counts": counts,
        "ok": all(check["status"] != "fail" for check in checks),
    }
    if console.json_mode:
        console.json(payload)
        return 0 if payload["ok"] else 1

    console.heading("Environment check")
    console.table(
        ["check", "status", "detail"],
        [[check["check"], check["status"], check["detail"]] for check in checks],
        max_column=88,
    )
    if counts:
        console.section("Local data")
        console.kv([(key, value) for key, value in counts.items()])
    console.print("")
    console.print("status: " + ("ok" if payload["ok"] else "problems found"))
    return 0 if payload["ok"] else 1


# ------------------------------------------------------------------- project
def cmd_project_create(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.create_project(args.name, args.description, args.slug)
    if console.json_mode:
        console.json(project.model_dump(mode="json"))
        return 0
    console.heading(f"Created project {project.name}")
    console.kv(
        [
            ("id", project.id),
            ("slug", project.slug),
            ("description", project.description or "—"),
            ("created", project.created_at.isoformat()),
        ]
    )
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    projects = svc.projects.list()
    rows = []
    for project in projects:
        repositories = svc.repositories.list_by_project(project.id)
        snapshots = svc.snapshots.list_for_project(project.id, limit=1000)
        indexed = [snapshot for snapshot in snapshots if snapshot.indexed_at is not None]
        rows.append(
            {
                "slug": project.slug,
                "name": project.name,
                "id": project.id,
                "repositories": len(repositories),
                "snapshots": len(snapshots),
                "indexed": len(indexed),
                "created": project.created_at.isoformat(),
            }
        )
    if console.json_mode:
        console.json({"projects": rows})
        return 0
    console.heading("Projects")
    console.table(
        ["slug", "name", "repos", "snapshots", "indexed"],
        [
            [row["slug"], row["name"], row["repositories"], row["snapshots"], row["indexed"]]
            for row in rows
        ],
    )
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.identifier)
    repositories = svc.repositories.list_by_project(project.id)
    snapshots = svc.snapshots.list_for_project(project.id, limit=10)
    payload = {
        "project": project.model_dump(mode="json"),
        "repositories": [repo.model_dump(mode="json") for repo in repositories],
        "recent_snapshots": [snapshot_payload(snapshot) for snapshot in snapshots],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"{project.name} ({project.slug})")
    console.kv(
        [
            ("id", project.id),
            ("description", project.description or "—"),
            ("created", project.created_at.isoformat()),
        ]
    )
    console.section("Repositories")
    console.table(
        ["id", "name", "branch", "path"],
        [
            [repo.id, repo.name, repo.default_branch or "—", repo.root_path]
            for repo in repositories
        ],
        max_column=60,
    )
    console.section("Recent snapshots")
    console.table(
        ["id", "identity", "kind", "status", "indexed"],
        [
            [
                snapshot.id,
                snapshot.identity_label,
                snapshot.kind.value,
                snapshot.status.value,
                snapshot.indexed_at.isoformat() if snapshot.indexed_at else "—",
            ]
            for snapshot in snapshots
        ],
    )
    return 0


# ---------------------------------------------------------------------- repo
def cmd_repo_add(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    result = svc.repository_service.attach(project, args.path, args.name)
    for warning in result.warnings:
        console.warning(warning)
    payload = {
        "project": project.slug,
        "repository": result.repository.model_dump(mode="json"),
        "created": result.created,
        "warnings": result.warnings,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(
        f"{'Attached' if result.created else 'Already attached'}: {result.repository.name}"
    )
    console.kv(
        [
            ("id", result.repository.id),
            ("root", result.repository.root_path),
            ("remote", result.repository.remote_url or "—"),
            ("default branch", result.repository.default_branch or "—"),
        ]
    )
    console.print("")
    console.print("Next: maya snapshot create")
    return 0


def cmd_repo_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    repositories = svc.repositories.list_by_project(project.id)
    rows = [
        {
            "id": repo.id,
            "name": repo.name,
            "path": repo.root_path,
            "remote": repo.remote_url,
            "branch": repo.default_branch,
        }
        for repo in repositories
    ]
    if console.json_mode:
        console.json({"project": project.slug, "repositories": rows})
        return 0
    console.heading(f"Repositories in {project.slug}")
    console.table(
        ["id", "name", "branch", "path"],
        [[row["id"], row["name"], row["branch"] or "—", row["path"]] for row in rows],
        max_column=60,
    )
    return 0


# ------------------------------------------------------------------ snapshot
def cmd_snapshot_create(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    repository = svc.resolve_repository(project, args.repo)
    outcome = svc.repository_service.create_snapshot(
        repository,
        working_tree="include" if args.include_working_tree else "committed",
    )
    for warning in outcome.warnings:
        console.warning(warning)
    snapshot = outcome.snapshot
    payload = {
        "snapshot": snapshot_payload(snapshot),
        "repository": outcome.repository.model_dump(mode="json"),
        "reused": outcome.reused,
        "warnings": outcome.warnings,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Snapshot {snapshot.identity_label} ({'reused' if outcome.reused else 'pinned'})")
    console.kv(
        [
            ("id", snapshot.id),
            ("kind", snapshot.kind.value),
            ("commit", snapshot.commit_sha),
            ("tree", snapshot.tree_sha),
            ("branch", snapshot.branch or "—"),
            ("working tree dirty", "yes" if snapshot.dirty else "no"),
            ("patch included", "yes" if snapshot.includes_worktree_patch else "no"),
            ("patch hash", snapshot.patch_hash or "—"),
            ("changed paths", len(snapshot.included_paths)),
            ("status", snapshot.status.value),
        ]
    )
    console.print("")
    console.print(f"Next: maya index --snapshot {snapshot.id}")
    return 0


def cmd_snapshot_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    repository = svc.resolve_repository(project, args.repo) if args.repo else None
    snapshots = (
        svc.snapshots.list_by_repository(repository.id, limit=args.limit)
        if repository
        else svc.snapshots.list_for_project(project.id, limit=args.limit)
    )
    payload = {"snapshots": [snapshot_payload(snapshot) for snapshot in snapshots]}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Snapshots")
    console.table(
        ["id", "identity", "kind", "dirty", "status", "files", "created"],
        [
            [
                snapshot.id,
                snapshot.identity_label,
                snapshot.kind.value,
                "yes" if snapshot.dirty else "no",
                snapshot.status.value,
                snapshot.file_count,
                snapshot.created_at.date().isoformat(),
            ]
            for snapshot in snapshots
        ],
    )
    return 0


def cmd_snapshot_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    snapshot = svc.snapshots.get(args.snapshot_id)
    repository = svc.repositories.get(snapshot.repository_id)
    payload = {
        "snapshot": snapshot_payload(snapshot),
        "repository": repository.model_dump(mode="json"),
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Snapshot {snapshot.id}")
    console.kv(
        [
            ("identity", snapshot.identity_label),
            ("repository", f"{repository.name} ({repository.root_path})"),
            ("kind", snapshot.kind.value),
            ("status", snapshot.status.value),
            ("commit", snapshot.commit_sha or "—"),
            ("tree", snapshot.tree_sha or "—"),
            ("branch", snapshot.branch or "—"),
            ("base commit", snapshot.base_commit_sha or "—"),
            ("working tree dirty", "yes" if snapshot.dirty else "no"),
            ("includes patch", "yes" if snapshot.includes_worktree_patch else "no"),
            ("patch hash", snapshot.patch_hash or "—"),
            ("patch artifact", snapshot.patch_artifact_id or "—"),
            ("included paths", len(snapshot.included_paths)),
            ("indexer version", snapshot.indexer_version),
            ("indexed at", snapshot.indexed_at.isoformat() if snapshot.indexed_at else "—"),
            ("files", snapshot.file_count),
            ("symbols", snapshot.symbol_count),
            ("bytes indexed", snapshot.total_bytes),
            ("error", snapshot.error or "—"),
        ]
    )
    if snapshot.submodules:
        console.section("Submodules")
        for path, sha in snapshot.submodules.items():
            console.bullet(f"{path} @ {sha}")
    if snapshot.index_metadata:
        console.section("Index metadata")
        for key, value in sorted(snapshot.index_metadata.items()):
            if isinstance(value, dict):
                console.kv([(key, json.dumps(value, sort_keys=True))], indent=2)
            else:
                console.kv([(key, value)], indent=2)
    return 0


# --------------------------------------------------------------------- index
def cmd_index(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    _project, _repository, snapshot = svc.resolve_snapshot_context(
        project_identifier=args.project,
        repository_identifier=args.repo,
        snapshot_id=args.snapshot,
        require_indexed=False,
    )
    report = svc.indexer.index(snapshot.id)
    payload = report.model_dump(mode="json")
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Indexed snapshot {snapshot.identity_label}")
    console.kv(
        [
            ("snapshot", report.snapshot_id),
            ("files indexed", report.files_indexed),
            ("files skipped", report.files_skipped),
            ("lines indexed", report.lines_indexed),
            ("bytes indexed", report.bytes_indexed),
            ("symbols", report.symbols),
            ("edges", f"{report.edges} ({report.edges_resolved} resolved, {report.edges_unresolved} unresolved)"),
            ("issues", report.issues),
            ("duration", f"{report.duration_ms} ms"),
            ("manifest artifact", report.manifest_artifact_id or "—"),
        ]
    )
    if report.languages:
        console.section("Languages")
        console.table(
            ["language", "files"],
            sorted(report.languages.items(), key=lambda item: -item[1]),
        )
    if report.parse_coverage:
        console.section("Parser coverage")
        console.table(["coverage", "files"], sorted(report.parse_coverage.items()))
    for warning in report.warnings:
        console.warning(warning)
    console.print("")
    console.print("Next: maya inspect, or maya ask \"…\"")
    return 0


# ------------------------------------------------------------------- inspect
def _render_map(console: Any, repository_map: RepositoryMap) -> None:
    console.heading(f"Repository map — {repository_map.repository_name}")
    console.kv(
        [
            ("snapshot", repository_map.snapshot_id),
            ("revision", f"{repository_map.kind} {(repository_map.commit_sha or '')[:12]}"),
            ("dirty", "yes" if repository_map.dirty else "no"),
            ("patch hash", repository_map.patch_hash or "—"),
            ("files", repository_map.files),
            ("skipped", repository_map.files_skipped),
            ("lines", repository_map.lines_indexed),
            ("bytes", repository_map.bytes_indexed),
            ("symbols", repository_map.symbols),
            ("edges", f"{repository_map.edges} ({repository_map.edges_resolved} resolved)"),
            ("indexer", repository_map.indexer_version),
        ]
    )
    console.section("Languages")
    console.table(
        ["language", "files", "lines"],
        [[row.language, row.files, row.lines] for row in repository_map.languages],
    )
    if repository_map.directories:
        console.section("Largest directories")
        console.table(
            ["directory", "files", "lines"],
            [[row.directory, row.files, row.lines] for row in repository_map.directories[:10]],
            max_column=50,
        )
    if repository_map.entry_points:
        console.section("Entry points")
        console.table(
            ["path", "kind", "detail"],
            [[entry.path, entry.kind, entry.detail] for entry in repository_map.entry_points[:20]],
            max_column=60,
        )
    if repository_map.package_managers:
        console.section("Package / build systems")
        console.bullet(", ".join(repository_map.package_managers))
    if repository_map.public_symbols:
        console.section("Public symbols (top-level)")
        console.table(
            ["symbol", "kind", "location"],
            [
                [
                    symbol.qualified_name,
                    symbol.kind,
                    f"{symbol.path}:{symbol.start_line}",
                ]
                for symbol in repository_map.public_symbols[:20]
            ],
            max_column=55,
        )
    if repository_map.issues:
        console.section("Index issues")
        console.table(
            ["kind", "count"],
            sorted(repository_map.issues.items(), key=lambda item: -item[1]),
        )
    if repository_map.test_files:
        console.section(f"Tests ({len(repository_map.test_files)})")
        for path in repository_map.test_files[:12]:
            console.bullet(path)
        if len(repository_map.test_files) > 12:
            console.note(f"    … {len(repository_map.test_files) - 12} more")
    if repository_map.limitations:
        console.section("Limitations")
        for limitation in repository_map.limitations:
            console.bullet(limitation)
    console.section("Parser coverage")
    console.table(["coverage", "files"], sorted(repository_map.parse_coverage.items()))
    console.print("")
    console.print(
        f"AST languages: {', '.join(repository_map.ast_languages) or '—'} | "
        f"lexical-only languages: {', '.join(repository_map.lexical_only_languages) or '—'}"
    )


def cmd_inspect(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    _project, _repository, snapshot = resolve_target_snapshot(args, svc)
    repository_map = svc.maps.build(snapshot.id)
    artifact_id = None
    if args.save:
        artifact = svc.artifacts.put_text(
            repository_map.model_dump_json(indent=2),
            kind=ArtifactKind.INDEX_MAP,
            media_type="application/json",
            created_by="inspect",
        )
        artifact_id = artifact.id
    payload = {
        "map": repository_map.model_dump(mode="json"),
        "saved_artifact_id": artifact_id,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    _render_map(console, repository_map)
    if artifact_id:
        console.note(f"saved map artifact: {artifact_id}")
    return 0


# -------------------------------------------------------------------- search
def cmd_search(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    repository = svc.resolve_repository(project, args.repo)
    snapshot = svc.resolve_snapshot(
        project, snapshot_id=args.snapshot, repository_id=repository.id
    )

    if args.symbols:
        symbol_hits = svc.search.search_symbols(snapshot.id, args.query, limit=args.limit)
        payload = {
            "snapshot_id": snapshot.id,
            "query": args.query,
            "mode": "symbols",
            "symbols": [hit.model_dump(mode="json") for hit in symbol_hits],
        }
        if console.json_mode:
            console.json(payload)
            return 0
        console.heading(f"Symbol search: {args.query}")
        console.table(
            ["symbol", "kind", "location", "match"],
            [
                [
                    hit.qualified_name,
                    hit.kind,
                    f"{hit.path}:{hit.start_line}",
                    hit.match_reason,
                ]
                for hit in symbol_hits
            ],
            max_column=55,
        )
        if not symbol_hits:
            console.note("No symbols matched.")
        return 0

    hits, expression = svc.search.search_text(
        snapshot.id,
        args.query,
        mode=args.mode,
        limit=args.limit,
        path_prefix=args.path,
        case_sensitive=args.case_sensitive,
    )
    payload = {
        "snapshot_id": snapshot.id,
        "query": args.query,
        "mode": args.mode,
        "fts_expression": expression,
        "hits": [hit.model_dump(mode="json") for hit in hits],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Search: {args.query}")
    console.table(
        ["location", "match", "line"],
        [
            [f"{hit.path}:{hit.line_no}", hit.match_kind, hit.text.strip()]
            for hit in hits
        ],
        max_column=70,
    )
    if not hits:
        console.note("No matches. Try --mode exact or --mode regex.")
    elif expression:
        console.note(f"\nFTS expression used: {expression}")
    return 0


# -------------------------------------------------------------------- symbol
def cmd_symbol_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    repository = svc.resolve_repository(project, args.repo)
    snapshot = svc.resolve_snapshot(
        project, snapshot_id=args.snapshot, repository_id=repository.id
    )

    identifier = args.identifier
    detail = None
    if identifier.startswith("sym_"):
        detail = svc.search.get_symbol(identifier)
    else:
        matches = svc.symbols.find_by_qualified(snapshot.id, identifier)
        if not matches:
            candidates = svc.search.search_symbols(snapshot.id, identifier, limit=10)
            if len(candidates) == 1:
                detail = svc.search.get_symbol(candidates[0].symbol_id)
            elif candidates:
                payload = {"query": identifier, "candidates": [c.model_dump(mode="json") for c in candidates]}
                if console.json_mode:
                    console.json(payload)
                    return 0
                console.heading(f"Multiple symbols match {identifier!r}")
                console.table(
                    ["symbol", "kind", "location"],
                    [
                        [candidate.qualified_name, candidate.kind, f"{candidate.path}:{candidate.start_line}"]
                        for candidate in candidates
                    ],
                    max_column=55,
                )
                console.note("Pass a qualified name or symbol id to disambiguate.")
                return 0
            else:
                raise NotFoundError(
                    f"No symbol matches {identifier!r}",
                    hint="Use `maya search --symbols <query>` to explore names.",
                )
        else:
            detail = svc.search.get_symbol(matches[0].id)

    if console.json_mode:
        console.json(detail.model_dump(mode="json"))
        return 0
    console.heading(f"{detail.qualified_name} ({detail.kind})")
    console.kv(
        [
            ("location", f"{detail.path}:{detail.start_line}-{detail.end_line}"),
            ("signature", detail.signature),
        ]
    )
    if detail.docstring:
        console.print("")
        console.print(detail.docstring)
    console.section("Source")
    console.code(detail.source)
    if detail.truncated_source:
        console.note("(source truncated to 220 lines)")
    if detail.relationships:
        console.section("Relationships")
        console.table(
            ["direction", "type", "target", "location", "confidence"],
            [
                [
                    relationship.direction,
                    relationship.edge_type,
                    relationship.qualified_name or relationship.target_name,
                    f"{relationship.path}:{relationship.start_line}" if relationship.path else "—",
                    relationship.confidence,
                ]
                for relationship in detail.relationships[:40]
            ],
            max_column=50,
        )
    return 0


# ----------------------------------------------------------------------- ask
def cmd_ask(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    _project, _repository, snapshot = resolve_target_snapshot(args, svc)
    result = svc.ask.ask(
        snapshot.id,
        args.question,
        token_budget=args.budget,
        use_model=not args.no_model,
    )
    record = result.record
    payload = {
        "record": record.model_dump(mode="json"),
        "diagnostics": result.bundle.diagnostics.model_dump(mode="json"),
        "notes": result.notes,
        "model_used": result.model_used,
        "citations": [citation.model_dump(mode="json") for citation in record.citations],
        "context": {
            "token_estimate": record.context_token_estimate,
            "token_budget": record.token_budget,
            "files_covered": result.bundle.context.files_covered,
            "excluded": len(result.bundle.context.excluded),
            "truncated": result.bundle.context.truncated,
        },
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Answer ({record.status.value})")
    console.print(record.answer_markdown)
    if record.citations:
        console.section("Citations")
        console.table(
            ["location", "valid", "reason", "quote"],
            [
                [
                    f"{citation.path}:{citation.start_line}-{citation.end_line}",
                    "yes" if citation.valid else "no",
                    citation.reason or citation.validation,
                    citation.quote,
                ]
                for citation in record.citations
            ],
            max_column=48,
        )
        invalid = [citation for citation in record.citations if not citation.valid]
        if invalid:
            console.section("Rejected citations")
            for citation in invalid:
                console.bullet(f"{citation.path}:{citation.start_line}-{citation.end_line}: {citation.validation}")
    if record.uncertainties:
        console.section("Uncertainties")
        for uncertainty in record.uncertainties:
            console.bullet(uncertainty)
    if record.unsupported_claims:
        console.section("Unverified claims (model-flagged)")
        for claim in record.unsupported_claims:
            console.bullet(claim)
    if result.notes:
        console.section("Retrieval notes")
        for note in result.notes:
            console.bullet(note)
    console.section("Record")
    console.kv(
        [
            ("qa record", record.id),
            ("artifact", record.artifact_id or "—"),
            ("context tokens", f"{record.context_token_estimate}/{record.token_budget}"),
            ("model used", "yes" if result.model_used else "no"),
        ]
    )
    return 0


# ------------------------------------------------------------------------ qa
def cmd_qa_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    records = svc.qa_store.list(
        project_id=project.id, snapshot_id=args.snapshot, limit=args.limit
    )
    payload = {"records": [record.model_dump(mode="json") for record in records]}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Question/answer records")
    console.table(
        ["id", "status", "tokens", "citations", "question"],
        [
            [
                record.id,
                record.status.value,
                f"{record.context_token_estimate}/{record.token_budget}",
                len(record.citations),
                record.question,
            ]
            for record in records
        ],
        max_column=52,
    )
    return 0


def cmd_qa_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    record = svc.qa_store.get(args.record_id)
    payload = {
        "record": record.model_dump(mode="json"),
        "artifact": (
            svc.artifacts.read_text(record.artifact_id) if record.artifact_id else None
        ),
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"QA record {record.id} ({record.status.value})")
    console.kv(
        [
            ("snapshot", record.snapshot_id),
            ("question", record.question),
            ("created", record.created_at.isoformat()),
            ("context tokens", f"{record.context_token_estimate}/{record.token_budget}"),
            ("model invocation", record.model_invocation_id or "—"),
            ("artifact", record.artifact_id or "—"),
        ]
    )
    console.section("Answer")
    console.print(record.answer_markdown)
    if record.citations:
        console.section("Citations")
        for citation in record.citations:
            state = "valid" if citation.valid else "REJECTED"
            console.bullet(f"[{state}] {citation.path}:{citation.start_line}-{citation.end_line} {citation.validation}")
    return 0


# -------------------------------------------------------------------- issues
def cmd_issues(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    _project, _repository, snapshot = resolve_target_snapshot(args, svc)
    kind = IssueKind(args.kind) if args.kind else None
    severity = IssueSeverity(args.severity) if args.severity else None
    issues = svc.issues.list(snapshot.id, kind=kind, severity=severity, limit=args.limit)
    counts = svc.issues.counts_by_kind(snapshot.id)
    payload = {
        "snapshot_id": snapshot.id,
        "counts": counts,
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Index issues for {snapshot.identity_label}")
    if counts:
        console.table(["kind", "count"], sorted(counts.items(), key=lambda item: -item[1]))
    console.section("Details")
    console.table(
        ["severity", "kind", "path", "detail"],
        [
            [issue.severity.value, issue.kind.value, issue.path or "—", issue.detail]
            for issue in issues
        ],
        max_column=55,
    )
    return 0


# -------------------------------------------------------------------- events
def cmd_events(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    project = svc.resolve_project(args.project)
    events = svc.events.list(
        project_id=project.id,
        snapshot_id=args.snapshot,
        entity_id=args.entity,
        event_type=args.type,
        limit=args.limit,
    )
    payload = {"events": [event.model_dump(mode="json") for event in events]}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Workflow events")
    console.table(
        ["time", "type", "entity", "actor", "payload"],
        [
            [
                event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
                event.event_type.value,
                event.entity_id,
                f"{event.actor_kind.value}:{event.actor_name}",
                json.dumps(event.payload, sort_keys=True),
            ]
            for event in events
        ],
        max_column=55,
    )
    return 0


# -------------------------------------------------------------------- config
def cmd_config_show(args: argparse.Namespace) -> int:
    console = args.console
    paths = AppPaths.resolve(args.home)
    settings = load_settings(paths)
    payload = redacted_settings_view(paths, settings)
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Configuration")
    console.print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_config_defaults(args: argparse.Namespace) -> int:
    console = args.console
    payload = Settings().model_dump(mode="json")
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Default configuration")
    console.print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


SECRET_KEY_FRAGMENTS = ("api_key", "secret", "token", "password")
FORBIDDEN_CONFIG_KEYS = {"schema_version"}


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise UsageError(f"Expected a boolean, got {raw!r}")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise UsageError(f"Expected an integer, got {raw!r}") from exc
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise UsageError(f"Expected a number, got {raw!r}") from exc
    if isinstance(current, list):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in raw.split(",") if item.strip()]
    if current is None:
        if raw.strip().lower() in ("null", "none", ""):
            return None
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def cmd_config_set(args: argparse.Namespace) -> int:
    console = args.console
    paths = AppPaths.resolve(args.home)
    settings = load_settings(paths)
    if "=" not in args.assignment:
        raise UsageError("Expected key=value", hint="Example: model.provider=openai_compatible")
    key, raw = args.assignment.split("=", 1)
    key = key.strip()
    if key in FORBIDDEN_CONFIG_KEYS:
        raise UsageError(f"{key} is managed by the application and cannot be set")
    parts = key.split(".")
    if parts[0] not in {"data_policy", "model", "retrieval", "indexing"}:
        raise UsageError(f"Unknown configuration section {parts[0]!r}")
    # Credentials belong to the model section; other sections legitimately have
    # keys like retrieval.default_token_budget that must stay settable.
    if (
        parts[0] == "model"
        and key != "model.api_key_env"
        and any(fragment in key.lower() for fragment in SECRET_KEY_FRAGMENTS)
    ):
        raise UsageError(
            "Secrets are never stored in configuration",
            hint="Put the key in an environment variable and set model.api_key_env instead.",
        )
    data = settings.model_dump(mode="json")
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise UsageError(f"Unknown configuration key {key!r}")
        node = node[part]
    leaf = parts[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise UsageError(f"Unknown configuration key {key!r}")
    node[leaf] = _coerce(node[leaf], raw)
    try:
        updated = Settings.model_validate(data)
    except Exception as exc:
        raise UsageError(f"Invalid value for {key}: {exc}") from exc
    save_settings(paths, updated)
    payload = redacted_settings_view(paths, updated)
    if console.json_mode:
        console.json({"updated": key, "value": node[leaf], "settings": payload})
        return 0
    console.heading(f"Updated {key}")
    console.kv([("value", json.dumps(node[leaf]))])
    if key == "data_policy" and updated.data_policy == "BYOK_REMOTE_MODEL":
        console.warning(
            "Remote model calls are now permitted. Only the context for a request is sent, "
            "to the provider you configure. See docs/threat-model.md."
        )
    return 0


def cmd_model_status(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    status = svc.gateway.status()
    payload = status.model_dump(mode="json")
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Model provider")
    console.kv(
        [
            ("provider", status.provider),
            ("model", status.model or "—"),
            ("data policy", status.data_policy),
            ("api key present", "yes" if status.key_present else "no"),
            ("callable", "yes" if status.callable else f"no ({status.reason})"),
            ("max output tokens", status.max_output_tokens),
            ("timeout", f"{status.timeout_seconds}s"),
        ]
    )
    if not status.callable:
        console.note(
            "\nTo enable answers: set MAYA_DATA_POLICY=BYOK_REMOTE_MODEL, "
            "configure provider/model/base_url, and export the API key environment variable."
        )
    return 0


# ------------------------------------------------------------------- schemas
def cmd_schemas_export(args: argparse.Namespace) -> int:
    console = args.console
    directory = Path(args.directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    from mayaworkbench.domain import models as domain_models
    from mayaworkbench.indexing.indexer import IndexReport
    from mayaworkbench.indexing.map import RepositoryMap
    from mayaworkbench.qa.service import ModelCitation, QaAnswerPayload
    from mayaworkbench.retrieval.models import ContextPack

    schema_models = {
        "Project": domain_models.Project,
        "Repository": domain_models.Repository,
        "RepositorySnapshot": domain_models.RepositorySnapshot,
        "IndexedFile": domain_models.IndexedFile,
        "Symbol": domain_models.Symbol,
        "SymbolEdge": domain_models.SymbolEdge,
        "IndexIssue": domain_models.IndexIssue,
        "Artifact": domain_models.Artifact,
        "WorkflowEvent": domain_models.WorkflowEvent,
        "ModelInvocation": domain_models.ModelInvocation,
        "Citation": domain_models.Citation,
        "QaRecord": domain_models.QaRecord,
        "RepositoryMap": RepositoryMap,
        "IndexReport": IndexReport,
        "ContextPack": ContextPack,
        "QaAnswerPayload": QaAnswerPayload,
        "ModelCitation": ModelCitation,
    }
    import hashlib

    entries = {}
    for name, model_class in schema_models.items():
        schema = model_class.model_json_schema()
        filename = f"{name}.schema.json"
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        path = directory / filename
        path.write_text(text, encoding="utf-8")
        entries[name] = {
            "file": filename,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    manifest = {
        "bundle_version": SCHEMA_BUNDLE_VERSION,
        "maya_version": MAYA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utcnow().isoformat(),
        "models": entries,
    }
    (directory / "schemas.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    payload = {"directory": str(directory), "manifest": manifest}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Exported {len(entries)} schemas")
    console.kv([("directory", str(directory)), ("bundle version", SCHEMA_BUNDLE_VERSION)])
    return 0


# ------------------------------------------------------------------------ db
def cmd_db_migrate(args: argparse.Namespace) -> int:
    console = args.console
    paths = AppPaths.resolve(args.home)
    paths.ensure_dirs()
    db = Database(paths.db_path)
    try:
        results = db.migrate()
        payload = {
            "database": str(paths.db_path),
            "schema_version": db.schema_version(),
            "migrations": [
                {"version": result.version, "name": result.name, "applied": result.applied}
                for result in results
            ],
        }
    finally:
        db.close()
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Database migrations")
    console.table(
        ["version", "name", "applied"],
        [[row["version"], row["name"], "yes" if row["applied"] else "already"] for row in payload["migrations"]],
    )
    console.note(f"schema version: {payload['schema_version']}")
    return 0


def cmd_db_info(args: argparse.Namespace) -> int:
    console = args.console
    svc = services(args)
    health = svc.db.health()
    counts = {}
    for table in (
        "projects",
        "repositories",
        "repository_snapshots",
        "indexed_files",
        "file_lines",
        "symbols",
        "symbol_edges",
        "import_aliases",
        "index_issues",
        "artifacts",
        "workflow_events",
        "model_invocations",
        "qa_records",
    ):
        counts[table] = int(svc.db.fetchone(f"SELECT COUNT(*) FROM {table}")[0])
    size_bytes = svc.paths.db_path.stat().st_size if svc.paths.db_path.exists() else 0
    payload = {
        "health": health,
        "schema_version": svc.db.schema_version(),
        "size_bytes": size_bytes,
        "counts": counts,
        "migrations": [
            {
                "version": row["version"],
                "name": row["name"],
                "applied_at": row["applied_at"],
            }
            for row in svc.db.applied_migrations()
        ],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Database")
    console.kv(
        [
            ("path", health.get("path")),
            ("schema version", payload["schema_version"]),
            ("journal mode", health.get("journal_mode")),
            ("size", f"{size_bytes / 1024:.1f} KiB"),
            ("SQLite", health.get("sqlite_version")),
        ]
    )
    console.section("Row counts")
    console.table(["table", "rows"], sorted(counts.items()))
    return 0


# -------------------------------------------------------------------- worker
def cmd_worker(args: argparse.Namespace) -> int:
    from mayaworkbench.worker import Worker

    console = args.console
    worker = Worker(services(args))
    exit_code = worker.run(once=args.once, run_id=args.run_id)
    if console.json_mode:
        console.json({"worker": {"exit_code": exit_code, "run_id": args.run_id}})
    return exit_code


# --------------------------------------------------------------------- entry
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    from mayaworkbench.cli.output import Console

    args.console = Console(
        json_mode=args.json,
        color=False if args.no_color else None,
    )
    paths = AppPaths.resolve(args.home)
    configure_logging(paths.logs_dir, verbose=args.verbose, quiet=args.quiet)

    try:
        return int(args.func(args) or 0)
    except MayaError as exc:
        args.console.error(str(exc), hint=exc.hint, code=exc.code)
        if args.verbose and exc.detail:
            print(exc.detail, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("maya: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level boundary
        args.console.error(f"unexpected error: {type(exc).__name__}: {exc}")
        if args.verbose:
            traceback.print_exc()
        return 1
    finally:
        global _SERVICES
        if _SERVICES is not None:
            _SERVICES.close()
            _SERVICES = None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

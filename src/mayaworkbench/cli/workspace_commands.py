"""CLI commands for the Engineering Workspace (WP4)."""

from __future__ import annotations

import argparse

from mayaworkbench.domain.enums import ResourceKind


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def register_workspace_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    workspace = sub.add_parser(
        "workspace", help="Manage durable multi-resource engineering workspaces"
    )
    subcommands = workspace.add_subparsers(dest="workspace_command", metavar="<action>")

    create = subcommands.add_parser("create", help="Create a workspace in a project")
    create.add_argument("--project")
    create.add_argument("--slug", required=True)
    create.add_argument("--title", default="")
    create.set_defaults(func=cmd_workspace_create)

    subcommands.add_parser("list", help="List workspaces").set_defaults(
        func=cmd_workspace_list
    )

    show = subcommands.add_parser("show", help="Show one workspace")
    show.add_argument("workspace_id")
    show.set_defaults(func=cmd_workspace_show)

    graph = subcommands.add_parser("graph", help="Show all attached resources and environments")
    graph.add_argument("workspace_id")
    graph.set_defaults(func=cmd_workspace_graph)

    attach = subcommands.add_parser("attach", help="Attach a typed resource")
    attach.add_argument("workspace_id")
    attach.add_argument("--kind", choices=[item.value for item in ResourceKind], required=True)
    attach.add_argument("--name", required=True)
    attach.add_argument("--ref", required=True, help="Repository id or scoped resource reference")
    attach.add_argument("--revision")
    attach.set_defaults(func=cmd_workspace_attach)

    environment = subcommands.add_parser("environment", help="Add an environment spec")
    environment.add_argument("workspace_id")
    environment.add_argument("--name", required=True)
    environment.add_argument("--ecosystem", required=True, help="python, node, rust, go, ...")
    environment.add_argument("--descriptor")
    environment.add_argument("--lockfile")
    environment.add_argument("--runtime", default="LOCAL_TRUSTED")
    environment.set_defaults(func=cmd_workspace_environment)


def cmd_workspace_create(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    workspace = svc.workspace_service.create(project.id, args.slug, title=args.title)
    if console.json_mode:
        console.json({"workspace": workspace.model_dump(mode="json")})
        return 0
    console.print(f"Created workspace {workspace.slug} ({workspace.id})")
    return 0


def cmd_workspace_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    workspaces = svc.workspace_service.list()
    if console.json_mode:
        console.json({"workspaces": [item.model_dump(mode="json") for item in workspaces]})
        return 0
    console.table(
        ["id", "slug", "project", "title"],
        [(item.id, item.slug, item.project_id, item.title) for item in workspaces],
    )
    return 0


def cmd_workspace_show(args: argparse.Namespace) -> int:
    console = args.console
    workspace = _services(args).workspace_service.get(args.workspace_id)
    payload = workspace.model_dump(mode="json")
    if console.json_mode:
        console.json({"workspace": payload})
        return 0
    console.print(f"{workspace.slug}: {workspace.title or '(untitled)'}")
    console.print(f"  id: {workspace.id}")
    console.print(f"  project: {workspace.project_id}")
    return 0


def cmd_workspace_attach(args: argparse.Namespace) -> int:
    console = args.console
    attachment = _services(args).workspace_service.attach_resource(
        args.workspace_id, args.kind, args.name, args.ref, revision=args.revision
    )
    if console.json_mode:
        console.json({"resource": attachment.model_dump(mode="json")})
        return 0
    console.print(f"Attached {attachment.kind.value} {attachment.name} ({attachment.id})")
    return 0


def cmd_workspace_environment(args: argparse.Namespace) -> int:
    console = args.console
    spec = _services(args).workspace_service.add_environment(
        args.workspace_id,
        args.name,
        args.ecosystem,
        descriptor_path=args.descriptor,
        lockfile_path=args.lockfile,
        runtime=args.runtime,
    )
    if console.json_mode:
        console.json({"environment": spec.model_dump(mode="json")})
        return 0
    console.print(f"Environment {spec.name} ({spec.ecosystem}) recorded: {spec.hash_sha256[:12]}")
    return 0


def cmd_workspace_graph(args: argparse.Namespace) -> int:
    console = args.console
    graph = _services(args).workspace_service.graph(args.workspace_id)
    if console.json_mode:
        console.json(graph)
        return 0
    console.heading(f"Workspace {graph['workspace']['slug']}")
    console.section("Resources")
    console.table(
        ["id", "kind", "name", "ref"],
        [(row["id"], row["kind"], row["name"], row["ref"]) for row in graph["resources"]],
    )
    console.section("Environments")
    console.table(
        ["name", "ecosystem", "runtime", "hash"],
        [
            (row["name"], row["ecosystem"], row["runtime"], row["hash_sha256"][:12])
            for row in graph["environments"]
        ],
    )
    console.section("Evidence")
    console.table(
        ["id", "status", "claim"],
        [(row["id"], row["status"], row["claim"][:70]) for row in graph["evidence"]],
    )
    return 0


__all__ = ["register_workspace_commands"]

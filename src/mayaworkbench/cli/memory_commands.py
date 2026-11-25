"""CLI commands for durable project memory (Phase 5, §15).

Memory is deliberately user-governed: model-authored items start UNCONFIRMED,
only an explicit confirmation promotes one, and commit-scoped items go STALE when
the repository moves. The CLI never silently confirms anything.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mayaworkbench.domain.enums import MemoryAuthor, MemoryKind, MemoryStatus
from mayaworkbench.domain.models import MemorySource
from mayaworkbench.errors import UsageError


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def _parse_sources(values: list[str]) -> list[MemorySource]:
    sources: list[MemorySource] = []
    for value in values:
        if ":" not in value:
            raise UsageError(
                f"--source expects KIND:REF, got {value!r}",
                hint="Example: --source EXPERIMENT:exp_01H... or --source USER_NOTE:chat",
            )
        kind, ref = value.split(":", 1)
        kind = kind.strip().upper()
        ref = ref.strip()
        if not kind or not ref:
            raise UsageError(f"--source has an empty kind or ref: {value!r}")
        sources.append(MemorySource(kind=kind, ref=ref))
    return sources


def register_memory_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    memory = sub.add_parser(
        "memory", help="Record, confirm and export durable project knowledge"
    )
    memory_sub = memory.add_subparsers(dest="memory_command", metavar="<action>")

    add = memory_sub.add_parser("add", help="Record one memory item")
    add.add_argument("--project")
    add.add_argument("--kind", choices=[item.value for item in MemoryKind], required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--statement", required=True)
    add.add_argument(
        "--author", choices=[item.value for item in MemoryAuthor], default="USER"
    )
    add.add_argument("--scope", default="project")
    add.add_argument("--repo")
    add.add_argument("--snapshot")
    add.add_argument("--commit", help="Full 40-character commit SHA")
    add.add_argument(
        "--confidence", choices=["LOW", "MEDIUM", "HIGH"], default="MEDIUM"
    )
    add.add_argument(
        "--source",
        action="append",
        default=[],
        help="Typed provenance: KIND:REF (TASK, RUN, EXPERIMENT, EXPERIMENT_COMPARISON, ARTIFACT, DOCUMENT, CLAIM, SNAPSHOT, USER_NOTE)",
    )
    add.add_argument("--supersedes", help="Memory id this item replaces")
    add.set_defaults(func=cmd_memory_add)

    listing = memory_sub.add_parser("list", help="List project memory")
    listing.add_argument("--project")
    listing.add_argument("--kind", choices=[item.value for item in MemoryKind])
    listing.add_argument("--status", choices=[item.value for item in MemoryStatus])
    listing.add_argument("--limit", type=int, default=100)
    listing.set_defaults(func=cmd_memory_list)

    show = memory_sub.add_parser("show", help="Show one memory item and its provenance")
    show.add_argument("memory_id")
    show.set_defaults(func=cmd_memory_show)

    confirm = memory_sub.add_parser("confirm", help="Confirm an UNCONFIRMED memory")
    confirm.add_argument("memory_id")
    confirm.add_argument("--note", default="")
    confirm.set_defaults(func=cmd_memory_confirm)

    retract = memory_sub.add_parser("retract", help="Retract a memory with a reason")
    retract.add_argument("memory_id")
    retract.add_argument("--reason", required=True)
    retract.set_defaults(func=cmd_memory_retract)

    delete = memory_sub.add_parser("delete", help="Delete a memory item permanently")
    delete.add_argument("memory_id")
    delete.add_argument("--yes", action="store_true", help="Required confirmation")
    delete.set_defaults(func=cmd_memory_delete)

    stale = memory_sub.add_parser(
        "stale", help="Mark commit-scoped memories stale after the repository moved"
    )
    stale.add_argument("--project")
    stale.add_argument("--repo")
    stale.set_defaults(func=cmd_memory_stale)

    export = memory_sub.add_parser("export", help="Export project memory as JSON and Markdown")
    export.add_argument("directory")
    export.add_argument("--project")
    export.set_defaults(func=cmd_memory_export)

    audit = memory_sub.add_parser("audit", help="Report conflicting confirmed memories")
    audit.add_argument("--project")
    audit.set_defaults(func=cmd_memory_audit)
    reconcile = memory_sub.add_parser(
        "reconcile", help="Advisory plan for near-duplicate and conflicting memories"
    )
    reconcile.add_argument("--project")
    reconcile.add_argument("--threshold", type=float, default=0.78)
    reconcile.set_defaults(func=cmd_memory_reconcile)
    retention = memory_sub.add_parser("retention", help="Retract memories past their window")
    retention.add_argument("--project")
    retention.add_argument("--max-age-days", type=int, default=365)
    retention.set_defaults(func=cmd_memory_retention)
    bundle_export = memory_sub.add_parser("export-bundle", help="Export memories as JSON")
    bundle_export.add_argument("--project")
    bundle_export.add_argument("path")
    bundle_export.set_defaults(func=cmd_memory_export_bundle)
    bundle_import = memory_sub.add_parser("import-bundle", help="Import memories as unconfirmed")
    bundle_import.add_argument("--project")
    bundle_import.add_argument("path")
    bundle_import.add_argument("--allow-conflicts", action="store_true")
    bundle_import.set_defaults(func=cmd_memory_import_bundle)


def cmd_memory_audit(args: argparse.Namespace) -> int:
    from mayaworkbench.memory.advanced import find_conflicts

    console = args.console
    project = _services(args).resolve_project(args.project)
    conflicts = find_conflicts(_services(args).memory_service, project.id)
    payload = {"conflicts": conflicts}
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0


def cmd_memory_reconcile(args: argparse.Namespace) -> int:
    from mayaworkbench.memory.advanced import reconcile

    console = args.console
    services = _services(args)
    project = services.resolve_project(args.project)
    payload = reconcile(
        services.memory_service, project.id, duplicate_threshold=args.threshold
    )
    console.json(payload) if console.json_mode else console.print(
        json.dumps(payload, indent=2)
    )
    return 0


def cmd_memory_retention(args: argparse.Namespace) -> int:
    from mayaworkbench.memory.advanced import apply_retention

    console = args.console
    project = _services(args).resolve_project(args.project)
    result = apply_retention(
        _services(args).memory_service, project.id, max_age_days=args.max_age_days
    )
    payload = {"retracted": result}
    console.json(payload) if console.json_mode else console.print(
        f"Retracted {len(result)} memory item(s)"
    )
    return 0


def cmd_memory_export_bundle(args: argparse.Namespace) -> int:
    from mayaworkbench.memory.advanced import export_bundle

    console = args.console
    project = _services(args).resolve_project(args.project)
    payload = export_bundle(_services(args).memory_service, project.id)
    Path(args.path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if not console.json_mode:
        console.print(f"Exported {len(payload['memories'])} memory item(s) to {args.path}")
    return 0


def cmd_memory_import_bundle(args: argparse.Namespace) -> int:
    from mayaworkbench.memory.advanced import import_bundle

    console = args.console
    project = _services(args).resolve_project(args.project)
    payload = import_bundle(
        _services(args).memory_service,
        project.id,
        json.loads(Path(args.path).read_text(encoding="utf-8")),
        allow_conflicts=bool(args.allow_conflicts),
    )
    console.json(payload) if console.json_mode else console.print(
        f"Imported {len(payload['created'])}, skipped {len(payload['skipped'])}, "
        f"conflicts {len(payload['conflicts'])}"
    )
    return 0


def cmd_memory_add(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    repository = svc.resolve_repository(project, args.repo) if args.repo else None
    memory = svc.memory_service.add(
        project_id=project.id,
        kind=MemoryKind(args.kind),
        title=args.title,
        statement=args.statement,
        author_kind=MemoryAuthor(args.author),
        scope=args.scope,
        repository_id=repository.id if repository else None,
        snapshot_id=args.snapshot,
        commit_sha=args.commit,
        confidence=args.confidence,
        sources=_parse_sources(args.source),
        supersedes_id=args.supersedes,
    )
    if console.json_mode:
        console.json(memory.model_dump(mode="json"))
        return 0
    console.heading(f"Recorded memory {memory.id}")
    console.kv(
        [
            ("kind", memory.kind.value),
            ("status", memory.status.value),
            ("author", memory.author_kind.value),
            ("title", memory.title),
            ("scope", memory.scope),
            ("commit", memory.commit_sha or "—"),
            ("sources", len(memory.sources)),
        ]
    )
    if memory.status == MemoryStatus.UNCONFIRMED:
        console.note(
            f"\nThis item is UNCONFIRMED. Confirm it deliberately: "
            f"maya memory confirm {memory.id}"
        )
    return 0


def cmd_memory_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    memories = svc.memory_service.list(
        project.id, kind=args.kind, status=args.status, limit=args.limit
    )
    rows = [
        {
            "id": item.id,
            "kind": item.kind.value,
            "status": item.status.value,
            "author": item.author_kind.value,
            "confidence": item.confidence,
            "title": item.title,
            "commit": item.commit_sha,
        }
        for item in memories
    ]
    if console.json_mode:
        console.json({"project": project.slug, "memories": rows})
        return 0
    console.heading(f"Project memory — {project.name}")
    if not rows:
        console.note("No memory records match.")
        return 0
    console.table(
        ["id", "kind", "status", "author", "conf", "title"],
        [
            [
                row["id"],
                row["kind"],
                row["status"],
                row["author"],
                row["confidence"],
                row["title"],
            ]
            for row in rows
        ],
        max_column=45,
    )
    return 0


def cmd_memory_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    memory = svc.memory_service.get(args.memory_id)
    payload = memory.model_dump(mode="json")
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"{memory.title} ({memory.kind.value})")
    console.kv(
        [
            ("id", memory.id),
            ("status", memory.status.value),
            ("author", memory.author_kind.value),
            ("confidence", memory.confidence),
            ("scope", memory.scope),
            ("repository", memory.repository_id or "—"),
            ("snapshot", memory.snapshot_id or "—"),
            ("commit", memory.commit_sha or "—"),
            ("supersedes", memory.supersedes_id or "—"),
            ("created", memory.created_at.isoformat()),
            ("updated", memory.updated_at.isoformat()),
            ("last validated", memory.last_validated_at.isoformat() if memory.last_validated_at else "—"),
        ]
    )
    console.section("Statement")
    console.print(memory.statement)
    if memory.sources:
        console.section("Provenance")
        console.table(
            ["kind", "ref", "sha256"],
            [
                [source.kind, source.ref, source.sha256 or "—"]
                for source in memory.sources
            ],
            max_column=55,
        )
    return 0


def cmd_memory_confirm(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    memory = svc.memory_service.confirm(args.memory_id, note=args.note)
    if console.json_mode:
        console.json(memory.model_dump(mode="json"))
        return 0
    console.heading(f"Confirmed {memory.id}")
    console.kv(
        [
            ("status", memory.status.value),
            ("title", memory.title),
            ("note", args.note or "—"),
        ]
    )
    return 0


def cmd_memory_retract(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    memory = svc.memory_service.retract(args.memory_id, reason=args.reason)
    if console.json_mode:
        console.json(memory.model_dump(mode="json"))
        return 0
    console.heading(f"Retracted {memory.id}")
    console.kv([("status", memory.status.value), ("reason", args.reason)])
    return 0


def cmd_memory_delete(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    if not args.yes:
        raise UsageError(
            "memory delete is permanent",
            hint="Re-run with --yes after confirming the item id.",
        )
    svc.memory_service.delete(args.memory_id)
    if console.json_mode:
        console.json({"deleted": args.memory_id})
        return 0
    console.heading(f"Deleted {args.memory_id}")
    return 0


def cmd_memory_stale(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    if not args.repo:
        raise UsageError(
            "memory stale needs --repo",
            hint="Only commit-scoped items for a named repository can go stale.",
        )
    repository = svc.resolve_repository(project, args.repo)
    ids = svc.memory_service.mark_stale(
        project_id=project.id, repository_id=repository.id
    )
    if console.json_mode:
        console.json({"stale": ids})
        return 0
    console.heading(f"Marked {len(ids)} memory item(s) stale")
    for memory_id in ids:
        console.bullet(memory_id)
    if not ids:
        console.note("No confirmed commit-scoped item described an older commit.")
    return 0


def cmd_memory_export(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    paths = svc.memory_service.export(project.id, args.directory)
    if console.json_mode:
        console.json({"project": project.slug, "files": paths})
        return 0
    console.heading("Exported project memory")
    console.kv([("json", paths["json"]), ("markdown", paths["markdown"])])
    return 0


__all__ = ["register_memory_commands"]

"""CLI commands for persistent skills."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _service(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services
    from mayaworkbench.skills.service import SkillsService

    return SkillsService(services(args).db)


def _ctx(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def register_skill_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    skill = sub.add_parser("skill", help="Manage reusable, versioned skills")
    subcommands = skill.add_subparsers(dest="skill_command", metavar="<action>")

    listing = subcommands.add_parser("list", help="List skills")
    listing.add_argument("--project")
    listing.add_argument("--enabled", action="store_true")
    listing.add_argument("--category")
    listing.add_argument("--limit", type=int, default=100)
    listing.set_defaults(func=cmd_skill_list)

    show = subcommands.add_parser("show", help="Show a skill")
    show.add_argument("skill_id")
    show.set_defaults(func=cmd_skill_show)

    add = subcommands.add_parser("add", help="Add a new version of a skill")
    add.add_argument("--project")
    add.add_argument("--key", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--category", default="procedure")
    add.add_argument("--title", default="")
    add.add_argument("--description", default="")
    add.add_argument("--body", help="Skill body text or path to a file")
    add.add_argument("--disable", action="store_true")
    add.set_defaults(func=cmd_skill_add)

    toggle = subcommands.add_parser("enable", help="Enable a skill")
    toggle.add_argument("skill_id")
    toggle.set_defaults(func=lambda args: cmd_skill_toggle(args, True))
    disable = subcommands.add_parser("disable", help="Disable a skill")
    disable.add_argument("skill_id")
    disable.set_defaults(func=lambda args: cmd_skill_toggle(args, False))

    promote = subcommands.add_parser("promote", help="Show the newest enabled version")
    promote.add_argument("--project")
    promote.add_argument("key")
    promote.set_defaults(func=cmd_skill_promote)

    export = subcommands.add_parser("export", help="Export skills as JSON")
    export.add_argument("--project")
    export.add_argument("--key")
    export.add_argument("--out")
    export.set_defaults(func=cmd_skill_export)

    import_cmd = subcommands.add_parser("import", help="Import skills from JSON")
    import_cmd.add_argument("--project")
    import_cmd.add_argument("path")
    import_cmd.add_argument("--allow-conflicts", action="store_true")
    import_cmd.set_defaults(func=cmd_skill_import)

    seed = subcommands.add_parser("seed", help="Install built-in skill seeds")
    seed.add_argument("--project")
    seed.set_defaults(func=cmd_skill_seed)

    run = subcommands.add_parser("run", help="Render or validate a skill without side effects")
    run.add_argument("skill_id")
    run.add_argument("--context", action="append", default=[], help="context k=v, repeatable")
    run.set_defaults(func=cmd_skill_run)


def cmd_skill_list(args: argparse.Namespace) -> int:
    console = args.console
    project = _ctx(args).resolve_project(args.project)
    skills = _service(args).list(
        project.id,
        enabled=True if args.enabled else None,
        category=args.category,
        limit=args.limit,
    )
    payload = {"skills": [skill.model_dump(mode="json") for skill in skills]}
    if console.json_mode:
        console.json(payload)
    else:
        console.table(
            ["id", "key", "version", "category", "enabled", "title"],
            [
                (s.id, s.effective_key, s.version, s.category, s.enabled, s.title)
                for s in skills
            ],
        )
    return 0


def cmd_skill_show(args: argparse.Namespace) -> int:
    console = args.console
    skill = _service(args).get(args.skill_id)
    payload = skill.model_dump(mode="json")
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0


def cmd_skill_add(args: argparse.Namespace) -> int:
    console = args.console
    project = _ctx(args).resolve_project(args.project)
    body = args.body or ""
    if body and Path(body).is_file():
        body = Path(body).read_text(encoding="utf-8")
    skill = _service(args).upsert(
        project.id, args.key, args.name, category=args.category,
        title=args.title, description=args.description, body=body,
        enabled=not args.disable,
    )
    if console.json_mode:
        console.json({"skill": skill.model_dump(mode="json")})
    else:
        console.print(f"Created {skill.effective_key} ({skill.id})")
    return 0


def cmd_skill_toggle(args: argparse.Namespace, enabled: bool) -> int:
    console = args.console
    skill = _service(args).set_enabled(args.skill_id, enabled)
    payload = {"skill": skill.model_dump(mode="json")}
    console.json(payload) if console.json_mode else console.print(
        f"{skill.effective_key} {'enabled' if skill.enabled else 'disabled'}"
    )
    return 0


def cmd_skill_promote(args: argparse.Namespace) -> int:
    console = args.console
    project = _ctx(args).resolve_project(args.project)
    skill = _service(args).promote(project.id, args.key)
    payload = {"skill": skill.model_dump(mode="json")}
    console.json(payload) if console.json_mode else console.print(skill.effective_key)
    return 0


def cmd_skill_export(args: argparse.Namespace) -> int:
    console = args.console
    project = _ctx(args).resolve_project(args.project)
    bundle = _service(args).export_bundle(project.id, key=args.key)
    text = json.dumps(bundle, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        if not console.json_mode:
            console.print(f"Exported {len(bundle)} skill(s) to {args.out}")
    else:
        console.print(text)
    return 0


def cmd_skill_import(args: argparse.Namespace) -> int:
    console = args.console
    project = _ctx(args).resolve_project(args.project)
    bundle = json.loads(Path(args.path).read_text(encoding="utf-8"))
    result = _service(args).import_bundle(
        project.id, bundle, allow_conflicts=bool(args.allow_conflicts)
    )
    console.json(result) if console.json_mode else console.print(
        f"Imported {len(result['created'])}, skipped {len(result['skipped'])}, "
        f"conflicts {len(result['conflicts'])}"
    )
    return 0


def cmd_skill_seed(args: argparse.Namespace) -> int:
    console = args.console
    project = _ctx(args).resolve_project(args.project)
    created = _service(args).seed_defaults(project.id)
    payload = {"created": [skill.effective_key for skill in created]}
    console.json(payload) if console.json_mode else console.print(
        f"Installed {len(created)} built-in skill(s)"
    )
    return 0


def cmd_skill_run(args: argparse.Namespace) -> int:
    from mayaworkbench.skills.executor import execute_skill

    console = args.console
    skill = _service(args).get(args.skill_id)
    context: dict[str, str] = {}
    for raw in args.context:
        if "=" in raw:
            key, value = raw.split("=", 1)
            context[key] = value
    result = execute_skill(skill, **context)
    payload = result.model_dump(mode="json")
    console.json(payload) if console.json_mode else console.print(result.output)
    return 0 if result.status != "FAILED" else 1


__all__ = ["register_skill_commands"]

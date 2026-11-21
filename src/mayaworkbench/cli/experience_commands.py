"""CLI commands for learning from negative results (WP2-01)."""

from __future__ import annotations

import argparse
import json


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def register_experience_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    experience = sub.add_parser(
        "experience", help="Record failed attempts and recall similar prior work"
    )
    subcommands = experience.add_subparsers(dest="experience_command", metavar="<action>")

    record = subcommands.add_parser("record", help="Record a failed attempt")
    record.add_argument("--project")
    record.add_argument("--goal", required=True)
    record.add_argument("--hypothesis", required=True)
    record.add_argument("--outcome", required=True)
    record.add_argument("--failure-signature", default="")
    record.add_argument("--base-commit")
    record.add_argument("--patch-hash")
    record.add_argument("--environment", default="")
    record.set_defaults(func=cmd_experience_record)

    suggest = subcommands.add_parser("suggest", help="Suggest similar prior attempts")
    suggest.add_argument("--project")
    suggest.add_argument("--goal", required=True)
    suggest.add_argument("--failure-signature", default="")
    suggest.add_argument("--limit", type=int, default=5)
    suggest.set_defaults(func=cmd_experience_suggest)


def cmd_experience_record(args: argparse.Namespace) -> int:
    console = args.console
    service = _services(args)
    project = service.resolve_project(args.project)
    memory = service.memory_service.record_attempt(
        project_id=project.id,
        goal=args.goal,
        hypothesis=args.hypothesis,
        outcome=args.outcome,
        failure_signature=args.failure_signature,
        base_commit=args.base_commit,
        patch_hash=args.patch_hash,
        environment=args.environment,
    )
    if console.json_mode:
        console.json({"memory": memory.model_dump(mode="json")})
    else:
        console.print(f"Recorded unconfirmed negative result {memory.id}")
        console.note("Confirm it explicitly with `maya memory confirm <id>` before it is recalled.")
    return 0


def cmd_experience_suggest(args: argparse.Namespace) -> int:
    console = args.console
    service = _services(args)
    project = service.resolve_project(args.project)
    suggestions = service.memory_service.suggest_prior_attempts(
        project_id=project.id,
        goal=args.goal,
        failure_signature=args.failure_signature,
        limit=args.limit,
    )
    if console.json_mode:
        console.json({"suggestions": suggestions})
    else:
        console.table(
            ["score", "memory", "prior hypothesis"],
            [
                (item["score"], item["memory_id"], item["hypothesis"][:70])
                for item in suggestions
            ],
        )
        console.note(suggestions[0]["advisory"] if suggestions else "No confirmed prior attempts.")
    return 0


__all__ = ["register_experience_commands"]

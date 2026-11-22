"""CLI commands for environment detection and provisioning plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _add_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path", nargs="?", default=".",
        help="Project root to inspect (default: .).",
    )
    parser.add_argument(
        "--path", dest="opt_path", default=None,
        help="Project root to inspect; overrides the positional path when both are given.",
    )


def _resolve_path(args: argparse.Namespace) -> str:
    # --path wins when both forms are supplied.
    return args.opt_path or args.path or "."


def register_environment_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    env = sub.add_parser("env", help="Detect project environments and render bounded plans")
    subcommands = env.add_subparsers(dest="env_command", metavar="<action>")

    detect = subcommands.add_parser("detect", help="Detect ecosystems from descriptors")
    _add_path_arguments(detect)
    detect.set_defaults(func=cmd_env_detect)

    plan = subcommands.add_parser("plan", help="Render a provisioning plan (no execution)")
    _add_path_arguments(plan)
    plan.add_argument("--runtime", default="ROOTLESS_CONTAINER")
    plan.set_defaults(func=cmd_env_plan)

    doctor = subcommands.add_parser("doctor", help="Check runtime capability vs detected needs")
    _add_path_arguments(doctor)
    doctor.set_defaults(func=cmd_env_doctor)


def cmd_env_detect(args: argparse.Namespace) -> int:
    from mayaworkbench.workspace.environments import detect

    console = args.console
    info = detect(Path(_resolve_path(args))).as_dict()
    console.json(info) if console.json_mode else console.print(json.dumps(info, indent=2))
    return 0


def cmd_env_plan(args: argparse.Namespace) -> int:
    from mayaworkbench.workspace.environments import build_plan

    console = args.console
    plan = build_plan(Path(_resolve_path(args)), runtime=args.runtime).as_dict()
    console.json(plan) if console.json_mode else console.print(json.dumps(plan, indent=2))
    return 0


def cmd_env_doctor(args: argparse.Namespace) -> int:
    from mayaworkbench.workspace.capabilities import probe_execution_capabilities
    from mayaworkbench.workspace.environments import detect

    console = args.console
    info = detect(Path(_resolve_path(args)))
    caps = probe_execution_capabilities()
    payload = {
        "detected": info.as_dict(),
        "runtime_available": caps["execution_backends"]["rootless-container"]["available"],
        "limits": caps["limits"],
        "note": "Provisioning steps require an isolated runtime and explicit approval; "
        "no host install is performed.",
    }
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0 if payload["runtime_available"] else 1


__all__ = ["register_environment_commands"]

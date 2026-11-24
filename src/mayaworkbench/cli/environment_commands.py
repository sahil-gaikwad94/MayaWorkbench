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

    provision = subcommands.add_parser(
        "provision", help="Execute an approved plan through the task runtime and attest it"
    )
    _add_path_arguments(provision)
    provision.add_argument("--runtime", default="LOCAL_TRUSTED")
    provision.add_argument(
        "--approve-install", action="store_true",
        help="Explicit consent for dependency-install steps in this plan",
    )
    provision.add_argument("--service", action="append", default=[], metavar="NAME:PORT",
                           help="Loopback service to health-check, e.g. api:8000")
    provision.add_argument("--out", default=None, help="Write the provision report JSON here")
    provision.set_defaults(func=cmd_env_provision)


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


def cmd_env_provision(args: argparse.Namespace) -> int:
    """Execute a plan with an explicit, local consent gate."""
    from pathlib import Path

    from mayaworkbench.errors import PolicyError
    from mayaworkbench.workspace.environments import build_plan
    from mayaworkbench.workspace.provisioning import (
        EnvironmentProvisioner,
        ServiceSpec,
    )
    from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
    from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace
    from mayaworkbench.workspace.task_runtime import CommandRunnerTaskRuntime

    console = args.console
    root = Path(_resolve_path(args)).resolve()
    plan = build_plan(root, runtime=args.runtime)
    approved = frozenset({"install", "install-build", "install-runtime"}) if args.approve_install else frozenset()
    services: list[ServiceSpec] = []
    for entry in args.service:
        try:
            name, port = entry.rsplit(":", 1)
            services.append(ServiceSpec(name=name, port=int(port)))
        except (ValueError, PolicyError) as exc:
            raise PolicyError(f"invalid --service {entry!r}: {exc}") from exc

    ws = RuntimeWorkspace.for_host(root)
    policy = CommandPolicy(
        profile="USER_APPROVED_LOCAL",
        network_policy="ALLOW_WITH_APPROVAL",
        dependency_policy="APPROVAL_REQUIRED",
        max_runtime_seconds=1800,
    )
    runner = CommandRunner(ws.path, policy=policy, run_home=root / ".maya-provision-home")
    runtime = CommandRunnerTaskRuntime(
        workspace=ws, runner=runner, trust_profile="LOCAL_TRUSTED",
        install_authorized=bool(args.approve_install),
    )
    report = EnvironmentProvisioner(
        plan=plan,
        runtime=runtime,
        approved_purposes=approved,
        run_home=root / ".maya-provision-home",
        services=tuple(services),
    ).provision()
    payload = report.as_dict()
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0 if report.status in {"PROVISIONED", "PARTIAL"} else 1


__all__ = ["register_environment_commands"]

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

    spec = subcommands.add_parser(
        "spec", help="Resolve an immutable environment spec (no execution)"
    )
    _add_path_arguments(spec)
    spec.add_argument("--runtime", default="ROOTLESS_CONTAINER")
    spec.add_argument("--image", default="python:3.12-slim")
    spec.add_argument("--image-digest", dest="image_digest", default="")
    spec.add_argument(
        "--secret-ref", dest="secret_refs", action="append", default=[], metavar="NAME",
        help="Environment variable supplied by secret reference (never a value).",
    )
    spec.add_argument(
        "--service", dest="services", action="append", default=[], metavar="NAME:PORT",
        help="Service the environment depends on (declared, not started).",
    )
    spec.set_defaults(func=cmd_env_spec)

    provision = subcommands.add_parser(
        "provision", help="Provision the resolved spec inside the isolated runtime"
    )
    _add_path_arguments(provision)
    provision.add_argument("--runtime", default="ROOTLESS_CONTAINER")
    provision.add_argument("--image", default="python:3.12-slim")
    provision.add_argument(
        "--execute", action="store_true",
        help="Run the steps. Without this flag the spec is reported and nothing runs.",
    )
    provision.add_argument(
        "--allow-network", dest="allow_network", action="store_true",
        help="Approve package downloads; without it those steps are BLOCKED.",
    )
    provision.add_argument("--timeout", type=int, default=900)
    # The provision run must accept the same spec inputs as `env spec`, or it
    # would execute a different digest from the one a reviewer saw.
    provision.add_argument("--image-digest", dest="image_digest", default="")
    provision.add_argument(
        "--secret-ref", dest="secret_refs", action="append", default=[], metavar="NAME"
    )
    provision.add_argument(
        "--service", dest="services", action="append", default=[], metavar="NAME:PORT"
    )
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


def _services(values: list[str]) -> tuple:
    """Parse ``NAME:PORT`` service declarations (no values, no guessing)."""

    from mayaworkbench.workspace.environments import ServiceSpec

    services = []
    for value in values:
        name, _, port = value.rpartition(":")
        if not name or not port.isdigit():
            raise SystemExit(
                f"invalid --service {value!r}: expected NAME:PORT (for example db:5432)"
            )
        services.append(ServiceSpec(name=name, port=int(port)))
    return tuple(services)


def _resolved_spec(args: argparse.Namespace):
    from mayaworkbench.workspace.environments import resolve_spec

    return resolve_spec(
        Path(_resolve_path(args)),
        runtime=args.runtime,
        image=args.image,
        image_digest=getattr(args, "image_digest", ""),
        services=_services(getattr(args, "services", [])),
        secret_refs=tuple(getattr(args, "secret_refs", [])),
    )


def cmd_env_spec(args: argparse.Namespace) -> int:
    console = args.console
    payload = _resolved_spec(args).as_dict()
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0


def cmd_env_provision(args: argparse.Namespace) -> int:
    console = args.console
    spec = _resolved_spec(args)
    if not args.execute:
        payload = {
            "executed": False,
            "spec": spec.as_dict(),
            "network_gated_steps": list(spec.network_gated_steps),
            "note": "No step ran. Re-run with --execute; steps that download "
            "packages also require --allow-network.",
        }
        console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
        return 0

    from mayaworkbench.domain.enums import ExecutionTrust
    from mayaworkbench.workspace.backends import (
        BackendCommandRunner,
        RootlessContainerBackend,
    )
    from mayaworkbench.workspace.provisioning import EnvironmentProvisioner
    from mayaworkbench.workspace.runner import CommandPolicy
    from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace
    from mayaworkbench.workspace.task_runtime import CommandRunnerTaskRuntime

    if spec.runtime != ExecutionTrust.ROOTLESS_CONTAINER.value:
        payload = {
            "executed": False,
            "blocked": True,
            "spec_sha256": spec.spec_sha256,
            "reasons": [
                f"provisioning is implemented only for ROOTLESS_CONTAINER, not {spec.runtime!r}"
            ],
        }
        console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
        return 2

    home = Path(args.home).expanduser() if args.home else Path.home() / ".mayaworkbench"
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="ALLOW_WITH_APPROVAL" if args.allow_network else "DENY_BY_DEFAULT",
        dependency_policy="ALLOW_WITH_APPROVAL" if args.allow_network else "DENY",
    )
    backend = RootlessContainerBackend(
        Path(spec.root),
        home / "env-runs" / spec.spec_sha256[:12],
        policy,
        image=spec.image,
    )
    evidence = backend.verify_capabilities()
    if not evidence.available:
        payload = {
            "executed": False,
            "blocked": True,
            "spec_sha256": spec.spec_sha256,
            "reasons": evidence.mandatory_failures
            or list(backend.capabilities().reasons),
            "note": "The environment was not provisioned. ROOTLESS_CONTAINER is "
            "unavailable on this host, and a host install is never substituted.",
        }
        console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
        return 2

    workspace = RuntimeWorkspace.for_container(
        backend, Path(spec.root), snapshot_id="env-" + spec.spec_sha256[:12]
    )
    runner = BackendCommandRunner(backend, runtime=workspace.runtime)
    runtime = CommandRunnerTaskRuntime(
        workspace=workspace,
        runner=runner,
        trust_profile=ExecutionTrust.ROOTLESS_CONTAINER.value,
    )
    try:
        run = EnvironmentProvisioner(
            runtime,
            spec,
            allow_network=args.allow_network,
            timeout_seconds=args.timeout,
        ).run()
    finally:
        runtime.stop(reason="environment provisioning finished")
    payload = {"executed": True, **run.as_dict()}
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0 if run.succeeded else 1


__all__ = ["register_environment_commands"]

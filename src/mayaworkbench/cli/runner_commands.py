"""CLI commands for execution-plane capability evidence (WP0/WP1)."""

from __future__ import annotations

import argparse

from mayaworkbench.workspace.capabilities import probe_execution_capabilities


def register_runner_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    runner = sub.add_parser(
        "runner", help="Inspect execution capabilities and trust profiles"
    )
    runner_sub = runner.add_subparsers(dest="runner_command", metavar="<action>")
    runner_sub.add_parser(
        "doctor", help="Report what the host can actually enforce at runtime"
    ).set_defaults(func=cmd_runner_doctor)
    runner_sub.add_parser(
        "profiles", help="List execution trust profiles and their availability"
    ).set_defaults(func=cmd_runner_profiles)


def cmd_runner_doctor(args: argparse.Namespace) -> int:
    console = args.console
    report = probe_execution_capabilities()
    if console.json_mode:
        console.json(report)
        return 0
    console.heading("Execution capabilities")
    console.print(f"Python {report['runtime']['python']} on {report['runtime']['platform']}")
    console.print(
        f"uid={report['runtime']['uid']} privileged={report['runtime']['privileged']}"
    )
    console.section("Resource limits")
    for name, item in report["limits"].items():
        mechanism = item["mechanism"] or "none"
        console.print(f"  {name:16} available={item['available']!s:5} mechanism={mechanism}")
    console.section("Network isolation")
    console.print(f"  available={report['network_isolation']['available']}")
    console.section("cgroup v2")
    console.print(f"  available={report['cgroup_v2']['available']} pids={report['cgroup_v2'].get('pids', False)}")
    console.warning(report["warning"])
    return 0


def cmd_runner_profiles(args: argparse.Namespace) -> int:
    console = args.console
    report = probe_execution_capabilities()
    if console.json_mode:
        console.json({"trust_profiles": report["trust_profiles"]})
        return 0
    console.heading("Execution trust profiles")
    for name, item in report["trust_profiles"].items():
        console.print(f"  {name:20} available={item['available']!s:5} {item['boundary']}")
    console.warning("Isolated profiles are blocked, never silently downgraded to host privileges.")
    return 0


__all__ = ["register_runner_commands"]

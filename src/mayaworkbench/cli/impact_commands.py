"""CLI commands for change-impact analysis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mayaworkbench.errors import UsageError


def _services(args):
    from mayaworkbench.cli.main import services

    return services(args)


def _resolve_target_snapshot(args, svc):
    from mayaworkbench.cli.main import resolve_target_snapshot

    return resolve_target_snapshot(args, svc)


def register_impact_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    impact = sub.add_parser("impact", help="Change-impact analysis for an indexed snapshot")
    impact_sub = impact.add_subparsers(dest="impact_command", metavar="<action>")
    analyze = impact_sub.add_parser("analyze", help="Compute files/tests/symbols affected by a change")
    analyze.add_argument("--project")
    analyze.add_argument("--repo")
    analyze.add_argument("--snapshot", default=None)
    analyze.add_argument(
        "--changed",
        nargs="+",
        required=True,
        help="Snapshot-relative file paths that are changed (or to be changed)",
    )
    analyze.add_argument("--max-depth", type=int, default=6)
    analyze.add_argument("--json", action="store_true", default=None)
    analyze.set_defaults(func=cmd_impact_analyze)

    # Convenience: bare `maya impact --changed ...` dispatches to analyze.
    impact.add_argument("--project", default=None)
    impact.add_argument("--repo", default=None)
    impact.add_argument("--snapshot", default=None)
    impact.add_argument("--changed", nargs="+", default=None)
    impact.add_argument("--max-depth", type=int, default=6)
    impact.add_argument("--json", action="store_true", default=None)
    impact.set_defaults(func=_dispatch_impact)


def _dispatch_impact(args: argparse.Namespace) -> int:
    if not getattr(args, "changed", None):
        raise UsageError(
            "Pass --changed <path> [...], or use `maya impact analyze`.",
            hint="Example: maya impact --changed src/model.py tests/test_model.py",
        )
    return cmd_impact_analyze(args)


def cmd_impact_analyze(args: argparse.Namespace) -> int:
    console = getattr(args, "console", None)
    if console is None:
        # Fallback console for direct invocation.
        from mayaworkbench.cli.console import Console

        console = Console(json_mode=bool(getattr(args, "json", False)))
    svc = _services(args)
    _project, _repository, snapshot = _resolve_target_snapshot(args, svc)
    report = svc.impact.analyze(
        snapshot.id,
        list(args.changed),
        max_depth=int(getattr(args, "max_depth", 6) or 6),
    )
    payload = {"snapshot_id": snapshot.id, "impact": report.model_dump(mode="json")}
    if console.json_mode or getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    # Human-readable summary.
    print(f"Snapshot: {snapshot.id}")
    print(f"Changed: {', '.join(report.changed)}")
    print(f"Affected files: {len(report.affected_files)}")
    for item in report.affected_files[:50]:
        print(f"  [{item.depth}] {item.path} ({item.reason}, {item.confidence})")
    if len(report.affected_files) > 50:
        print(f"  ... {len(report.affected_files) - 50} more")
    print(f"Affected tests: {len(report.affected_tests)}")
    for path in report.affected_tests[:50]:
        print(f"  {path}")
    if len(report.affected_tests) > 50:
        print(f"  ... {len(report.affected_tests) - 50} more")
    if report.affected_symbols:
        print(f"Affected symbols: {len(report.affected_symbols)}")
    for note in report.notes:
        print(f"note: {note}")
    return 0

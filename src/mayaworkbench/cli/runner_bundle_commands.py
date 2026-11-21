"""CLI commands for user-owned portable runner bundles (WP10).

The parent CLI registers this module after ``runner_commands`` so ``export`` and
``import`` live under the existing ``maya runner`` command group. This module
never edits ``cli/main.py`` and never calls storage directly.
"""

from __future__ import annotations

import argparse


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def _runner_subparsers(sub: argparse._SubParsersAction):  # type: ignore[type-arg]
    """Reuse the ``runner`` parser if the parent already registered it."""
    runner = sub.choices.get("runner") if hasattr(sub, "choices") else None
    if runner is None:
        runner = sub.add_parser(
            "runner",
            help="Inspect execution capabilities and exchange portable run bundles",
        )
        return runner.add_subparsers(dest="runner_command", metavar="<action>")
    for action in runner._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return runner.add_subparsers(dest="runner_command", metavar="<action>")


def register_runner_bundle_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    runner_sub = _runner_subparsers(sub)

    export = runner_sub.add_parser(
        "export", help="Export a run or comparison as a portable runner bundle"
    )
    group = export.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", dest="run_id", help="xrun_... run id")
    group.add_argument("--comparison", dest="comparison_id", help="xcmp_... comparison id")
    export.add_argument(
        "--out", required=True, help="Destination .tar.gz path (must not already exist)"
    )
    export.set_defaults(func=cmd_runner_export)

    imp = runner_sub.add_parser(
        "import", help="Verify and import a portable runner bundle"
    )
    imp.add_argument("bundle", help="Path to the .tar.gz bundle")
    imp.set_defaults(func=cmd_runner_import)


def cmd_runner_export(args: argparse.Namespace) -> int:
    console = args.console
    identifier = args.run_id or args.comparison_id
    result = _services(args).experiment_service.export_run_bundle(identifier, args.out)
    payload = {
        "path": str(result.path),
        "archive_sha256": result.archive_sha256,
        "schema_version": result.manifest.get("schema_version"),
        "files": len(result.manifest.get("files", [])),
        "note": (
            "Imported results are unattested unless a trusted-runner signature "
            "verifies; no remote dispatch is performed."
        ),
    }
    if console.json_mode:
        console.json(payload)
    else:
        console.heading("Portable runner bundle exported")
        console.kv(
            [
                ("path", payload["path"]),
                ("sha256", payload["archive_sha256"]),
                ("files", payload["files"]),
            ]
        )
        console.warning(payload["note"])
    return 0


def cmd_runner_import(args: argparse.Namespace) -> int:
    console = args.console
    result = _services(args).experiment_service.import_run_bundle(args.bundle)
    payload = {
        "path": str(result.path),
        "archive_sha256": result.archive_sha256,
        "attestation": result.attestation,
        "imported_runs": len(result.created_run_ids),
        "observations": result.observation_count,
        "limitation": result.limitation,
        "notes": result.notes,
    }
    if console.json_mode:
        console.json(payload)
    else:
        console.heading("Portable runner bundle imported")
        console.kv(
            [
                ("path", payload["path"]),
                ("sha256", payload["archive_sha256"]),
                ("attestation", payload["attestation"]),
                ("imported runs", payload["imported_runs"]),
                ("observations", payload["observations"]),
            ]
        )
        console.warning(payload["limitation"])
        for note in result.notes:
            console.note(f"- {note}")
    return 0


__all__ = ["register_runner_bundle_commands"]

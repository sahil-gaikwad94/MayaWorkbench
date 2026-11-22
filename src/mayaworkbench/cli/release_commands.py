"""CLI command for machine-readable release evidence (WP-LAUNCH-1).

The command assembles a record from inputs that already exist. It deliberately
does not run pytest, Podman or a model provider: a report must be supplied, or
the command exits with a typed error instead of guessing counts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mayaworkbench.errors import UsageError
from mayaworkbench.release.evidence import (
    DEFAULT_TEST_COMMAND,
    build_evidence,
    render_markdown,
    to_json,
)


def register_release_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    release = sub.add_parser("release", help="Produce release attestations from real inputs")
    release_sub = release.add_subparsers(dest="release_command", metavar="<action>")

    evidence = release_sub.add_parser(
        "evidence", help="Build a machine-readable release evidence record"
    )
    evidence.add_argument(
        "--pytest-report",
        help="Path to a pytest JSON or JUnit XML report (required; counts are never guessed)",
    )
    evidence.add_argument(
        "--test-command",
        default=DEFAULT_TEST_COMMAND,
        help=f"Exact command that produced the report (default: {DEFAULT_TEST_COMMAND!r})",
    )
    evidence.add_argument(
        "--adversarial-dir",
        help="Directory containing results*.json adversarial harness results",
    )
    evidence.add_argument(
        "--out", help="Directory in which to write release-evidence.json and .md"
    )
    # SUPPRESS keeps the global --json value when this local flag is absent.
    evidence.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the evidence record as JSON",
    )
    evidence.set_defaults(func=cmd_release_evidence, no_migrate=True)


def cmd_release_evidence(args: argparse.Namespace) -> int:
    console = args.console
    if not getattr(args, "pytest_report", None):
        raise UsageError(
            "--pytest-report is required: release evidence never guesses test counts",
            hint=(
                "Generate a report (e.g. `python -m pytest -q --junitxml=report.xml` or "
                "pytest-json-report) and pass its path with --pytest-report."
            ),
        )

    # EvidenceError propagates to the top-level handler, which emits a typed
    # JSON error and the correct non-zero exit status.
    evidence = build_evidence(
        root=Path.cwd(),
        pytest_report=Path(args.pytest_report),
        test_command=args.test_command,
        adversarial_dir=Path(args.adversarial_dir) if args.adversarial_dir else None,
    )

    if args.out:
        target_dir = Path(args.out).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        json_path = target_dir / "release-evidence.json"
        md_path = target_dir / "release-evidence.md"
        json_path.write_text(to_json(evidence) + "\n", encoding="utf-8")
        md_path.write_text(render_markdown(evidence), encoding="utf-8")
        if console.json_mode:
            console.json(
                {
                    "json_path": str(json_path),
                    "markdown_path": str(md_path),
                    "canonical_sha256": evidence.canonical_sha256,
                }
            )
        else:
            console.heading("Release evidence written")
            console.kv(
                [
                    ("json", str(json_path)),
                    ("markdown", str(md_path)),
                    ("canonical sha256", evidence.canonical_sha256),
                ]
            )
        return 0

    if console.json_mode:
        console.json(json.loads(to_json(evidence)))
    else:
        console.print(render_markdown(evidence))
    return 0


__all__ = ["register_release_commands", "cmd_release_evidence"]

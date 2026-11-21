"""CLI for the north-star evaluation harness."""

from __future__ import annotations

import argparse
import json

from mayaworkbench.evaluation.metrics import outcome_from_bundle, score


def register_evaluation_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    evaluation = sub.add_parser("eval", help="Score verified task completion from evidence bundles")
    subcommands = evaluation.add_subparsers(dest="eval_command", metavar="<action>")
    scoring = subcommands.add_parser("score", help="Score one or more verification bundles")
    scoring.add_argument("bundle", nargs="+", help="Bundle directories containing report.json")
    scoring.add_argument("--human-minutes", type=float, default=0.0)
    scoring.set_defaults(func=cmd_eval_score)


def cmd_eval_score(args: argparse.Namespace) -> int:
    console = args.console
    outcomes = [
        outcome_from_bundle(bundle, human_minutes=args.human_minutes)
        for bundle in args.bundle
    ]
    payload = score(outcomes)
    payload["tasks_detail"] = [item.__dict__ for item in outcomes]
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0


__all__ = ["register_evaluation_commands"]

"""CLI for optional semantic adapters and their local evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def register_semantic_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    semantic = sub.add_parser("semantic", help="Evaluate optional semantic retrieval adapters")
    subcommands = semantic.add_subparsers(dest="semantic_command", metavar="<action>")
    evaluate = subcommands.add_parser("evaluate", help="Compare baseline vs reranked Hit@k")
    evaluate.add_argument("cases", help="JSON file with query/candidates/relevant cases")
    evaluate.add_argument("--k", type=int, default=5)
    evaluate.set_defaults(func=cmd_semantic_evaluate)


def cmd_semantic_evaluate(args: argparse.Namespace) -> int:
    from mayaworkbench.retrieval.semantic import evaluate_hybrid

    console = args.console
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    payload = evaluate_hybrid(cases, k=args.k)
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0


__all__ = ["register_semantic_commands"]

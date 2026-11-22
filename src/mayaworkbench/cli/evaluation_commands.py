"""CLI for the north-star evaluation harness and the offline R2I evaluator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mayaworkbench.domain.enums import ArtifactKind
from mayaworkbench.errors import MayaError, NotFoundError, UsageError
from mayaworkbench.evaluation.live import (
    live_gate_status,
    render_markdown as render_live_markdown,
    run_eval,
)
from mayaworkbench.evaluation.metrics import outcome_from_bundle, score
from mayaworkbench.evaluation.r2i import (
    bundled_seed_case,
    claims_from_artifact,
    evaluate_case,
    evaluate_corpus,
    evaluation_payload,
    load_gold_corpus,
    mappings_from_artifact,
    render_markdown,
)
from mayaworkbench.evaluation.tasks import (
    bundled_seed_corpus,
    corpus_hash,
    load_corpus,
)


def register_evaluation_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    evaluation = sub.add_parser("eval", help="Score verified task completion from evidence bundles")
    subcommands = evaluation.add_subparsers(dest="eval_command", metavar="<action>")
    scoring = subcommands.add_parser("score", help="Score one or more verification bundles")
    scoring.add_argument("bundle", nargs="+", help="Bundle directories containing report.json")
    scoring.add_argument("--human-minutes", type=float, default=0.0)
    scoring.set_defaults(func=cmd_eval_score)

    r2i = subcommands.add_parser(
        "r2i", help="Evaluate claim extraction and claim-to-code mapping offline"
    )
    r2i.add_argument(
        "--corpus",
        help="Gold case JSON file, list of cases, or directory (default: bundled seed case)",
    )
    claims_source = r2i.add_mutually_exclusive_group()
    claims_source.add_argument("--claims-artifact", help="CLAIM_SET artifact id to evaluate")
    claims_source.add_argument("--claims", help="Path to a claims JSON export")
    matrix_source = r2i.add_mutually_exclusive_group()
    matrix_source.add_argument("--matrix-artifact", help="CLAIM_MATRIX artifact id to evaluate")
    matrix_source.add_argument("--matrix", help="Path to a mapping matrix JSON export")
    r2i.set_defaults(func=cmd_eval_r2i)

    tasks = subcommands.add_parser(
        "tasks", help="List the labelled benchmark task corpus and its stable hash"
    )
    tasks.add_argument(
        "--corpus", help="Task corpus JSON file or directory (default: bundled internal seed)"
    )
    tasks.add_argument(
        "--json", action="store_true", dest="eval_tasks_json", help="Emit the tasks as JSON"
    )
    tasks.set_defaults(func=cmd_eval_tasks)

    run = subcommands.add_parser(
        "run", help="Run the benchmark through the task pipeline (offline dry-run by default)"
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="Opt in to a real provider; requires MAYA_LIVE_EVAL=1, a key and a positive cap",
    )
    run.add_argument("--corpus", help="Task corpus JSON file or directory")
    run.add_argument("--max-spend-usd", type=float, help="Hard total spend cap in USD")
    run.add_argument("--task-limit", type=int, help="Run at most this many tasks")
    run.add_argument("--out", help="Directory to write scorecard.json and scorecard.md")
    run.add_argument(
        "--json", action="store_true", dest="eval_run_json", help="Emit the scorecard as JSON"
    )
    run.set_defaults(func=cmd_eval_run)


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


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    # Imported lazily: cli.main registers this module while building its parser.
    from mayaworkbench.cli.main import services

    return services(args)


def _json_payload(
    *,
    artifact_id: str | None,
    path: str | None,
    svc: Any,
    expected_kind: ArtifactKind,
    label: str,
) -> Any:
    if not artifact_id and not path:
        raise UsageError(
            f"Provide either --{label}-artifact or --{label} to evaluate {label}",
            hint="Run `maya research extract` / `maya map run` first, or pass a JSON export path.",
        )
    if artifact_id:
        try:
            artifact = svc.artifacts.get(artifact_id)
        except NotFoundError as exc:
            raise UsageError(f"Unknown {label} artifact {artifact_id}: {exc}") from exc
        if artifact.kind is not expected_kind:
            raise UsageError(
                f"Artifact {artifact_id} is {artifact.kind.value}, expected {expected_kind.value}"
            )
        try:
            text = svc.artifacts.read_text(artifact_id)
        except MayaError as exc:
            raise UsageError(f"Cannot read {label} artifact {artifact_id}: {exc}") from exc
        source = f"artifact {artifact_id}"
    else:
        target = Path(str(path)).expanduser()
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"Cannot read {label} JSON from {target}: {exc}") from exc
        source = str(target)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{label} JSON from {source} is malformed: {exc}") from exc


def cmd_eval_r2i(args: argparse.Namespace) -> int:
    """Measure extraction and mapping quality; never a pass/fail gate."""
    console = args.console
    svc = _services(args)
    try:
        if args.corpus:
            cases = load_gold_corpus(args.corpus)
        else:
            cases = [bundled_seed_case()]
        claims_payload = _json_payload(
            artifact_id=args.claims_artifact,
            path=args.claims,
            svc=svc,
            expected_kind=ArtifactKind.CLAIM_SET,
            label="claims",
        )
        matrix_payload = _json_payload(
            artifact_id=args.matrix_artifact,
            path=args.matrix,
            svc=svc,
            expected_kind=ArtifactKind.CLAIM_MATRIX,
            label="matrix",
        )
        predictions = claims_from_artifact(claims_payload)
        mappings = mappings_from_artifact(matrix_payload)
        pairs = [(case, evaluate_case(case, claims=predictions, mappings=mappings)) for case in cases]
    except ValueError as exc:
        raise UsageError(f"Invalid R2I evaluation input: {exc}") from exc

    evaluation = pairs[0][1] if len(pairs) == 1 else evaluate_corpus(pairs)
    payload = evaluation_payload(evaluation)
    if console.json_mode:
        console.json(payload)
    else:
        console.print(render_markdown(evaluation))
    return 0


def _enable_subcommand_json(args: argparse.Namespace, *names: str) -> None:
    """ honour both `maya --json eval …` and `maya eval … --json`."""
    if any(getattr(args, name, False) for name in names):
        args.console.json_mode = True


def _load_task_corpus(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    try:
        return load_corpus(args.corpus) if args.corpus else bundled_seed_corpus()
    except ValueError as exc:
        raise UsageError(f"Invalid evaluation task corpus: {exc}") from exc


def cmd_eval_tasks(args: argparse.Namespace) -> int:
    """List the labelled corpus and the hash that pins it."""
    _enable_subcommand_json(args, "eval_tasks_json")
    console = args.console
    tasks = _load_task_corpus(args)
    payload = {
        "count": len(tasks),
        "corpus_hash": corpus_hash(tasks),
        "is_seed_corpus": all(task.is_seed_corpus for task in tasks),
        "provenance": tasks[0].provenance,
        "tasks": [task.model_dump(mode="json") for task in tasks],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Evaluation task corpus")
    console.kv(
        [
            ("tasks", len(tasks)),
            ("corpus hash", payload["corpus_hash"]),
            ("internal seed", "yes" if payload["is_seed_corpus"] else "no"),
            ("provenance", payload["provenance"]),
        ]
    )
    console.table(
        ["id", "family", "checks", "anti-gaming"],
        [
            [
                task.id,
                task.family,
                ", ".join(task.required_checks) or "—",
                "yes" if task.anti_gaming_probe else "no",
            ]
            for task in tasks
        ],
        max_column=52,
    )
    return 0


def cmd_eval_run(args: argparse.Namespace) -> int:
    """Run the benchmark; dry-run is the offline default."""
    _enable_subcommand_json(args, "eval_run_json")
    console = args.console
    if args.live:
        passed, reasons = live_gate_status(args.max_spend_usd)
        if not passed:
            raise UsageError(
                "live evaluation is not permitted: " + "; ".join(reasons),
                hint=(
                    "Set MAYA_LIVE_EVAL=1, export a model key and a positive "
                    "MAYA_LIVE_SPEND_CAP_USD (or pass --max-spend-usd > 0)."
                ),
            )
    tasks = _load_task_corpus(args)
    try:
        scorecard = run_eval(
            tasks,
            live=args.live,
            max_spend_usd=args.max_spend_usd,
            task_limit=args.task_limit,
        )
    except ValueError as exc:
        raise UsageError(f"Invalid evaluation run: {exc}") from exc

    payload = scorecard.model_dump(mode="json")
    if args.out:
        out = Path(args.out).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        (out / "scorecard.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (out / "scorecard.md").write_text(render_live_markdown(scorecard), encoding="utf-8")
        payload["out_dir"] = str(out)
    if console.json_mode:
        console.json(payload)
    else:
        console.print(render_live_markdown(scorecard))
        if args.out:
            console.note(f"scorecard written to {args.out}")
    return 0


__all__ = ["register_evaluation_commands"]

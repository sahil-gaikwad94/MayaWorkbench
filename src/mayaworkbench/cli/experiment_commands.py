"""CLI commands for the Experiment Lab (Phase 5).

The CLI is a presentation layer: every command calls ``ExperimentService``.
Pre-registration is deliberately multi-step (create → plan → approve → run), so
a user cannot approve an experiment in the same breath as defining it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from mayaworkbench.domain.enums import (
    ExperimentProfile,
    ExperimentRole,
    ExperimentStatus,
    MetricAggregation,
    MetricDirection,
    MetricsSourceKind,
)
from mayaworkbench.domain.models import (
    ExperimentBudget,
    ExperimentConfigRef,
    ExperimentDataset,
    ExperimentDeterminism,
    ExperimentEnvironment,
    ExperimentEvaluation,
    ExperimentHardware,
    ExperimentModelRef,
    ExperimentPreprocessing,
    ExperimentProtocol,
    MetricsSource,
    MetricSpec,
)
from mayaworkbench.errors import UsageError


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise UsageError(
                f"{flag} expects name=value, got {item!r}",
                hint=f"Example: {flag} train=0.8",
            )
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise UsageError(f"{flag} has an empty name in {item!r}")
        out[key] = value.strip()
    return out


def parse_metric_spec(text: str) -> MetricSpec:
    """Parse ``key,direction=LOWER,aggregation=MEAN,primary=true,unit=loss``.

    The first field is the metric key; the rest are explicit properties. Every
    property that affects the verdict must be typed by the user before the run,
    never inferred from the numbers afterwards.
    """
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise UsageError("empty --metric specification")
    key = parts[0]
    attributes: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            raise UsageError(
                f"metric property must be name=value, got {part!r}",
                hint=(
                    "Example: --metric "
                    "'val_loss,direction=LOWER,aggregation=MEAN,primary=true'"
                ),
            )
        name, value = part.split("=", 1)
        attributes[name.strip()] = value.strip()
    if "direction" not in attributes:
        raise UsageError(
            "each metric needs an explicit direction (LOWER, HIGHER or NEUTRAL)",
            hint="Directions are pre-registered; they are never inferred after seeing results.",
        )
    raw_direction = attributes.pop("direction").upper()
    raw_direction = {
        "LOWER": "LOWER_IS_BETTER",
        "HIGHER": "HIGHER_IS_BETTER",
    }.get(raw_direction, raw_direction)
    try:
        direction = MetricDirection(raw_direction)
    except ValueError as exc:
        raise UsageError(
            "unknown metric direction; use LOWER, HIGHER or NEUTRAL "
            "(LOWER_IS_BETTER/HIGHER_IS_BETTER also accepted)"
        ) from exc
    aggregation_raw = attributes.pop("aggregation", "LAST").upper()
    try:
        aggregation = MetricAggregation(aggregation_raw)
    except ValueError as exc:
        raise UsageError(f"unknown metric aggregation: {exc}") from exc
    primary_raw = attributes.pop("primary", "false").lower()
    if primary_raw not in {"true", "false", "1", "0", "yes", "no"}:
        raise UsageError(f"primary must be true/false, got {primary_raw!r}")
    primary = primary_raw in {"true", "1", "yes"}
    unknown = sorted(set(attributes) - {"unit", "description"})
    if unknown:
        raise UsageError(
            f"unknown metric properties: {', '.join(unknown)}",
            hint="Allowed: direction, aggregation, primary, unit, description.",
        )
    return MetricSpec(
        key=key,
        direction=direction,
        unit=attributes.get("unit", ""),
        aggregation=aggregation,
        primary=primary,
        description=attributes.get("description", ""),
    )


def _protocol(args: argparse.Namespace) -> ExperimentProtocol:
    return ExperimentProtocol(
        dataset=ExperimentDataset(
            id=args.dataset_id,
            version=args.dataset_version,
            fingerprint=args.dataset_fingerprint,
            fingerprint_method=args.dataset_fingerprint_method,
            path=args.dataset_path,
        ),
        splits=_pairs(args.split, "--split"),
        preprocessing=ExperimentPreprocessing(
            code_hash=args.preprocessing_hash,
            description=args.preprocessing_description,
        ),
        evaluation=ExperimentEvaluation(
            script_path=args.evaluation_script,
            script_hash=args.evaluation_script_hash,
        ),
        model=ExperimentModelRef(
            identity=args.model_identity,
            checkpoint_hash=args.checkpoint_hash,
        ),
        config=ExperimentConfigRef(
            resolved=_pairs(args.config_value, "--config-value"),
            hash=args.config_hash,
        ),
        environment=ExperimentEnvironment(
            lockfile_path=args.lockfile,
            lockfile_hash=args.lockfile_hash,
            image_digest=args.image_digest,
            python_version=args.python_version,
            framework_versions=_pairs(args.framework, "--framework"),
        ),
        hardware=ExperimentHardware(
            profile=args.hardware_profile,
            device=args.device,
            cpu_count=args.cpu_count,
            memory_mb=args.memory_mb,
        ),
        determinism=ExperimentDeterminism(
            seeded=not args.non_deterministic,
            notes=args.determinism_notes,
        ),
        batch_size=args.batch_size,
        sequence_length=args.sequence_length,
        stopping_rule=args.stopping_rule,
        runtime_limit_seconds=args.runtime_limit,
    )


def register_experiment_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    experiment = sub.add_parser(
        "experiment", help="Pre-register, run and compare experiments (Phase 5)"
    )
    experiment_sub = experiment.add_subparsers(
        dest="experiment_command", metavar="<action>"
    )

    # create ---------------------------------------------------------------
    create = experiment_sub.add_parser(
        "create", help="Create an immutable, content-hashed pre-registration"
    )
    create.add_argument("--project")
    create.add_argument("--repo")
    create.add_argument("--snapshot")
    create.add_argument("--name", required=True)
    create.add_argument("--hypothesis", required=True)
    create.add_argument(
        "--role", choices=[item.value for item in ExperimentRole], required=True
    )
    create.add_argument(
        "--profile",
        choices=[item.value for item in ExperimentProfile],
        default="CPU_SMOKE",
    )
    create.add_argument(
        "--metric",
        dest="metrics",
        action="append",
        required=True,
        help=(
            "key,direction=LOWER|HIGHER|NEUTRAL,aggregation=LAST|MEAN|...,primary=true "
            "(repeat for each metric)"
        ),
    )
    create.add_argument(
        "--seed", dest="seeds", action="append", type=int, required=True
    )
    create.add_argument("--timeout", type=int, default=300)
    create.add_argument(
        "--metrics-source",
        choices=[item.value for item in MetricsSourceKind],
        default="STDOUT_JSON",
    )
    create.add_argument("--metrics-file", help="Workspace-relative path for JSON_FILE")
    create.add_argument(
        "--treatment-dimension",
        action="append",
        default=[],
        help=(
            "Declared intervention dimension (repeatable). Default: command, "
            "repository.commit, repository.patch"
        ),
    )
    create.add_argument("--allowed-variation", action="append", default=[])
    create.add_argument("--min-seeds", type=int)
    create.add_argument(
        "--margin",
        type=float,
        help="Non-inferiority margin on the primary metric (native units)",
    )
    create.add_argument("--budget-runs", type=int, default=10)
    create.add_argument("--budget-runtime", type=int, default=3600)
    create.add_argument("--budget-artifact-bytes", type=int, default=5_000_000)
    create.add_argument("--budget-observations", type=int, default=10_000)
    create.add_argument("--task")
    create.add_argument("--dataset-id", default="")
    create.add_argument("--dataset-version", default="")
    create.add_argument("--dataset-fingerprint", default="")
    create.add_argument("--dataset-fingerprint-method", default="")
    create.add_argument("--dataset-path")
    create.add_argument("--split", action="append", default=[])
    create.add_argument("--preprocessing-hash", default="")
    create.add_argument("--preprocessing-description", default="")
    create.add_argument("--evaluation-script")
    create.add_argument("--evaluation-script-hash", default="")
    create.add_argument("--model-identity", default="")
    create.add_argument("--checkpoint-hash", default="")
    create.add_argument("--config-value", action="append", default=[])
    create.add_argument("--config-hash", default="")
    create.add_argument("--lockfile")
    create.add_argument("--lockfile-hash", default="")
    create.add_argument("--image-digest", default="")
    create.add_argument("--python-version", default="")
    create.add_argument("--framework", action="append", default=[])
    create.add_argument("--hardware-profile", default="")
    create.add_argument("--device", default="")
    create.add_argument("--cpu-count", type=int)
    create.add_argument("--memory-mb", type=int)
    create.add_argument("--non-deterministic", action="store_true")
    create.add_argument("--determinism-notes", default="")
    create.add_argument("--batch-size", type=int)
    create.add_argument("--sequence-length", type=int)
    create.add_argument("--stopping-rule", default="")
    create.add_argument("--runtime-limit", type=int)
    create.add_argument(
        "--command",
        nargs=argparse.REMAINDER,
        help=(
            "Command to execute, placed last; everything after it is the command: "
            "--command python eval.py --seed {seed}"
        ),
    )
    create.set_defaults(func=cmd_experiment_create)

    # list / show / plan ---------------------------------------------------
    listing = experiment_sub.add_parser("list", help="List experiment definitions")
    listing.add_argument("--project")
    listing.add_argument("--task")
    listing.add_argument("--role", choices=[item.value for item in ExperimentRole])
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_experiment_list)

    show = experiment_sub.add_parser(
        "show", help="Show a definition and its runs, or a comparison report"
    )
    show.add_argument("experiment_id", help="exp_... or xcmp_... id")
    show.set_defaults(func=cmd_experiment_show)

    plan = experiment_sub.add_parser("plan", help="Dry-run the declared experiment")
    plan.add_argument("experiment_id")
    plan.set_defaults(func=cmd_experiment_plan)

    # lifecycle ------------------------------------------------------------
    approve = experiment_sub.add_parser("approve", help="Approve a DRAFT pre-registration")
    approve.add_argument("experiment_id")
    approve.add_argument("--reason")
    approve.set_defaults(func=cmd_experiment_approve)

    revoke = experiment_sub.add_parser("revoke", help="Revoke an approved pre-registration")
    revoke.add_argument("experiment_id")
    revoke.add_argument("--reason", required=True)
    revoke.set_defaults(func=cmd_experiment_revoke)

    run = experiment_sub.add_parser("run", help="Execute approved seeds locally")
    run.add_argument("experiment_id")
    run.add_argument("--seed", dest="seeds", action="append", type=int)
    run.add_argument("--keep-workspace", action="store_true")
    run.set_defaults(func=cmd_experiment_run)

    # external evidence ----------------------------------------------------
    record = experiment_sub.add_parser(
        "record", help="Record one externally executed run from a metrics file"
    )
    record.add_argument("experiment_id")
    record.add_argument("--seed", type=int, required=True)
    record.add_argument("--metrics", required=True, help="JSON/JSONL/CSV metrics file")
    record.add_argument("--note", required=True, help="Where and how the run executed")
    record.add_argument("--executed-at", help="ISO timestamp from the external run")
    record.set_defaults(func=cmd_experiment_record)

    ingest = experiment_sub.add_parser(
        "ingest", help="Attach externally produced metrics to a failed local run"
    )
    ingest.add_argument("run_id")
    ingest.add_argument("--metrics", required=True)
    ingest.set_defaults(func=cmd_experiment_ingest)

    # comparison -----------------------------------------------------------
    compare = experiment_sub.add_parser(
        "compare", help="Compare a candidate against a baseline under the protocol"
    )
    compare.add_argument("candidate_id")
    compare.add_argument("--baseline", required=True)
    compare.add_argument(
        "--remember",
        action="store_true",
        help="Also store the result as unconfirmed project memory",
    )
    compare.set_defaults(func=cmd_experiment_compare)

    comparisons = experiment_sub.add_parser("comparisons", help="List comparisons")
    comparisons.add_argument("--project")
    comparisons.add_argument("--task")
    comparisons.add_argument("--candidate")
    comparisons.add_argument("--limit", type=int, default=50)
    comparisons.set_defaults(func=cmd_experiment_comparisons)

    comparison_show = experiment_sub.add_parser(
        "show-comparison", help="Print the stored comparison report"
    )
    comparison_show.add_argument("comparison_id")
    comparison_show.set_defaults(func=cmd_experiment_show_comparison)

    # reproduction ---------------------------------------------------------
    bundle = experiment_sub.add_parser(
        "bundle", help="Export a reproduction bundle for an experiment or comparison"
    )
    bundle.add_argument("identifier")
    bundle.add_argument("directory")
    bundle.set_defaults(func=cmd_experiment_bundle)

    verify = experiment_sub.add_parser(
        "verify-bundle", help="Independently verify a reproduction bundle"
    )
    verify.add_argument("directory")
    verify.set_defaults(func=cmd_experiment_verify_bundle)

    adapter = experiment_sub.add_parser(
        "export-adapter", help="Export a comparison to MLflow or TensorBoard format"
    )
    adapter.add_argument("comparison_id")
    adapter.add_argument("--format", choices=["mlflow", "tensorboard"], required=True)
    adapter.add_argument("--out", required=True)
    adapter.set_defaults(func=cmd_experiment_export_adapter)


def cmd_experiment_export_adapter(args: argparse.Namespace) -> int:
    from mayaworkbench.experiments.adapters import export_adapter

    console = args.console
    svc = _services(args)
    comparison = svc.experiment_store.get_comparison(args.comparison_id).model_dump(mode="json")
    files = export_adapter(comparison, format=args.format, directory=args.out)
    payload = {"format": args.format, "files": files, "attestation": "UNATTESTED"}
    console.json(payload) if console.json_mode else console.print(
        f"Exported {args.format} adapter to {args.out}"
    )
    return 0


# ------------------------------------------------------------------ create
def cmd_experiment_create(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project, _repository, snapshot = svc.resolve_snapshot_context(
        project_identifier=args.project,
        repository_identifier=args.repo,
        snapshot_id=args.snapshot,
    )
    metrics = [parse_metric_spec(item) for item in args.metrics]
    primaries = [item for item in metrics if item.primary]
    if len(primaries) != 1:
        raise UsageError(
            "exactly one metric must be primary",
            hint="Mark one with primary=true; secondary metrics are exploratory.",
        )
    keys = [item.key for item in metrics]
    if len(keys) != len(set(keys)):
        raise UsageError("metric keys must be unique")
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    role = ExperimentRole(args.role)
    profile = ExperimentProfile(args.profile)
    definition = svc.experiment_service.create_definition(
        project_id=project.id,
        snapshot_id=snapshot.id,
        name=args.name,
        hypothesis=args.hypothesis,
        role=role,
        profile=profile,
        command=command,
        seeds=args.seeds,
        metrics=metrics,
        timeout_seconds=args.timeout,
        metrics_source=MetricsSource(
            kind=MetricsSourceKind(args.metrics_source), path=args.metrics_file
        ),
        protocol=_protocol(args),
        treatment_dimensions=args.treatment_dimension or None,
        allowed_variation=args.allowed_variation,
        budget=ExperimentBudget(
            max_runs=args.budget_runs,
            max_total_runtime_seconds=args.budget_runtime,
            max_artifact_bytes=args.budget_artifact_bytes,
            max_metric_observations=args.budget_observations,
        ),
        min_seeds=args.min_seeds,
        non_inferiority_margin=args.margin,
        task_id=args.task,
    )
    payload = svc.experiment_service.definition_view(definition.id)
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Pre-registered experiment {definition.name}")
    console.kv(
        [
            ("id", definition.id),
            ("role", definition.role.value),
            ("profile", definition.profile.value),
            ("status", definition.status.value),
            ("snapshot", definition.snapshot_id),
            ("commit", definition.base_commit_sha),
            ("seeds", ", ".join(str(seed) for seed in definition.seeds)),
            ("content sha256", definition.content_sha256),
            ("metrics", ", ".join(f"{item.key} ({item.direction.value})" for item in definition.metrics)),
        ]
    )
    console.section("Next")
    console.bullet(f"maya experiment plan {definition.id}")
    console.bullet(f"maya experiment approve {definition.id}")
    console.note(
        "\nThe definition is immutable. Any change needs a new pre-registration "
        "and a new approval."
    )
    return 0


def cmd_experiment_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project) if args.project else None
    definitions = svc.experiment_store.list_definitions(
        project_id=project.id if project else None,
        task_id=args.task,
        role=args.role,
        limit=args.limit,
    )
    rows = [
        {
            "id": item.id,
            "name": item.name,
            "role": item.role.value,
            "profile": item.profile.value,
            "status": item.status.value,
            "seeds": len(item.seeds),
            "runs": len(svc.experiment_store.list_runs(item.id)),
            "task": item.task_id,
        }
        for item in definitions
    ]
    if console.json_mode:
        console.json({"experiments": rows})
        return 0
    console.heading("Experiments")
    console.table(
        ["id", "name", "role", "profile", "status", "seeds", "runs"],
        [
            [
                row["id"],
                row["name"],
                row["role"],
                row["profile"],
                row["status"],
                row["seeds"],
                row["runs"],
            ]
            for row in rows
        ],
        max_column=48,
    )
    return 0


def cmd_experiment_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    if args.experiment_id.startswith("xcmp_"):
        comparison = svc.experiment_store.get_comparison(args.experiment_id)
        report = svc.experiment_service.comparison_markdown(comparison.id)
        if console.json_mode:
            console.json(
                {"comparison": comparison.model_dump(mode="json"), "report": report}
            )
            return 0
        console.print(report)
        return 0
    payload = svc.experiment_service.definition_view(args.experiment_id)
    if console.json_mode:
        console.json(payload)
        return 0
    definition = payload["definition"]
    console.heading(f"{definition['name']} ({definition['status']})")
    console.kv(
        [
            ("id", definition["id"]),
            ("role", definition["role"]),
            ("profile", definition["profile"]),
            ("hypothesis", definition["hypothesis"]),
            ("snapshot", definition["snapshot_id"]),
            ("commit", definition["base_commit_sha"]),
            ("command", " ".join(definition["command"]) or "(static)"),
            ("seeds", ", ".join(str(seed) for seed in definition["seeds"])),
            ("min seeds", definition["min_seeds"]),
            ("content sha256", definition["content_sha256"]),
            ("usable seeds", ", ".join(str(seed) for seed in payload["usable_seeds"]) or "—"),
            ("approved by", definition.get("approved_by") or "—"),
            ("approval reason", definition.get("approval_reason") or "—"),
        ]
    )
    console.section("Metrics")
    console.table(
        ["key", "direction", "aggregation", "primary", "unit"],
        [
            [
                metric["key"],
                metric["direction"],
                metric["aggregation"],
                "yes" if metric["primary"] else "no",
                metric["unit"] or "—",
            ]
            for metric in definition["metrics"]
        ],
    )
    console.section("Runs")
    if payload["runs"]:
        console.table(
            ["id", "seed", "attempt", "status", "usable", "exit", "observations", "error"],
            [
                [
                    run["id"],
                    run["seed"],
                    run["attempt"],
                    run["status"],
                    "yes" if run["status"] == "COMPLETED" and run["metrics_ingested"] else "no",
                    run["exit_code"] if run["exit_code"] is not None else "—",
                    run["observation_count"],
                    (run["error"] or run["metrics_error"] or "—")[:48],
                ]
                for run in payload["runs"]
            ],
            max_column=40,
        )
    else:
        console.note("No runs yet.")
    if payload["comparisons"]:
        console.section("Comparisons (as candidate)")
        console.table(
            ["id", "verdict", "comparable", "created"],
            [
                [item["id"], item["verdict"], item["comparable"], item["created_at"]]
                for item in payload["comparisons"]
            ],
        )
    return 0


def cmd_experiment_plan(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    payload = svc.experiment_service.plan(args.experiment_id)
    if console.json_mode:
        console.json(payload)
        return 0
    definition = payload["definition"]
    console.heading(f"Plan: {definition['name']}")
    if payload["blockers"]:
        console.section("Blockers")
        for blocker in payload["blockers"]:
            console.bullet(blocker)
    console.kv(
        [
            ("status", definition["status"]),
            ("seed runs planned", len(payload["planned_runs"])),
            ("existing runs", payload["existing_runs"]),
            ("worst case", f"{payload['worst_case_seconds']}s"),
            ("budget runs", payload["budget"]["max_runs"]),
            ("budget runtime", payload["budget"]["max_total_runtime_seconds"]),
        ]
    )
    console.section("Commands")
    console.table(
        ["seed", "attempt", "command"],
        [
            [item["seed"], item["attempt"], " ".join(item["command"]) or "(static)"]
            for item in payload["planned_runs"]
        ],
        max_column=70,
    )
    if payload["unknown_protocol_fields"]:
        console.section("Unknown protocol fields")
        for path in payload["unknown_protocol_fields"]:
            console.bullet(path)
        console.note("Unknown fields are reported as gaps, never assumed equal.")
    console.note("\n" + payload["reminder"])
    return 0


def cmd_experiment_approve(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    definition = svc.experiment_service.approve(args.experiment_id, reason=args.reason)
    if console.json_mode:
        console.json(definition.model_dump(mode="json"))
        return 0
    console.heading(f"Approved {definition.id}")
    console.kv(
        [
            ("status", definition.status.value),
            ("approved by", definition.approved_by),
            ("approved at", definition.approved_at.isoformat() if definition.approved_at else "—"),
            ("reason", definition.approval_reason or "—"),
        ]
    )
    console.print("")
    console.print(f"Next: maya experiment run {definition.id}")
    return 0


def cmd_experiment_revoke(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    definition = svc.experiment_service.revoke(args.experiment_id, reason=args.reason)
    if console.json_mode:
        console.json(definition.model_dump(mode="json"))
        return 0
    console.heading(f"Revoked {definition.id}")
    console.kv([("status", definition.status.value), ("reason", definition.revocation_reason or args.reason)])
    return 0


def cmd_experiment_run(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    runs = svc.experiment_service.run(
        args.experiment_id,
        seeds=args.seeds,
        keep_workspace=args.keep_workspace,
    )
    payload = {"runs": [run.model_dump(mode="json") for run in runs]}
    if console.json_mode:
        console.json(payload)
        return 0 if all(run.usable for run in runs) else 1
    console.heading("Experiment runs")
    console.table(
        ["id", "seed", "attempt", "status", "exit", "duration", "observations"],
        [
            [
                run.id,
                run.seed,
                run.attempt,
                run.status.value,
                run.exit_code if run.exit_code is not None else "—",
                f"{run.duration_ms} ms" if run.duration_ms is not None else "—",
                run.observation_count,
            ]
            for run in runs
        ],
    )
    for run in runs:
        if run.error:
            console.warning(f"{run.id}: {run.error}")
        if run.metrics_error:
            console.warning(f"{run.id}: metrics: {run.metrics_error}")
    usable = [run for run in runs if run.usable]
    console.print("")
    console.print(f"usable runs: {len(usable)}/{len(runs)}")
    if usable:
        console.print(
            f"Next: maya experiment bundle {args.experiment_id} <new-directory>"
        )
    return 0 if len(usable) == len(runs) else 1


def cmd_experiment_record(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    text = Path(args.metrics).expanduser().read_text(encoding="utf-8")
    run = svc.experiment_service.record_external(
        args.experiment_id,
        seed=args.seed,
        metrics_text=text,
        note=args.note,
        executed_at=args.executed_at,
    )
    if console.json_mode:
        console.json(run.model_dump(mode="json"))
        return 0
    console.heading(f"Recorded external run {run.id}")
    console.kv(
        [
            ("experiment", run.experiment_id),
            ("seed", run.seed),
            ("attempt", run.attempt),
            ("status", run.status.value),
            ("observations", run.observation_count),
            ("note", args.note),
        ]
    )
    console.note(
        "\nExternal runs are marked IMPORTED: their environment was not verified here."
    )
    return 0


def cmd_experiment_ingest(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    text = Path(args.metrics).expanduser().read_text(encoding="utf-8")
    run = svc.experiment_service.ingest(args.run_id, metrics_text=text)
    if console.json_mode:
        console.json(run.model_dump(mode="json"))
        return 0
    console.heading(f"Ingested metrics for {run.id}")
    console.kv(
        [
            ("experiment", run.experiment_id),
            ("seed", run.seed),
            ("observation count", run.observation_count),
            ("metrics artifact", run.metrics_artifact_id or "—"),
        ]
    )
    return 0


def cmd_experiment_compare(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    comparison = svc.experiment_service.compare(
        args.candidate_id,
        baseline_id=args.baseline,
        remember=args.remember,
    )
    report = svc.experiment_service.comparison_markdown(comparison.id)
    payload = {
        "comparison": comparison.model_dump(mode="json"),
        "report": report,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.print(report)
    console.print("")
    console.print(f"comparison: {comparison.id}")
    console.print(
        f"Next: maya experiment bundle {comparison.id} <new-directory>"
    )
    if args.remember:
        memories = svc.memory_service.list(comparison.project_id, limit=50)
        match = next(
            (
                item
                for item in memories
                if item.scope == f"experiment-comparison:{comparison.id}"
            ),
            None,
        )
        console.note(
            "Stored as UNCONFIRMED project memory"
            + (f" ({match.id}); confirm it explicitly." if match else ".")
        )
    return 0


def cmd_experiment_comparisons(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project) if args.project else None
    items = svc.experiment_store.list_comparisons(
        project_id=project.id if project else None,
        candidate_experiment_id=args.candidate,
        task_id=args.task,
        limit=args.limit,
    )
    rows = [
        {
            "id": item.id,
            "candidate": item.candidate_experiment_id,
            "baseline": item.baseline_experiment_id,
            "verdict": item.verdict.value,
            "comparable": item.comparable,
            "design": item.design.value,
            "created": item.created_at.isoformat(),
        }
        for item in items
    ]
    if console.json_mode:
        console.json({"comparisons": rows})
        return 0
    console.heading("Experiment comparisons")
    console.table(
        ["id", "verdict", "comparable", "design", "candidate", "baseline"],
        [
            [
                row["id"],
                row["verdict"],
                "yes" if row["comparable"] else "no",
                row["design"],
                row["candidate"],
                row["baseline"],
            ]
            for row in rows
        ],
        max_column=36,
    )
    return 0


def cmd_experiment_show_comparison(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    report = svc.experiment_service.comparison_markdown(args.comparison_id)
    if console.json_mode:
        console.json({"comparison_id": args.comparison_id, "report": report})
        return 0
    console.print(report)
    return 0


def cmd_experiment_bundle(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    result = svc.experiment_service.bundle(args.identifier, args.directory)
    if console.json_mode:
        console.json(
            {"path": str(result.path), "manifest": result.manifest}
        )
        return 0
    console.heading("Experiment reproduction bundle")
    console.kv(
        [
            ("directory", str(result.path)),
            ("kind", result.manifest["kind"]),
            ("files", len(result.manifest["files"])),
            ("root sha256", result.manifest["root_sha256"]),
            ("gaps", len(result.manifest["gaps"])),
        ]
    )
    if result.manifest["gaps"]:
        console.section("Known gaps")
        for gap in result.manifest["gaps"]:
            console.bullet(gap)
    console.print("")
    console.print(f"Verify: maya experiment verify-bundle {result.path}")
    return 0


def cmd_experiment_verify_bundle(args: argparse.Namespace) -> int:
    console = args.console
    from mayaworkbench.experiments.bundle import verify_experiment_bundle

    result = verify_experiment_bundle(args.directory)
    payload = {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "manifest": result.manifest,
        "reproduce": result.reproduce,
    }
    if console.json_mode:
        console.json(payload)
        return 0 if result.ok else 1
    console.heading("Experiment bundle verification")
    console.print("status: " + ("ok" if result.ok else "FAILED"))
    if result.manifest:
        console.kv(
            [
                ("bundle", result.manifest.get("bundle_id", "—")),
                ("kind", result.manifest.get("kind", "—")),
                ("files", len(result.manifest.get("files", []))),
                ("root sha256", result.manifest.get("root_sha256", "—")),
            ]
        )
    if result.errors:
        console.section("Errors")
        for error in result.errors:
            console.bullet(error)
    if result.warnings:
        console.section("Warnings")
        for warning in result.warnings:
            console.bullet(warning)
    reproduce = result.reproduce
    if reproduce.get("gaps"):
        console.section("Declared reproduction gaps")
        for gap in reproduce["gaps"]:
            console.bullet(gap)
    return 0 if result.ok else 1


__all__ = [
    "register_experiment_commands",
    "parse_metric_spec",
]

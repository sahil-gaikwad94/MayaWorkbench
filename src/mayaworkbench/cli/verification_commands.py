"""CLI commands for verification and evidence (Phase 4).

Registration lives in this module for the same reason as the research/task
command areas: a missing command area becomes a parser test failure instead of a
silent product gap.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mayaworkbench.domain.enums import TaskDecision
from mayaworkbench.errors import UsageError
from mayaworkbench.verification import registry as check_registry
from mayaworkbench.verification.bundle import verify_bundle
from mayaworkbench.verification.registry import command_check, invariant_check


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def _resolve_run(svc, identifier: str):  # type: ignore[no-untyped-def]
    if identifier.startswith("run_"):
        return svc.runs.get(identifier)
    run = svc.task_service.latest_run_for_task(identifier)
    if run is None:
        raise UsageError(
            f"Task {identifier} has no run",
            hint="Run `maya task start` and `maya task run` first.",
        )
    return run


def _criteria(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _parse_scalar(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_invariant_expression(expression: str) -> Any:
    """Parse ``name:module=...,callable=...,shape=1x4x32,param.x=1``."""
    name, _, rest = expression.partition(":")
    if not name or not rest:
        raise UsageError(
            f"Invalid invariant expression: {expression!r}",
            hint=(
                "Use name:module=<dotted-or-.py>,callable=<name>,"
                "shape=1,4,32,dtype=float32,seed=0,param.<key>=<json>"
            ),
        )
    fields: dict[str, str] = {}
    for item in rest.split(","):
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise UsageError(f"Invalid invariant field: {item!r}")
        fields[key.strip()] = value.strip()
    parameters: dict[str, Any] = {}
    for key in list(fields):
        if key.startswith("param."):
            parameters[key[6:]] = _parse_scalar(fields.pop(key))
    shape: list[int] | None = None
    if fields.get("shape"):
        try:
            shape = [int(part) for part in fields.pop("shape").split("x") if part]
        except ValueError as exc:
            raise UsageError(
                "shape must use dimensions separated by 'x', e.g. shape=1x4x32"
            ) from exc
    # A typo must fail instead of being silently ignored and producing a check
    # that runs something different from what the user wrote.
    known = {"module", "callable", "dtype", "seed", "key", "timeout", "required"}
    unknown_fields = sorted(set(fields) - known)
    if unknown_fields:
        raise UsageError(
            "Unknown invariant field(s): " + ", ".join(unknown_fields),
            hint="Use module, callable, shape, dtype, seed, key, timeout, required or param.<key>.",
        )
    module = fields.pop("module", "")
    if not module:
        raise UsageError("An invariant requires module=<module-or-.py-path>")
    return invariant_check(
        name,
        module=module,
        callable_name=fields.pop("callable", None),
        input_shape=shape,
        dtype=fields.pop("dtype", "float32"),
        seed=int(fields.pop("seed", "0")),
        parameters=parameters,
        key=fields.pop("key", None),
        timeout_seconds=int(fields.pop("timeout", "300")),
        required=fields.pop("required", "true").lower() not in {"false", "0", "no"},
    )


def _extra_checks(args: argparse.Namespace) -> list[Any]:
    criteria = _criteria(getattr(args, "check_criteria", None))
    checks = []
    for command in getattr(args, "checks", []) or []:
        checks.append(
            command_check(
                command,
                criteria=criteria,
                timeout_seconds=getattr(args, "check_timeout", 300),
                required=True,
            )
        )
    for command in getattr(args, "optional_checks", []) or []:
        checks.append(
            command_check(
                command,
                criteria=criteria,
                timeout_seconds=getattr(args, "check_timeout", 300),
                required=False,
            )
        )
    for expression in getattr(args, "invariants", []) or []:
        spec = parse_invariant_expression(expression)
        if criteria:
            spec = spec.model_copy(update={"criteria": criteria})
        checks.append(spec)
    return checks


def register_verification_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    verify = sub.add_parser("verify", help="Run deterministic checks and build an evidence report")
    verify_sub = verify.add_subparsers(dest="verify_command", metavar="<action>")

    verify_sub.add_parser(
        "checks", help="List built-in checks and ML invariant templates"
    ).set_defaults(func=cmd_verify_checks)

    plan = verify_sub.add_parser("plan", help="Show declared checks and suggestions")
    plan.add_argument("identifier", help="Run id (run_…) or task id")
    plan.set_defaults(func=cmd_verify_plan)

    run = verify_sub.add_parser("run", help="Execute a verification attempt")
    run.add_argument("identifier", help="Run id (run_…) or task id")
    run.add_argument("--check", dest="checks", action="append", default=[], help="Required command check")
    run.add_argument(
        "--optional-check",
        dest="optional_checks",
        action="append",
        default=[],
        help="Command check whose failure is recorded but not blocking",
    )
    run.add_argument(
        "--check-criteria",
        help="Comma-separated criterion ids (AC1,AC2) for the checks added here",
    )
    run.add_argument("--check-timeout", type=int, default=300)
    run.add_argument(
        "--invariant",
        dest="invariants",
        action="append",
        default=[],
        help="ML invariant, e.g. forward_smoke:module=attention,callable=build_attention,shape=1x4x32",
    )
    run.add_argument("--no-review", action="store_true", help="Skip the independent model review")
    run.set_defaults(func=cmd_verify_run)

    show = verify_sub.add_parser("show", help="Show the latest verification report")
    show.add_argument("identifier", help="Run id (run_…) or task id")
    show.set_defaults(func=cmd_verify_show)

    report = verify_sub.add_parser("report", help="Write the Markdown report")
    report.add_argument("identifier", help="Run id (run_…) or task id")
    report.add_argument("--out", help="Destination file (default: stdout)")
    report.set_defaults(func=cmd_verify_report)

    bundle = verify_sub.add_parser("bundle", help="Export an evidence bundle")
    bundle.add_argument("identifier", help="Run id (run_…) or task id")
    bundle.add_argument("destination")
    bundle.set_defaults(func=cmd_verify_bundle)

    check_bundle = verify_sub.add_parser(
        "check-bundle", help="Recompute and verify an exported bundle"
    )
    check_bundle.add_argument("directory")
    check_bundle.set_defaults(func=cmd_verify_check_bundle)


# ----------------------------------------------------------------- handlers
def cmd_verify_checks(args: argparse.Namespace) -> int:
    console = args.console
    entries = check_registry.registry_summary()
    if console.json_mode:
        console.json({"checks": entries})
        return 0
    console.heading("Verification checks")
    console.table(
        ["key", "gate", "kind", "required", "description"],
        [
            [
                item["key"],
                item["gate"],
                item["kind"],
                "yes" if item["required"] else "no",
                item["description"],
            ]
            for item in entries
        ],
        max_column=80,
    )
    console.note(
        "Built-in checks always run. Command and invariant checks must be declared "
        "in the contract (`maya task create --check ...`) or added to an attempt "
        "with `maya verify run --check ...`."
    )
    return 0


def cmd_verify_plan(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    declared = svc.verification.declared_checks(run.id)
    proposals = svc.verification.proposals(run.id)
    payload = {
        "run_id": run.id,
        "declared": [item.model_dump(mode="json") for item in declared],
        "proposed": [item.model_dump(mode="json") for item in proposals],
        "builtin": [item.key for item in check_registry.BUILTIN_CHECKS],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Verification plan for {run.id}")
    def command_text(item) -> str:  # type: ignore[no-untyped-def]
        if item.argv:
            return " ".join(item.argv)
        if item.invariant:
            return f"invariant {item.invariant.name} on {item.invariant.module}"
        return ""

    console.section("Declared in the contract")
    if declared:
        console.table(
            ["key", "kind", "required", "criteria", "command"],
            [
                [
                    item.key,
                    item.kind.value,
                    "yes" if item.required else "no",
                    ",".join(item.criteria) or "-",
                    command_text(item),
                ]
                for item in declared
            ],
            max_column=70,
        )
    else:
        console.note(
            "No checks are declared. Verification will report INCONCLUSIVE because the "
            "implementation would be inspected but not validated."
        )
    console.section("Suggestions (not authorized)")
    if proposals:
        console.table(
            ["key", "command"],
            [[item.key, " ".join(item.argv or [])] for item in proposals],
            max_column=70,
        )
    else:
        console.note("No automatic suggestions for this snapshot.")
    console.section("Built-in checks")
    console.print(", ".join(payload["builtin"]))
    return 0


def cmd_verify_run(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    extras = _extra_checks(args)
    report, checks = svc.task_service.verify_run(
        run.id, extra_checks=extras, include_review=not args.no_review
    )
    payload = {
        "report": report.model_dump(mode="json"),
        "checks": [item.model_dump(mode="json") for item in checks],
    }
    if console.json_mode:
        console.json(payload)
    else:
        console.heading(f"Verification {report.decision.value}")
        console.kv(
            [
                ("report", report.id),
                ("attempt", report.verification_id),
                ("checks", len(checks)),
                ("blocking", len(report.blocking)),
                ("limitations", len(report.limitations)),
            ]
        )
        console.section("Checks")
        console.table(
            ["key", "outcome", "required", "summary"],
            [
                [
                    item.key,
                    item.outcome.value,
                    "yes" if item.required else "no",
                    item.summary,
                ]
                for item in checks
            ],
            max_column=70,
        )
        if report.criteria:
            console.section("Acceptance criteria")
            console.table(
                ["id", "status", "evidence"],
                [
                    [item.criterion_id, item.status.value, item.note]
                    for item in report.criteria
                ],
                max_column=70,
            )
        if report.blocking:
            console.section("Blocking reasons")
            for item in report.blocking:
                console.warning(item)
        if report.limitations:
            console.section("Limitations")
            for item in report.limitations:
                console.bullet(item)
        console.note(
            "A passing check describes only what it exercised. See the report for the "
            "difference between implemented, checked and model-assessed claims."
        )
    return (
        0
        if report.decision
        in {TaskDecision.ACCEPTED, TaskDecision.COMPLETED_WITH_LIMITATIONS}
        else 1
    )


def _latest(svc, identifier: str):  # type: ignore[no-untyped-def]
    run = _resolve_run(svc, identifier)
    report = svc.verification.latest_report(run.id)
    if report is None:
        raise UsageError(
            f"Run {run.id} has no verification report",
            hint=f"Run `maya verify run {run.id}`.",
        )
    return run, report


def cmd_verify_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run, report = _latest(svc, args.identifier)
    checks = svc.verification.report_checks(report)
    payload = {
        "run": run.id,
        "report": report.model_dump(mode="json"),
        "checks": [item.model_dump(mode="json") for item in checks],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Verification report for {run.id}")
    console.kv(
        [
            ("decision", report.decision.value),
            ("report", report.id),
            ("attempt", report.verification_id),
            ("created", report.created_at.isoformat()),
            ("json sha256", report.json_sha256),
        ]
    )
    console.section("Summary")
    console.print(report.summary)
    console.section("Checks")
    console.table(
        ["key", "outcome", "required", "summary"],
        [
            [item.key, item.outcome.value, "yes" if item.required else "no", item.summary]
            for item in checks
        ],
        max_column=70,
    )
    if report.limitations:
        console.section("Limitations")
        for item in report.limitations:
            console.bullet(item)
    return 0


def cmd_verify_report(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run, report = _latest(svc, args.identifier)
    markdown_id = report.details.get("markdown_artifact_id")
    if not markdown_id:
        raise UsageError("The report has no Markdown artifact")
    text = svc.artifacts.read_text(markdown_id)
    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if console.json_mode:
            console.json({"path": str(target), "run_id": run.id, "report_id": report.id})
        else:
            console.heading("Wrote verification report")
            console.kv([("path", str(target)), ("decision", report.decision.value)])
        return 0
    if console.json_mode:
        console.json({"markdown": text, "report": report.model_dump(mode="json")})
    else:
        console.print(text)
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    result = svc.verification.export_bundle(run.id, args.destination)
    if console.json_mode:
        console.json(
            {
                "path": str(result.path),
                "manifest": result.manifest,
                "run_id": run.id,
            }
        )
        return 0
    console.heading("Exported evidence bundle")
    console.kv(
        [
            ("path", str(result.path)),
            ("files", len(result.manifest["files"])),
            ("root sha256", result.manifest["root_sha256"]),
        ]
    )
    console.note(
        "Verify it with `maya verify check-bundle <directory>` before treating it as evidence."
    )
    return 0


def cmd_verify_check_bundle(args: argparse.Namespace) -> int:
    console = args.console
    result = verify_bundle(args.directory)
    payload = {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "root_sha256": result.manifest.get("root_sha256"),
        "decision": result.report.get("decision"),
    }
    if console.json_mode:
        console.json(payload)
    else:
        console.heading("Bundle verified" if result.ok else "Bundle FAILED verification")
        console.kv(
            [
                ("directory", str(Path(args.directory).expanduser())),
                ("root sha256", payload["root_sha256"] or "missing"),
                ("decision", payload["decision"] or "missing"),
            ]
        )
        for error in result.errors:
            console.error(error)
        for warning in result.warnings:
            console.warning(warning)
    return 0 if result.ok else 1


__all__ = ["register_verification_commands", "parse_invariant_expression"]

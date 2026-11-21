"""CLI commands for controlled engineering tasks (Phase 3).

Registration lives here so the parser cannot silently lose a workflow area
(MWB-001). Each handler calls the same ``TaskService`` the worker and future API
use; no business logic is implemented in the CLI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mayaworkbench.domain.enums import (
    ApprovalAction,
    ExecutionTrust,
    TaskType,
    ToolCapability,
    WorkflowState,
)
from mayaworkbench.errors import UsageError


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def register_task_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    task = sub.add_parser("task", help="Create, approve and run controlled engineering tasks")
    task_sub = task.add_subparsers(dest="task_command", metavar="<action>")

    reliability = task_sub.add_parser("reliability", help="Show tool-call reliability for a run")
    reliability.add_argument("run_id")
    reliability.set_defaults(func=cmd_task_reliability)
    explain = task_sub.add_parser("explain-params", help="Show model/routing parameters for a run")
    explain.add_argument("run_id")
    explain.set_defaults(func=cmd_task_explain_params)

    create = task_sub.add_parser("create", help="Create a task and its immutable contract")
    create.add_argument("--project")
    create.add_argument("--repo")
    create.add_argument("--snapshot", help="Pinned snapshot id (default: latest indexed)")
    create.add_argument("--title", help="Short task title (default: the goal)")
    create.add_argument("--goal", required=True, help="What the task must achieve")
    create.add_argument("--task-type", choices=[item.value for item in TaskType], default="ENGINEERING_CHANGE")
    create.add_argument("--allow-path", action="append", required=True, help="Repository-relative writable path")
    create.add_argument("--exclude-path", action="append", default=[])
    create.add_argument("--workspace", help="Engineering Workspace id")
    create.add_argument("--resource", action="append", default=[], help="Attached resource id (repeatable)")
    create.add_argument("--acceptance", action="append", default=[])
    create.add_argument("--unknown", action="append", default=[])
    create.add_argument("--max-steps", type=int, default=30)
    create.add_argument("--max-tool-calls", type=int, default=120)
    create.add_argument("--max-tokens", type=int, default=50_000)
    create.add_argument("--max-cost", type=float, default=0.0)
    create.add_argument("--runtime", type=int, default=1800, help="Wall-clock seconds")
    create.add_argument("--max-output", type=int, default=200_000)
    create.add_argument(
        "--network",
        choices=["DENY_BY_DEFAULT", "ALLOW_WITH_APPROVAL"],
        default="DENY_BY_DEFAULT",
        help="Command network policy; DENY_BY_DEFAULT refuses execution without isolation",
    )
    create.add_argument(
        "--capability",
        action="append",
        choices=[item.value for item in ToolCapability],
        default=[],
        help="Grant one tool capability (repeatable); a non-empty list is a closed allowlist",
    )
    create.add_argument(
        "--trust",
        choices=[item.value for item in ExecutionTrust],
        default=ExecutionTrust.LOCAL_TRUSTED.value,
        help="Execution trust profile (isolated profiles are blocked until available)",
    )
    create.add_argument("--allow-delete", action="store_true", help="Permit file deletion")
    create.add_argument(
        "--allow-unknown-cost",
        action="store_true",
        help="Permit model calls whose price is not configured",
    )
    create.add_argument("--document", action="append", default=[], help="Research document id")
    create.add_argument("--claim", action="append", default=[], help="Research claim id")
    create.add_argument("--mapping-artifact", help="Claim-to-code matrix artifact id")
    create.add_argument(
        "--check",
        dest="checks",
        action="append",
        default=[],
        help="Required verification command, e.g. --check 'pytest -q'",
    )
    create.add_argument(
        "--optional-check",
        dest="optional_checks",
        action="append",
        default=[],
        help="Verification command recorded but not blocking",
    )
    create.add_argument(
        "--check-criteria",
        help="Comma-separated criterion ids (AC1,AC2) for the declared checks",
    )
    create.add_argument("--check-timeout", type=int, default=300)
    create.add_argument(
        "--require-experiment",
        action="store_true",
        help=(
            "Gate acceptance on a fair, comparable experiment attached to the task "
            "(Gate F). The comparison must exist before verification runs."
        ),
    )
    create.add_argument(
        "--invariant",
        dest="invariants",
        action="append",
        default=[],
        help="ML invariant, e.g. forward_smoke:module=model,callable=Model,shape=1x4x32",
    )
    create.set_defaults(func=cmd_task_create)

    listing = task_sub.add_parser("list", help="List tasks")
    listing.add_argument("--project")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_task_list)

    show = task_sub.add_parser("show", help="Show a task, its contract and runs")
    show.add_argument("task_id")
    show.set_defaults(func=cmd_task_show)

    start = task_sub.add_parser("start", help="Plan the task and request approvals")
    start.add_argument("task_id")
    start.add_argument("--plan-file", help="Use a reviewed JSON plan instead of a model")
    start.add_argument("--approval-ttl", type=int, default=24 * 3600)
    start.set_defaults(func=cmd_task_start)

    plan = task_sub.add_parser(
        "plan", help="Show the implementation plan or the typed role DAG (JSON)"
    )
    plan.add_argument("identifier", help="Task id or run id")
    plan.add_argument(
        "--roles",
        action="store_true",
        help="Emit the deterministic typed role DAG instead of the implementation plan",
    )
    plan.set_defaults(func=cmd_task_plan)

    roles = task_sub.add_parser(
        "roles", help="List the fixed roles and their default authority"
    )
    roles.set_defaults(func=cmd_task_roles)

    approve = task_sub.add_parser("approve", help="Grant one pending approval")
    approve.add_argument("approval_id")
    approve.add_argument("--reason", default=None)
    approve.set_defaults(func=cmd_task_approve)

    deny = task_sub.add_parser("deny", help="Deny one pending approval")
    deny.add_argument("approval_id")
    deny.add_argument("--reason", required=True)
    deny.set_defaults(func=cmd_task_deny)

    approvals = task_sub.add_parser("approvals", help="List approvals for a run")
    approvals.add_argument("run_id")
    approvals.set_defaults(func=cmd_task_approvals)

    run = task_sub.add_parser("run", help="Execute an approved task in the worker process")
    run.add_argument("task_id")
    run.add_argument("--detach", action="store_true", help="Do not wait for the worker")
    run.add_argument("--timeout", type=int, default=None)
    run.add_argument(
        "--roles",
        action="store_true",
        help="Use the single deterministic role orchestrator instead of the legacy single-role loop",
    )
    run.set_defaults(func=cmd_task_run)

    status = task_sub.add_parser("status", help="Show a run, its approvals, diff and review")
    status.add_argument("identifier", help="Run id (run_…) or task id")
    status.set_defaults(func=cmd_task_status)

    pause = task_sub.add_parser("pause", help="Pause a run at the next safe point")
    pause.add_argument("run_id")
    pause.set_defaults(func=cmd_task_pause)

    resume = task_sub.add_parser("resume", help="Resume a paused run")
    resume.add_argument("run_id")
    resume.set_defaults(func=cmd_task_resume)

    cancel = task_sub.add_parser("cancel", help="Cancel a run")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(func=cmd_task_cancel)

    diff = task_sub.add_parser("diff", help="Capture and show the candidate patch")
    diff.add_argument("identifier", help="Run id (run_…) or task id")
    diff.add_argument("--output", help="Also write the patch to this path")
    diff.set_defaults(func=cmd_task_diff)

    review = task_sub.add_parser("review", help="Run the structural diff review")
    review.add_argument("identifier", help="Run id (run_…) or task id")
    review.set_defaults(func=cmd_task_review)

    finish = task_sub.add_parser("finish", help="Record the user's accept/reject decision")
    finish.add_argument("identifier", help="Run id (run_…) or task id")
    decision = finish.add_mutually_exclusive_group(required=True)
    decision.add_argument("--accept", action="store_true")
    decision.add_argument("--reject", action="store_true")
    finish.add_argument("--reason", default="reviewed by the user")
    finish.add_argument(
        "--override",
        action="store_true",
        help=(
            "Record a human override of a negative verification decision. The report "
            "is not changed; the override is stored as a limitation and an event."
        ),
    )
    finish.set_defaults(func=cmd_task_finish)

    cleanup = task_sub.add_parser("cleanup", help="Remove an isolated workspace")
    cleanup.add_argument("run_id")
    cleanup.set_defaults(func=cmd_task_cleanup)

    export = task_sub.add_parser("export", help="Export a task evidence bundle")
    export.add_argument("identifier", help="Run id (run_…) or task id")
    export.add_argument("destination")
    export.set_defaults(func=cmd_task_export)


# --------------------------------------------------------------------- helpers
def _resolve_run(svc, identifier: str):  # type: ignore[no-untyped-def]
    if identifier.startswith("run_"):
        return svc.runs.get(identifier)
    run = svc.task_service.latest_run_for_task(identifier)
    if run is None:
        raise UsageError(
            f"Task {identifier} has no run",
            hint="Run `maya task start` first.",
        )
    return run


def cmd_task_reliability(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = svc.runs.get(args.run_id)
    from mayaworkbench.tasks.llm_capabilities import tool_reliability

    scores = tool_reliability(run.details)
    payload = {
        "run_id": run.id,
        "tools": {name: item.__dict__ for name, item in scores.items()},
    }
    if console.json_mode:
        console.json(payload)
    else:
        console.table(
            ["tool", "success", "failure", "blocked", "score"],
            [
                (name, item.success, item.failure, item.blocked, item.score)
                for name, item in scores.items()
            ],
        )
    return 0


def cmd_task_explain_params(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = svc.runs.get(args.run_id)
    details = run.details or {}
    from mayaworkbench.tasks.llm_capabilities import route_task

    route = route_task(
        details.get("task_type", "ENGINEERING_CHANGE"),
        uncertainty=float(details.get("uncertainty", 0.0) or 0.0),
        context_tokens=int(details.get("context_tokens", 0) or 0),
        data_policy=svc.settings.data_policy.value if hasattr(svc.settings.data_policy, "value") else str(svc.settings.data_policy),
    )
    payload = {
        "run_id": run.id,
        "route": route.__dict__,
        "model_params": details.get("model_params", {}),
        "parse_errors": details.get("agent_parse_errors", []),
    }
    console.json(payload) if console.json_mode else console.print(json.dumps(payload, indent=2))
    return 0


def _task_type(value: str) -> TaskType:
    return TaskType(value)


# -------------------------------------------------------------------- create
def cmd_task_create(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    _project, _repository, snapshot = svc.resolve_snapshot_context(
        project_identifier=args.project,
        repository_identifier=args.repo,
        snapshot_id=args.snapshot,
        require_indexed=True,
    )
    constraints: dict[str, Any] = {
        "max_steps": args.max_steps,
        "max_tool_calls": args.max_tool_calls,
        "max_model_tokens": args.max_tokens,
        "max_model_cost_usd": args.max_cost,
        "max_runtime_seconds": args.runtime,
        "max_output_bytes": args.max_output,
        "network_access": args.network,
        "capabilities": list(args.capability or []),
        "execution_trust": args.trust,
        "allow_file_deletion": bool(args.allow_delete),
        "allow_unknown_cost": bool(args.allow_unknown_cost),
    }
    from mayaworkbench.verification.registry import command_check
    from mayaworkbench.cli.verification_commands import parse_invariant_expression

    criteria_refs = [
        item.strip().upper()
        for item in (args.check_criteria or "").split(",")
        if item.strip()
    ]
    checks = []
    for command in args.checks:
        checks.append(
            command_check(
                command,
                criteria=criteria_refs,
                timeout_seconds=args.check_timeout,
                required=True,
            )
        )
    for command in args.optional_checks:
        checks.append(
            command_check(
                command,
                criteria=criteria_refs,
                timeout_seconds=args.check_timeout,
                required=False,
            )
        )
    for expression in args.invariants:
        spec = parse_invariant_expression(expression)
        if criteria_refs:
            spec = spec.model_copy(update={"criteria": criteria_refs})
        checks.append(spec)
    task, contract = svc.task_service.create_task(
        project_id=_project.id,
        title=args.title or args.goal[:120],
        goal=args.goal,
        task_type=_task_type(args.task_type),
        snapshot_id=snapshot.id,
        allowed_paths=args.allow_path,
        excluded_paths=args.exclude_path,
        workspace_id=args.workspace,
        resource_ids=list(args.resource or []),
        acceptance_criteria=args.acceptance or None,
        constraints=constraints,
        unknowns=args.unknown or None,
        research_document_ids=list(args.document),
        claim_ids=list(args.claim),
        mapping_artifact_id=args.mapping_artifact,
        verification_checks=checks,
        require_experiment=args.require_experiment,
    )
    payload = {"task": task.model_dump(mode="json"), "contract": contract.model_dump(mode="json")}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Created task {task.title}")
    console.kv(
        [
            ("task", task.id),
            ("contract", contract.id),
            ("contract sha256", contract.content_sha256),
            ("snapshot", contract.snapshot_id),
            ("task type", contract.task_type.value),
            ("scope", ", ".join(contract.scope.allowed_paths)),
            ("network", contract.constraints.network_access),
            ("approvals", ", ".join(item.value for item in contract.required_approvals)),
            ("verification checks", len(contract.verification_checks) or "none declared"),
            (
                "experiment required",
                "yes (Gate F)" if contract.require_experiment else "no",
            ),
        ]
    )
    if not contract.verification_checks:
        console.warning(
            "No verification checks are declared. Verification will report INCONCLUSIVE "
            "for this task; add --check 'pytest -q' (or an invariant) to validate behaviour."
        )
    console.print("")
    console.print(f"Next: maya task start {task.id}")
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    tasks = svc.task_service.list_tasks(project_id=project.id, limit=args.limit)
    rows = [
        {
            "id": task.id,
            "title": task.title,
            "type": task.task_type.value,
            "created": task.created_at.isoformat(),
        }
        for task in tasks
    ]
    if console.json_mode:
        console.json({"tasks": rows})
        return 0
    console.heading(f"Tasks in {project.slug}")
    console.table(
        ["id", "title", "type", "created"],
        [[row["id"], row["title"], row["type"], row["created"][:19]] for row in rows],
        max_column=60,
    )
    if not rows:
        console.note("No tasks yet. Create one with `maya task create --goal ... --allow-path ...`.")
    return 0


def cmd_task_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    payload = svc.task_service.task_view(args.task_id)
    if console.json_mode:
        console.json(payload)
        return 0
    task = payload["task"]
    contract = payload["contract"]
    console.heading(f"{task['title']}")
    console.kv(
        [
            ("task", task["id"]),
            ("goal", task["goal"]),
            ("type", task["task_type"]),
            ("contract", contract["id"]),
            ("snapshot", contract["snapshot_id"]),
            ("scope", ", ".join(contract["scope"]["allowed_paths"])),
        ]
    )
    console.section("Runs")
    console.table(
        ["run", "state", "steps", "tool calls"],
        [
            [run["id"], run["state"], run["step_count"], run["tool_call_count"]]
            for run in payload["runs"]
        ],
    )
    return 0


def cmd_task_start(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    result = svc.task_service.start_task(
        args.task_id,
        plan_file=args.plan_file,
        approval_ttl_seconds=args.approval_ttl,
    )
    payload = {
        "task": result.task.model_dump(mode="json"),
        "contract": result.contract.model_dump(mode="json"),
        "run": result.run.model_dump(mode="json"),
        "plan": result.plan.model_dump(mode="json"),
        "approvals": [item.model_dump(mode="json") for item in result.approvals],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Run {result.run.id} is awaiting approval")
    console.kv(
        [
            ("state", result.run.state.value),
            ("plan", result.plan.id),
            ("plan sha256", result.plan.plan_sha256),
            ("summary", result.plan.summary),
        ]
    )
    console.section("Approvals")
    console.table(
        ["approval", "action", "expires"],
        [
            [item.id, item.action.value, item.expires_at.isoformat()]
            for item in result.approvals
        ],
    )
    console.print("")
    for item in result.approvals:
        console.print(f"  maya task approve {item.id}")
    return 0


def cmd_task_plan(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    if args.roles:
        from mayaworkbench.tasks.orientation import build_orientation
        from mayaworkbench.tasks.roles import build_role_plan

        if args.identifier.startswith("run_"):
            run = svc.runs.get(args.identifier)
            task = svc.tasks.get(run.task_id)
            contract = svc.contracts.get(run.contract_id)
        else:
            task = svc.tasks.get(args.identifier)
            contract = svc.contracts.latest(task.id)
        orientation: dict[str, Any] | None = None
        try:
            orientation = build_orientation(
                retrieval=svc.retrieval,
                files=svc.files,
                symbols=svc.symbols,
                issues=svc.issues,
                snapshot_id=contract.snapshot_id,
                goal=task.goal,
                allowed_paths=contract.scope.allowed_paths,
                excluded_paths=contract.scope.excluded_paths,
            )
        except Exception:  # noqa: BLE001 - orientation is an aid, never a gate
            orientation = None
        role_plan = build_role_plan(task, contract, orientation)
        console.json({"role_plan": role_plan.to_json()})
        return 0

    run = _resolve_run(svc, args.identifier)
    plan = (
        svc.plans.get(run.plan_id)
        if run.plan_id
        else svc.plans.latest_for_run(run.id)
    )
    console.json({"plan": plan.model_dump(mode="json") if plan else None})
    return 0


def cmd_task_roles(args: argparse.Namespace) -> int:
    console = args.console
    from mayaworkbench.tasks.roles import (
        ROLE_AUTHORITY,
        ROLE_MODEL_LABEL,
        ROLE_OUTPUT,
        AgentRole,
    )

    rows = [
        {
            "role": role.value,
            "output_artifact": ROLE_OUTPUT[role],
            "authority": ROLE_AUTHORITY[role],
            "model_label": ROLE_MODEL_LABEL[role],
        }
        for role in AgentRole
    ]
    console.json(
        {
            "roles": rows,
            "orchestrator": "one deterministic orchestrator",
            "swarm": False,
            "note": (
                "Authority is descriptive. Grants come from the immutable contract "
                "and consumed approvals; only the backend changes workflow state."
            ),
        }
    )
    return 0


def cmd_task_approve(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    approval = svc.task_service.approve(args.approval_id, reason=args.reason)
    if console.json_mode:
        console.json({"approval": approval.model_dump(mode="json")})
        return 0
    console.heading(f"Approved {approval.action.value}")
    console.kv([("approval", approval.id), ("status", approval.status.value)])
    return 0


def cmd_task_deny(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    approval = svc.task_service.deny(args.approval_id, reason=args.reason)
    if console.json_mode:
        console.json({"approval": approval.model_dump(mode="json")})
        return 0
    console.heading(f"Denied {approval.action.value}")
    console.kv([("approval", approval.id), ("status", approval.status.value)])
    return 0


def cmd_task_approvals(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    items = svc.task_service.approvals_for_run(args.run_id)
    payload = {"approvals": [item.model_dump(mode="json") for item in items]}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Approvals")
    console.table(
        ["approval", "action", "status", "expires"],
        [
            [item.id, item.action.value, item.status.value, item.expires_at.isoformat()]
            for item in items
        ],
    )
    return 0


def cmd_task_run(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    if getattr(args, "roles", False):
        from mayaworkbench.tasks.orchestrator import set_role_plan_enabled

        run = _resolve_run(svc, args.task_id)
        set_role_plan_enabled(svc.runs, run.id, enabled=True)
    result = svc.task_service.run_task(
        args.task_id, detached=args.detach, timeout_seconds=args.timeout
    )
    payload = {
        "run": result.run.model_dump(mode="json"),
        "job": result.job.model_dump(mode="json"),
        "log_path": result.log_path,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Run {result.run.id}: {result.run.state.value}")
    console.kv(
        [
            ("steps", result.run.step_count),
            ("tool calls", result.run.tool_call_count),
            ("tokens", result.run.model_tokens),
            ("cost", f"${result.run.model_cost_usd:.6f}"),
            ("worker log", result.log_path),
            ("error", result.run.error or "—"),
        ]
    )
    if result.run.state == WorkflowState.REVIEWING:
        console.print("")
        console.print(f"Next: maya task diff {result.run.id} && maya task review {result.run.id}")
    return 0


def cmd_task_status(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    payload = svc.task_service.run_view(run.id)
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Run {run.id}: {run.state.value}")
    console.kv(
        [
            ("task", run.task_id),
            ("steps", run.step_count),
            ("tool calls", run.tool_call_count),
            ("tokens", run.model_tokens),
            ("cost", f"${run.model_cost_usd:.6f}"),
            ("error", run.error or "—"),
        ]
    )
    for approval in payload["approvals"]:
        console.bullet(f"{approval['action']}: {approval['status']} ({approval['id']})")
    rounds = (run.details or {}).get("in_loop_check_rounds") or []
    if rounds:
        console.section("In-loop declared checks (advisory)")
        console.table(
            ["round", "verdict", "duration_ms", "checks"],
            [
                [
                    item.get("round"),
                    item.get("verdict"),
                    item.get("duration_ms"),
                    ", ".join(
                        f"{check.get('key')}={check.get('outcome')}"
                        for check in item.get("checks", [])
                    ),
                ]
                for item in rounds
            ],
            max_column=60,
        )
        console.note(
            "In-loop checks guide repair. `maya verify run` re-executes them and is the evidence."
        )
    if payload["diff"]:
        console.section("Candidate patch")
        console.kv(
            [
                ("files", len(payload["diff"]["files"])),
                ("additions", payload["diff"]["additions"]),
                ("deletions", payload["diff"]["deletions"]),
                ("sha256", payload["diff"]["patch_sha256"]),
            ]
        )
    return 0


def cmd_task_pause(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = svc.task_service.pause_run(args.run_id)
    if console.json_mode:
        console.json({"run": run.model_dump(mode="json")})
        return 0
    console.heading(f"Pause requested for {run.id}")
    console.kv([("state", run.state.value)])
    return 0


def cmd_task_resume(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    result = svc.task_service.resume_run(args.run_id)
    if console.json_mode:
        console.json({"run": result.run.model_dump(mode="json"), "job": result.job.model_dump(mode="json")})
        return 0
    console.heading(f"Resumed {result.run.id}")
    console.kv([("state", result.run.state.value), ("job", result.job.id)])
    return 0


def cmd_task_cancel(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = svc.task_service.cancel_run(args.run_id, reason=args.reason)
    if console.json_mode:
        console.json({"run": run.model_dump(mode="json")})
        return 0
    console.heading(f"Cancelled {run.id}")
    console.kv([("state", run.state.value), ("reason", args.reason)])
    return 0


def cmd_task_diff(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    diff = svc.task_service.capture_diff(run.id)
    patch = svc.artifacts.read_text(diff.patch_artifact_id)
    if args.output:
        target = Path(args.output).expanduser()
        target.write_text(patch, encoding="utf-8")
    payload = {
        "run": svc.runs.get(run.id).model_dump(mode="json"),
        "diff": diff.model_dump(mode="json"),
        "patch": patch,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Candidate patch for {run.id}")
    console.kv(
        [
            ("files", len(diff.files)),
            ("additions", diff.additions),
            ("deletions", diff.deletions),
            ("sha256", diff.patch_sha256),
            ("original checkout unchanged", "yes" if diff.original_checkout_unchanged else "NO"),
        ]
    )
    console.table(
        ["path", "status", "+", "-"],
        [[item.path, item.status, item.additions, item.deletions] for item in diff.files],
        max_column=60,
    )
    for warning in diff.warnings:
        console.warning(warning)
    if not console.json_mode and not args.output:
        console.section("Patch")
        console.print(patch)
    return 0


def cmd_task_review(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    review = svc.task_service.review_run(run.id)
    payload = {"review": review.model_dump(mode="json")}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Structural review for {run.id}")
    console.kv(
        [
            ("scope ok", "yes" if review.scope_ok else "NO"),
            ("secrets found", "yes" if review.secrets_found else "no"),
            ("tests modified", "yes" if review.tests_modified else "no"),
            ("dependencies changed", "yes" if review.dependencies_changed else "no"),
            ("findings", len(review.findings)),
        ]
    )
    console.table(
        ["severity", "code", "message"],
        [[item.severity, item.code, item.message] for item in review.findings],
        max_column=70,
    )
    console.note(
        "Structural review only; it is not deterministic verification. Run "
        "`maya verify run <run>` for execution evidence."
    )
    return 0


def cmd_task_finish(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    updated = svc.task_service.finish_run(
        run.id,
        accept=bool(args.accept),
        reason=args.reason,
        override=bool(getattr(args, "override", False)),
    )
    if console.json_mode:
        console.json({"run": updated.model_dump(mode="json")})
        return 0
    console.heading(f"Run {updated.id}: {updated.state.value}")
    console.kv([("reason", args.reason)])
    report = svc.verification.latest_report(updated.id)
    if report is not None:
        console.kv(
            [
                ("verification decision", report.decision.value),
                ("verification report", report.id),
                ("override", "yes" if report.decision.value not in {"ACCEPTED", "COMPLETED_WITH_LIMITATIONS"} else "no"),
            ]
        )
    if updated.state == WorkflowState.COMPLETED_WITH_LIMITATIONS:
        console.note(
            "Accepted with limitations; inspect `maya verify show " + updated.id + "`."
        )
    return 0


def cmd_task_cleanup(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    workspace = svc.task_service.cleanup_run(args.run_id)
    if console.json_mode:
        console.json({"workspace": workspace.model_dump(mode="json")})
        return 0
    console.heading(f"Workspace {workspace.status.value}")
    console.kv([("workspace", workspace.id), ("path", workspace.path)])
    return 0


def cmd_task_export(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    run = _resolve_run(svc, args.identifier)
    target = svc.task_service.export_run(run.id, args.destination)
    payload = {"exported_to": str(target), "run": run.model_dump(mode="json")}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Exported task bundle")
    console.kv([("path", str(target)), ("run", run.id)])
    return 0


__all__ = ["register_task_commands"]

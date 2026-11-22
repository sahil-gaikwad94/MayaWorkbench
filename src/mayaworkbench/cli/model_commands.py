"""CLI commands for the WP7 model capability platform and connectors.

Registration is defensive: the legacy ``model status`` parser may already exist
in ``cli/main.py``. This module reuses it instead of adding a duplicate parser.
Handlers use ``args.console`` JSON mode like the other command areas and never
emit key material. Connector commands perform no network calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mayaworkbench.errors import UsageError
from mayaworkbench.models.capabilities import (
    TASK_TYPES,
    HashingEmbeddingModel,
    LexicalOverlapReranker,
    UnsupportedVisionModel,
)
from mayaworkbench.models.connectors import build_default_connector_registry
from mayaworkbench.models.local_openai_compatible import LOCAL_OPENAI_COMPATIBLE, build_local_provider
from mayaworkbench.models.routing import (
    ProviderHealthTracker,
    RouteRequest,
    default_router,
    explain_route,
    provider_specs_from_settings,
)


def _get_or_add_group(
    sub: argparse._SubParsersAction, name: str, help_text: str, dest: str  # type: ignore[type-arg]
) -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:  # type: ignore[type-arg]
    parser = sub.choices.get(name) if hasattr(sub, "choices") else None
    if parser is None:
        parser = sub.add_parser(name, help=help_text)
    nested = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    if nested is None:
        nested = parser.add_subparsers(dest=dest, metavar="<action>")
    return parser, nested  # type: ignore[return-value]


def _add_action(
    subparsers: argparse._SubParsersAction, name: str, **kwargs: Any  # type: ignore[type-arg]
) -> argparse.ArgumentParser:
    if name in subparsers.choices:
        return subparsers.choices[name]
    return subparsers.add_parser(name, **kwargs)


def register_model_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``model`` and ``connector`` command groups."""
    _model, model_sub = _get_or_add_group(
        sub, "model", "Model capability platform", "model_command"
    )
    _add_action(model_sub, "list", help="List capability interfaces and configured providers").set_defaults(
        func=cmd_model_list
    )
    route, route_sub = _get_or_add_group(
        model_sub, "route", "Explain provider routing decisions", "route_command"
    )
    _add_action(
        route_sub,
        "explain",
        help="Deterministically explain a route for a task type",
    ).set_defaults(func=cmd_model_route_explain)
    explain = route_sub.choices["explain"]
    explain.add_argument("--task-type", choices=list(TASK_TYPES), default="general")
    explain.add_argument("--context-tokens", type=int, default=0)
    explain.add_argument("--uncertain", action="store_true")
    explain.add_argument("--token-budget", type=int)
    explain.add_argument(
        "--data-policy", choices=["LOCAL_ONLY", "BYOK_REMOTE_MODEL"], default=None
    )
    explain.add_argument("--require-structured-output", action="store_true")
    _add_action(model_sub, "health", help="Show provider failure/latency history").set_defaults(
        func=cmd_model_health
    )

    _connector, connector_sub = _get_or_add_group(
        sub, "connector", "External read-only connectors", "connector_command"
    )
    test = _add_action(connector_sub, "test", help="Check local connector configuration (no network)")
    test.add_argument("name")
    test.set_defaults(func=cmd_connector_test)
    revoke = _add_action(connector_sub, "revoke", help="Revoke a connector in this process")
    revoke.add_argument("name")
    revoke.set_defaults(func=cmd_connector_revoke)
    _add_action(connector_sub, "list", help="List registered connectors").set_defaults(
        func=cmd_connector_list
    )
    link = _add_action(
        connector_sub, "mlflow-link", help="Fetch an MLflow run and link it as advisory evidence"
    )
    link.add_argument("mlflow_run_id")
    link.add_argument("--task", required=True)
    link.add_argument("--maya-run", required=True)
    link.set_defaults(func=cmd_connector_mlflow_link)
    approve = _add_action(
        connector_sub, "approve", help="Record a one-time, spec-hash-bound consent grant"
    )
    approve.add_argument("name")
    approve.add_argument("operation")
    approve.add_argument("--param", action="append", default=[])
    approve.add_argument("--actor", default="user")
    approve.set_defaults(func=cmd_connector_approve)
    invoke = _add_action(
        connector_sub, "invoke", help="Invoke an approved connector operation"
    )
    invoke.add_argument("name")
    invoke.add_argument("operation")
    invoke.add_argument("--param", action="append", default=[])
    invoke.set_defaults(func=cmd_connector_invoke)


# ---------------------------------------------------------------------- utils
def _context(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.config import AppPaths, load_settings

    paths = AppPaths.resolve(getattr(args, "home", None))
    return load_settings(paths), paths


def _health_path(paths: Any) -> Path:
    return Path(paths.home) / "models" / "health.json"


# ---------------------------------------------------------------------- model
def cmd_model_list(args: argparse.Namespace) -> int:
    console = args.console
    settings, _paths = _context(args)
    providers = provider_specs_from_settings(settings)
    local_provider = build_local_provider(settings)
    local_adapter = local_provider.describe()
    chat_capabilities = local_provider.capabilities()
    payload: dict[str, Any] = {
        "interfaces": {
            "chat_model": {
                "structured_output": chat_capabilities.structured_output,
                "streaming": chat_capabilities.streaming,
                "tool_calling": chat_capabilities.tool_calling,
                "parallel_tool_calls": chat_capabilities.parallel_tool_calls,
                "provider_family": chat_capabilities.provider_family,
                "note": "Extends ModelProvider; workbench owns prompts and validation.",
            },
            "embedding_model": HashingEmbeddingModel().describe(),
            "reranker": LexicalOverlapReranker().describe(),
            "vision_model": UnsupportedVisionModel().describe(),
            "tool_connector": {
                "registry": "in_process",
                "policy_authority": "workbench_policy",
                "note": "Model-proposed calls are refused unless the allowlist permits them.",
            },
        },
        "local_adapter": local_adapter,
        "providers": [spec.model_dump(mode="json") for spec in providers],
        "data_policy": settings.data_policy,
        "local_mode": LOCAL_OPENAI_COMPATIBLE,
        "key_values_emitted": False,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Model capabilities")
    console.kv(
        [
            ("data policy", settings.data_policy),
            ("chat", "structured output; tool calls; SSE streaming via request.stream"),
            ("embedding", "local hashing fallback (lexical, deterministic)"),
            ("reranker", "local lexical overlap fallback (deterministic)"),
            ("vision", "unsupported — no provider adapter shipped"),
            ("local mode", LOCAL_OPENAI_COMPATIBLE),
        ]
    )
    console.section("Configured providers")
    console.table(
        ["name", "provider", "model", "mode", "tier", "key"],
        [
            [
                spec.name,
                spec.provider if hasattr(spec, "provider") else spec.name,
                spec.model,
                spec.mode,
                spec.tier,
                "present" if spec.key_present else "absent",
            ]
            for spec in providers
        ],
        max_column=40,
    )
    console.note("Key values are never printed, stored in results or sent to other tools.")
    return 0


def cmd_model_route_explain(args: argparse.Namespace) -> int:
    console = args.console
    settings, paths = _context(args)
    token_budget = args.token_budget or settings.retrieval.default_token_budget
    data_policy = args.data_policy or settings.data_policy
    if args.context_tokens < 0:
        raise UsageError("--context-tokens cannot be negative")
    request = RouteRequest(
        task_type=args.task_type,
        context_tokens=args.context_tokens,
        uncertain=bool(args.uncertain),
        token_budget=token_budget,
        data_policy=data_policy,
        require_structured_output=bool(args.require_structured_output),
    )
    router = default_router(settings, health_path=_health_path(paths))
    payload = explain_route(request, router)
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Route explanation: {request.task_type}")
    console.kv(
        [
            ("eligible", "yes" if payload["eligible"] else "no"),
            ("selected", (payload["decision"] or {}).get("provider", "—")),
            ("data policy", data_policy),
            ("context tokens", request.context_tokens),
            ("token budget", token_budget),
            ("policy authority", payload["policy_authority"]),
        ]
    )
    for step in payload["steps"]:
        console.bullet(step)
    if payload["reason"]:
        console.warning(payload["reason"])
    return 0


def cmd_model_health(args: argparse.Namespace) -> int:
    console = args.console
    settings, paths = _context(args)
    tracker = ProviderHealthTracker(path=_health_path(paths))
    for spec in provider_specs_from_settings(settings):
        tracker.get(spec.name)
    payload = tracker.snapshot()
    payload["note"] = (
        "Local history only: provider names, outcomes, latencies and error classes; "
        "no keys, prompts or payloads are stored."
    )
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Provider health")
    console.kv(
        [
            ("max consecutive failures", payload["thresholds"]["max_consecutive_failures"]),
            ("max avg latency (ms)", payload["thresholds"]["max_avg_latency_ms"] or "unbounded"),
        ]
    )
    console.table(
        ["provider", "observations", "failures", "avg ms", "last error"],
        [
            [
                name,
                item["observations"],
                item["failures"],
                item["average_latency_ms"],
                item["last_error_class"] or "—",
            ]
            for name, item in sorted(payload["providers"].items())
        ],
        max_column=30,
    )
    console.note(payload["note"])
    return 0


# ------------------------------------------------------------------ connector
def _advanced_registry():  # type: ignore[no-untyped-def]
    from mayaworkbench.config import AppPaths, load_settings
    from mayaworkbench.models.connectors.registry import build_advanced_connector_registry

    paths = AppPaths.resolve(None)
    settings = load_settings(paths)
    del settings  # connector configuration is intentionally environment-driven
    return build_advanced_connector_registry(
        db_path=str(paths.db_path), log_paths=(str(paths.logs_dir),)
    )


def _parse_params(values: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise UsageError(f"--param expects k=v, got {raw!r}")
        key, value = raw.split("=", 1)
        params[key] = value
    return params


def cmd_connector_approve(args: argparse.Namespace) -> int:
    console = args.console
    registry = _advanced_registry()
    grant = registry.approve(
        args.name, args.operation, _parse_params(args.param), actor=args.actor
    )
    payload = {"approval_id": grant.approval_id, "spec_sha256": grant.spec_sha256}
    if console.json_mode:
        console.json(payload)
    else:
        console.print(f"Approved {args.operation} on {args.name}: {grant.approval_id}")
    return 0


def cmd_connector_invoke(args: argparse.Namespace) -> int:
    console = args.console
    registry = _advanced_registry()
    try:
        result = registry.invoke_with_consent(
            args.name, args.operation, _parse_params(args.param)
        )
    except Exception as exc:  # noqa: BLE001
        console.error(str(exc), code="CONNECTOR_ERROR")
        return 1
    if console.json_mode:
        console.json(result.model_dump(mode="json"))
    else:
        console.print(f"{result.connector}/{result.operation}: {result.target}")
    return 0


def cmd_connector_mlflow_link(args: argparse.Namespace) -> int:
    """Link a fetched MLflow run to a Maya task/run as advisory evidence."""
    console = args.console
    from mayaworkbench.api.validation import ID_RE
    from mayaworkbench.clock import utcnow
    from mayaworkbench.cli.main import services
    from mayaworkbench.domain.enums import ActorKind, ArtifactKind, EventType
    from mayaworkbench.domain.models import WorkflowEvent
    from mayaworkbench.ids import new_ulid

    if not ID_RE.fullmatch(args.task) or not ID_RE.fullmatch(args.maya_run):
        console.error("task and Maya run ids must be valid record ids", code="INVALID_ID")
        return 2
    svc = services(args)
    run = svc.runs.get(args.maya_run)
    task = svc.tasks.get(args.task)
    if run.task_id != task.id:
        console.error("the Maya run does not belong to the given task", code="SCOPE_MISMATCH")
        return 2
    registry = _advanced_registry()
    try:
        result = registry.invoke_with_consent(
            "mlflow", "get_run", {"run_id": args.mlflow_run_id}
        )
    except Exception as exc:  # noqa: BLE001
        console.error(str(exc), code="CONNECTOR_ERROR")
        return 1
    payload = {
        "mlflow": result.model_dump(mode="json"),
        "maya": {"task_id": task.id, "run_id": run.id, "project_id": run.project_id},
        "linked_at": utcnow().isoformat(),
        "authority": "advisory_external_evidence",
    }
    artifact = svc.artifacts.put_text(
        json.dumps(payload, indent=2, sort_keys=True),
        kind=ArtifactKind.OTHER,
        media_type="application/json",
        created_by="connector:mlflow",
    )
    svc.events.append(
        WorkflowEvent(
            event_id="evt_" + new_ulid(),
            event_type=EventType.ARTIFACT_CREATED,
            project_id=run.project_id,
            task_id=task.id,
            run_id=run.id,
            entity_type="mlflow_link",
            entity_id=artifact.id,
            actor_kind=ActorKind.SYSTEM,
            actor_name="SYSTEM:connector-mlflow",
            payload={
                "mlflow_run_id": args.mlflow_run_id,
                "artifact_id": artifact.id,
                "response_hash": result.response_hash,
            },
        )
    )
    if console.json_mode:
        console.json({"artifact_id": artifact.id, "result": result.model_dump(mode="json")})
    else:
        console.print(f"Linked MLflow run {args.mlflow_run_id} → artifact {artifact.id}")
    return 0


def cmd_connector_list(args: argparse.Namespace) -> int:
    console = args.console
    registry = build_default_connector_registry()
    payload = {"connectors": registry.describe()}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Registered connectors")
    console.table(
        ["name", "mode", "operations", "token", "writes"],
        [
            [
                item["name"],
                item["mode"],
                ", ".join(item["operations"]),
                "present" if item["token_present"] else "absent",
                "yes" if item["writes_supported"] else "no",
            ]
            for item in payload["connectors"]
        ],
        max_column=55,
    )
    console.note("Connectors are read-only here; no model-proposed side effect is authorised.")
    return 0


def cmd_connector_test(args: argparse.Namespace) -> int:
    console = args.console
    registry = build_default_connector_registry()
    connector = registry.get(args.name)
    description = connector.describe()
    configured = bool(description.get("token_present")) and not description.get("revoked")
    payload: dict[str, Any] = {
        "connector": args.name,
        "status": "CONFIG_OK" if configured else "NOT_CONFIGURED",
        "network_call": False,
        "description": description,
        "hint": (
            "Exported read-only token detected; no request was sent."
            if configured
            else f"No token detected in ${description.get('token_env')}. Set it before use."
        ),
        "note": (
            "This is a local configuration/allowlist check only; it never contacts GitHub. "
            "Connector calls require an injected HTTP function in tests."
        ),
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Connector configuration: {args.name}")
    console.kv(
        [
            ("status", payload["status"]),
            ("mode", description.get("mode", "—")),
            ("token", "present" if description.get("token_present") else "absent"),
            ("allowed hosts", ", ".join(description.get("allowed_hosts", []))),
        ]
    )
    console.note(payload["note"])
    return 0


def cmd_connector_revoke(args: argparse.Namespace) -> int:
    console = args.console
    registry = build_default_connector_registry()
    registry.revoke(args.name)
    payload: dict[str, Any] = {
        "revoked": args.name,
        "status": "REVOKED_IN_PROCESS",
        "note": (
            "In-memory token and cached results were cleared for this process. "
            "There is no durable connector credential store in WP7; remove the "
            "environment token to revoke it permanently."
        ),
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Revoked connector: {args.name}")
    console.note(payload["note"])
    return 0


__all__ = ["register_model_commands"]

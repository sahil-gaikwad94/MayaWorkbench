"""Typed roles and the deterministic role DAG (WP8, audit §9.1–§9.2).

This module deliberately models **one** orchestrator with role-specific typed
handoffs. It is not a swarm: no role invents its own objective or tools, no role
may silently change workflow state, and only the steps relevant to a task type
are present in the plan.

The DAG is a pure function of the immutable task, contract and (optional)
orientation snapshot. Building it twice produces the same ``plan_sha256``.
Independent analysis steps (research + workspace) are marked parallelisable;
implementation waits for their typed outputs. An independent reviewer receives
the contract, patch and deterministic results/evidence — never the implementer's
self-evaluation as its only context.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from mayaworkbench.domain.enums import ApprovalAction, StrEnum, TaskType, ToolCapability
from mayaworkbench.domain.models import Record, Task, TaskContract


class AgentRole(StrEnum):
    """The fixed role vocabulary. A plan never invents a role."""

    TASK_ANALYST = "task_analyst"
    RESEARCH_ANALYST = "research_analyst"
    WORKSPACE_ANALYST = "workspace_analyst"
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    ENVIRONMENT_ENGINEER = "environment_engineer"
    QA_TEST_ENGINEER = "qa_test_engineer"
    SECURITY_REVIEWER = "security_reviewer"
    EXPERIMENT_ANALYST = "experiment_analyst"
    INDEPENDENT_REVIEWER = "independent_reviewer"
    RELEASE_COORDINATOR = "release_coordinator"


class StepState(StrEnum):
    """Lifecycle of one role step. Backend-owned, never model-declared."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


#: Default authority from audit §9.1. This is documentation carried into the
#: typed artifact; it is not a capability grant. Grants still come from the
#: immutable contract and the consumed approvals.
ROLE_AUTHORITY: dict[AgentRole, str] = {
    AgentRole.TASK_ANALYST: "No writes or external actions",
    AgentRole.RESEARCH_ANALYST: "Read-only research sources",
    AgentRole.WORKSPACE_ANALYST: "Read-only workspace",
    AgentRole.ARCHITECT: "No direct writes",
    AgentRole.IMPLEMENTER: "Approved isolated write scope",
    AgentRole.ENVIRONMENT_ENGINEER: "Runtime-specific capabilities",
    AgentRole.QA_TEST_ENGINEER: "Test runtime; no weakening baseline tests",
    AgentRole.SECURITY_REVIEWER: "Read-only by default",
    AgentRole.EXPERIMENT_ANALYST: "Read-only metrics/artifacts",
    AgentRole.INDEPENDENT_REVIEWER: "Read-only diff and logs",
    AgentRole.RELEASE_COORDINATOR: "Draft only; publishing requires human approval",
}

#: Name of the typed artifact each role is expected to emit.
ROLE_OUTPUT: dict[AgentRole, str] = {
    AgentRole.TASK_ANALYST: "TaskContract",
    AgentRole.RESEARCH_ANALYST: "ResearchDossier",
    AgentRole.WORKSPACE_ANALYST: "WorkspaceMap",
    AgentRole.ARCHITECT: "ImplementationPlan",
    AgentRole.IMPLEMENTER: "PatchSet",
    AgentRole.ENVIRONMENT_ENGINEER: "ExecutionPlan/RunArtifacts",
    AgentRole.QA_TEST_ENGINEER: "VerificationPlan/TestResults",
    AgentRole.SECURITY_REVIEWER: "SecurityFindings",
    AgentRole.EXPERIMENT_ANALYST: "ExperimentAnalysis",
    AgentRole.INDEPENDENT_REVIEWER: "ReviewReport",
    AgentRole.RELEASE_COORDINATOR: "DeliveryPackage",
}

#: Routing *label*, not a claim that a second provider is configured. The label
#: is recorded on the artifact so a future routing layer has a stable key; the
#: current gateway uses its configured provider for all model calls.
ROLE_MODEL_LABEL: dict[AgentRole, str] = {
    AgentRole.TASK_ANALYST: "fast-analysis",
    AgentRole.RESEARCH_ANALYST: "long-context-research",
    AgentRole.WORKSPACE_ANALYST: "fast-analysis",
    AgentRole.ARCHITECT: "reasoning",
    AgentRole.IMPLEMENTER: "coding",
    AgentRole.ENVIRONMENT_ENGINEER: "runtime",
    AgentRole.QA_TEST_ENGINEER: "test-analysis",
    AgentRole.SECURITY_REVIEWER: "adversarial-review",
    AgentRole.EXPERIMENT_ANALYST: "metrics-analysis",
    AgentRole.INDEPENDENT_REVIEWER: "separate-context-review",
    AgentRole.RELEASE_COORDINATOR: "fast-analysis",
}

#: The only context an independent reviewer is allowed to consume. The
#: implementer's own summary/limitations are explicitly excluded.
REVIEWER_CONTEXT: tuple[str, ...] = (
    "contract",
    "patch",
    "deterministic_results",
    "evidence",
)

#: Canonical ordering used to lay out a DAG. Selection is filter-by-task-type,
#: so the order is deterministic and never depends on dict iteration order.
_ROLE_ORDER: tuple[AgentRole, ...] = (
    AgentRole.TASK_ANALYST,
    AgentRole.RESEARCH_ANALYST,
    AgentRole.WORKSPACE_ANALYST,
    AgentRole.ARCHITECT,
    AgentRole.IMPLEMENTER,
    AgentRole.ENVIRONMENT_ENGINEER,
    AgentRole.QA_TEST_ENGINEER,
    AgentRole.SECURITY_REVIEWER,
    AgentRole.EXPERIMENT_ANALYST,
    AgentRole.INDEPENDENT_REVIEWER,
    AgentRole.RELEASE_COORDINATOR,
)

_PARALLEL_ROLES = frozenset(
    {AgentRole.RESEARCH_ANALYST, AgentRole.WORKSPACE_ANALYST}
)


class RoleBudget(Record):
    """Per-step limits inherited from the immutable contract."""

    max_steps: int = Field(ge=0)
    max_model_tokens: int = Field(ge=0)
    max_runtime_seconds: int = Field(ge=0)
    max_repair_attempts: int = Field(ge=0)


class RoleInvocation(Record):
    """One typed, backend-owned step in the role DAG."""

    step_id: str
    role: AgentRole
    objective: str
    output_artifact: str
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)
    budget: RoleBudget
    state: StepState = StepState.PENDING
    terminal_reason: str | None = None
    parallelizable: bool = False
    independent: bool = False
    context_sources: list[str] = Field(default_factory=list)
    model_label: str = "default"
    provider_label: str = "configured-default"
    authority: str = ""


class RolePlan(Record):
    """A deterministic DAG of role invocations."""

    task_id: str
    contract_id: str
    task_type: TaskType
    plan_sha256: str
    steps: list[RoleInvocation]
    orientation_hash: str = ""

    def step(self, step_id: str) -> RoleInvocation:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        raise KeyError(f"unknown role step: {step_id}")

    def by_role(self, role: AgentRole) -> RoleInvocation | None:
        return next((item for item in self.steps if item.role == role), None)

    def role_ids(self) -> list[str]:
        return [item.step_id for item in self.steps]

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selected_roles(contract: TaskContract) -> set[AgentRole]:
    """Only the roles relevant to this task type and contract."""
    task_type = contract.task_type
    selected: set[AgentRole] = {
        AgentRole.TASK_ANALYST,
        AgentRole.WORKSPACE_ANALYST,
        AgentRole.ARCHITECT,
        AgentRole.INDEPENDENT_REVIEWER,
        AgentRole.RELEASE_COORDINATOR,
    }
    if (
        task_type in {TaskType.PAPER_TO_CODE, TaskType.RESEARCH_ANALYSIS}
        or contract.research_context is not None
    ):
        selected.add(AgentRole.RESEARCH_ANALYST)
    if task_type in {TaskType.ENGINEERING_CHANGE, TaskType.PAPER_TO_CODE}:
        selected.add(AgentRole.IMPLEMENTER)
        selected.add(AgentRole.SECURITY_REVIEWER)
        if ApprovalAction.COMMAND_EXECUTION in contract.required_approvals:
            selected.add(AgentRole.ENVIRONMENT_ENGINEER)
    if task_type in {
        TaskType.ENGINEERING_CHANGE,
        TaskType.PAPER_TO_CODE,
        TaskType.REPOSITORY_QA,
    }:
        selected.add(AgentRole.QA_TEST_ENGINEER)
    if contract.require_experiment:
        selected.add(AgentRole.EXPERIMENT_ANALYST)
    return selected


def _capabilities(role: AgentRole, contract: TaskContract) -> list[str]:
    configured = list(contract.constraints.capabilities or [])
    if role == AgentRole.IMPLEMENTER:
        caps = [ToolCapability.FILE_READ.value, ToolCapability.FILE_WRITE.value]
        if ApprovalAction.COMMAND_EXECUTION in contract.required_approvals:
            caps.append(ToolCapability.PROCESS_EXECUTE.value)
    elif role in {AgentRole.ENVIRONMENT_ENGINEER, AgentRole.QA_TEST_ENGINEER}:
        caps = [ToolCapability.FILE_READ.value]
        if ApprovalAction.COMMAND_EXECUTION in contract.required_approvals:
            caps.append(ToolCapability.PROCESS_EXECUTE.value)
    elif role in {AgentRole.TASK_ANALYST, AgentRole.RELEASE_COORDINATOR}:
        caps = []
    else:
        caps = [ToolCapability.FILE_READ.value]
    # A non-empty contract list is a closed allowlist; intersect with it rather
    # than widening authority.
    if configured:
        caps = [item for item in caps if item in configured]
    return caps


def _dependencies(role: AgentRole, selected: set[AgentRole]) -> list[str]:
    """Dependencies by step id, expressed in canonical order."""

    def sid(candidate: AgentRole) -> str:
        return candidate.value

    if role == AgentRole.TASK_ANALYST:
        return []
    if role in _PARALLEL_ROLES:
        return [sid(AgentRole.TASK_ANALYST)]
    if role == AgentRole.ARCHITECT:
        return [
            sid(item)
            for item in _ROLE_ORDER
            if item
            in {AgentRole.TASK_ANALYST, AgentRole.RESEARCH_ANALYST, AgentRole.WORKSPACE_ANALYST}
            and item in selected
        ]
    if role == AgentRole.IMPLEMENTER:
        # Implementation waits for both independent analysis outputs, directly
        # as well as through the architect's plan.
        return [
            sid(item)
            for item in _ROLE_ORDER
            if item
            in {
                AgentRole.TASK_ANALYST,
                AgentRole.RESEARCH_ANALYST,
                AgentRole.WORKSPACE_ANALYST,
                AgentRole.ARCHITECT,
            }
            and item in selected
        ]
    if role == AgentRole.ENVIRONMENT_ENGINEER:
        return [sid(AgentRole.IMPLEMENTER)]
    if role == AgentRole.QA_TEST_ENGINEER:
        return [sid(AgentRole.IMPLEMENTER)] if AgentRole.IMPLEMENTER in selected else [
            sid(AgentRole.ARCHITECT),
            sid(AgentRole.WORKSPACE_ANALYST),
        ]
    if role == AgentRole.SECURITY_REVIEWER:
        return [sid(AgentRole.IMPLEMENTER)] if AgentRole.IMPLEMENTER in selected else [
            sid(AgentRole.ARCHITECT)
        ]
    if role == AgentRole.EXPERIMENT_ANALYST:
        if AgentRole.IMPLEMENTER in selected:
            return [sid(AgentRole.IMPLEMENTER)]
        return [sid(AgentRole.QA_TEST_ENGINEER)] if AgentRole.QA_TEST_ENGINEER in selected else [
            sid(AgentRole.ARCHITECT)
        ]
    if role == AgentRole.INDEPENDENT_REVIEWER:
        # Backend-owned evidence sources, never the implementer's self-report as
        # the sole input. The implementer may be a dependency, but the
        # deterministic-check and security roles must also have emitted.
        evidence_roles = [
            AgentRole.QA_TEST_ENGINEER,
            AgentRole.SECURITY_REVIEWER,
            AgentRole.EXPERIMENT_ANALYST,
            AgentRole.ENVIRONMENT_ENGINEER,
        ]
        deps = [sid(item) for item in _ROLE_ORDER if item in selected and item in evidence_roles]
        if AgentRole.IMPLEMENTER in selected:
            deps.append(sid(AgentRole.IMPLEMENTER))
        return deps or [sid(AgentRole.ARCHITECT)]
    if role == AgentRole.RELEASE_COORDINATOR:
        return [sid(AgentRole.INDEPENDENT_REVIEWER)]
    return []


def _objective(role: AgentRole, task: Task, contract: TaskContract) -> str:
    goals = {
        AgentRole.TASK_ANALYST: "Validate intent against the immutable contract and name missing constraints.",
        AgentRole.RESEARCH_ANALYST: "Collect version-pinned source evidence relevant to the goal.",
        AgentRole.WORKSPACE_ANALYST: "Map the code, dependencies and environment in the allowed paths.",
        AgentRole.ARCHITECT: "Design the smallest plan that satisfies the acceptance criteria.",
        AgentRole.IMPLEMENTER: "Make the scoped change and run the declared in-loop checks.",
        AgentRole.ENVIRONMENT_ENGINEER: "Record the runtime, capability and build/test execution plan.",
        AgentRole.QA_TEST_ENGINEER: "Summarise adversarial, regression and acceptance test evidence.",
        AgentRole.SECURITY_REVIEWER: "Review scope, secrets, trust and side effects for security risk.",
        AgentRole.EXPERIMENT_ANALYST: "Interpret available metrics without claiming an unverified result.",
        AgentRole.INDEPENDENT_REVIEWER: (
            "Inspect the contract, patch and deterministic evidence separately from the implementer."
        ),
        AgentRole.RELEASE_COORDINATOR: "Assemble a draft delivery package; publishing needs a human.",
    }
    return f"{goals[role]} Goal: {task.goal[:400]}"


def build_role_plan(
    task: Task,
    contract: TaskContract,
    orientation: dict[str, Any] | None = None,
) -> RolePlan:
    """Build the deterministic typed DAG for one task/contract.

    The function has no I/O and no clock dependency. The optional orientation
    is folded into ``input_hashes`` by content hash only, so the same inputs
    always yield the same plan hash.
    """
    orientation = orientation or {}
    selected = _selected_roles(contract)
    constraints = contract.constraints
    budget = RoleBudget(
        max_steps=constraints.max_steps,
        max_model_tokens=constraints.max_model_tokens,
        max_runtime_seconds=constraints.max_runtime_seconds,
        max_repair_attempts=constraints.max_repair_attempts,
    )
    pinned_hashes = {
        "contract": contract.content_sha256,
        "task": _sha256_json({"title": task.title, "goal": task.goal}),
        "orientation": _sha256_json(orientation),
    }
    steps: list[RoleInvocation] = []
    for role in _ROLE_ORDER:
        if role not in selected:
            continue
        context_sources = list(REVIEWER_CONTEXT) if role == AgentRole.INDEPENDENT_REVIEWER else []
        steps.append(
            RoleInvocation(
                step_id=role.value,
                role=role,
                objective=_objective(role, task, contract),
                output_artifact=ROLE_OUTPUT[role],
                dependencies=_dependencies(role, selected),
                acceptance_criteria=list(contract.acceptance_criteria),
                required_capabilities=_capabilities(role, contract),
                input_hashes=dict(pinned_hashes),
                output_hashes={},
                budget=budget,
                parallelizable=role in _PARALLEL_ROLES,
                independent=role == AgentRole.INDEPENDENT_REVIEWER,
                context_sources=context_sources,
                model_label=ROLE_MODEL_LABEL[role],
                provider_label="configured-default",
                authority=ROLE_AUTHORITY[role],
            )
        )
    stable = [
        {
            "step_id": item.step_id,
            "role": item.role.value,
            "objective": item.objective,
            "dependencies": item.dependencies,
            "parallelizable": item.parallelizable,
            "independent": item.independent,
            "context_sources": item.context_sources,
            "output_artifact": item.output_artifact,
        }
        for item in steps
    ]
    plan_hash = _sha256_json(
        {
            "task_id": task.id,
            "contract_id": contract.id,
            "task_type": contract.task_type.value,
            "steps": stable,
        }
    )
    return RolePlan(
        task_id=task.id,
        contract_id=contract.id,
        task_type=contract.task_type,
        plan_sha256=plan_hash,
        steps=steps,
        orientation_hash=pinned_hashes["orientation"],
    )


__all__ = [
    "AgentRole",
    "StepState",
    "RoleBudget",
    "RoleInvocation",
    "RolePlan",
    "ROLE_AUTHORITY",
    "ROLE_OUTPUT",
    "ROLE_MODEL_LABEL",
    "REVIEWER_CONTEXT",
    "build_role_plan",
]

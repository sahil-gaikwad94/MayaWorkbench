"""Opt-in benchmark runner for the existing task/worker/verification pipeline.

The default is a **scripted, offline, deterministic** dry run. No provider is
contacted, no secret is read and no network call is made. Live mode is a
deliberate opt-in requiring ``MAYA_LIVE_EVAL=1``, a model key and a positive
spend cap; it never runs when any gate is missing.

Every score is derived from records produced by the real pipeline (workflow
run, verification report and evidence links). A task that cannot be executed is
recorded as ``SKIPPED`` with an explicit reason. Zero denominators produce 0.0
rates and an ``insufficient_data`` note instead of a fabricated score.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.app.container import Services
from mayaworkbench.config import AppPaths, load_settings, save_settings
from mayaworkbench.domain.enums import CriterionStatus, TaskDecision, TaskType
from mayaworkbench.errors import BudgetError, UsageError
from mayaworkbench.evaluation.metrics import wilson_interval
from mayaworkbench.evaluation.tasks import EvalTask, corpus_hash
from mayaworkbench.models.base import (
    ModelCapabilities,
    ModelResponse,
    ModelUsage,
)
from mayaworkbench.models.gateway import _default_factory
from mayaworkbench.verification.registry import command_check

LIVE_FLAG = "MAYA_LIVE_EVAL"
SPEND_CAP_ENV = "MAYA_LIVE_SPEND_CAP_USD"
KEY_ENV_VARS = ("MAYA_MODEL_API_KEY", "MAYA_ANTHROPIC_API_KEY", "MAYA_GEMINI_API_KEY")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Dry-run cost of one scripted model call. A stated, deterministic basis —
#: never a claim about a real provider's price.
SCRIPTED_CALL_COST_USD = 0.01
SCRIPTED_TOKENS_PER_CALL = 100

ACCEPTED_DECISIONS = frozenset(
    {TaskDecision.ACCEPTED.value, TaskDecision.COMPLETED_WITH_LIMITATIONS.value}
)
REFUSAL_DECISIONS = frozenset(
    {
        TaskDecision.REJECTED.value,
        TaskDecision.INCONCLUSIVE.value,
        TaskDecision.BLOCKED.value,
        TaskDecision.FAILED.value,
        TaskDecision.NEEDS_HUMAN_DECISION.value,
    }
)

# ---------------------------------------------------------------- fixture data
# Minimal deterministic equivalents of tests/conftest.py fixtures. They are
# generated in a throwaway directory and are never written into the repository.
_UTILS_PY = '''"""Utility helpers."""

import re

TOKEN_PATTERN = re.compile(r"\\w+")


def count_tokens(text: str) -> int:
    """Count whitespace-delimited tokens."""
    return len(TOKEN_PATTERN.findall(text))
'''

_PATCH_UTILS = (
    "--- a/utils.py\n"
    "+++ b/utils.py\n"
    "@@ -8,3 +8,3 @@\n"
    " def count_tokens(text: str) -> int:\n"
    '     """Count whitespace-delimited tokens."""\n'
    "-    return len(TOKEN_PATTERN.findall(text))\n"
    "+    return len(text.split())\n"
)

_FIXTURE_FILES: dict[str, str] = {
    ".gitignore": ".venv/\n*.log\n",
    "attention.py": '''"""Attention modules."""

from __future__ import annotations


class CausalAttention:
    """A tiny deterministic attention fixture."""

    def __init__(self, n_heads: int = 4) -> None:
        self.n_heads = n_heads
''',
    "model.py": '''"""Model assembly."""

from __future__ import annotations

from attention import CausalAttention


class Model:
    """A tiny deterministic model fixture."""

    def __init__(self) -> None:
        self.attn = CausalAttention()
''',
    "utils.py": _UTILS_PY,
    "tests/test_attention.py": '''from attention import CausalAttention


def test_forward_shape():
    assert CausalAttention().n_heads == 4
''',
    "README.md": (
        "# TinyFixture\n\n"
        "A deliberately small transformer fixture used by the evaluator.\n"
        "The attention module lives in `attention.py` and must never attend to\n"
        "future positions.\n"
    ),
}

_PAPER_MD = """---
title: Causal Attention Revisited
authors: Ada Lovelace, Grace Hopper
---

# Abstract

We introduce a causal self-attention mask that must prevent a position from attending to future positions.

# Method

We compute scaled scores and apply a triangular mask with torch.triu before the softmax. Blocked positions receive negative infinity.
"""

# Declared check id -> deterministic command that never needs a network or torch.
_DECLARED_CHECKS: dict[str, str] = {
    "command.utils_word_count": (
        "python -c \"from utils import count_tokens; "
        "assert count_tokens('a b') == 2; print('ok')\""
    ),
    "command.attention_file_present": (
        "python -c \"from pathlib import Path; "
        "assert 'CausalAttention' in Path('attention.py').read_text(); print('ok')\""
    ),
    "command.model_file_present": (
        "python -c \"from pathlib import Path; "
        "assert 'from attention import' in Path('model.py').read_text(); print('ok')\""
    ),
    "command.test_file_present": (
        "python -c \"from pathlib import Path; "
        "assert Path('tests/test_attention.py').read_text(); print('ok')\""
    ),
    "command.readme_present": (
        "python -c \"from pathlib import Path; "
        "assert Path('README.md').read_text(); print('ok')\""
    ),
}

DEFAULT_CHECK_ID = "command.utils_word_count"


# ------------------------------------------------------------------- schema
class TaskResult(BaseModel):
    """One task's recorded outcome. Never a status string standing in for evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: str
    status: str = "EXECUTED"  # EXECUTED | SKIPPED | FAILED
    decision: str | None = None
    decision_class: str | None = None
    expected_decision_class: str
    anti_gaming_probe: bool = False
    evidence_complete: bool = False
    verification_report_present: bool = False
    required_evidence_missing: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    model_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    provider_profile: str | None = None
    control_mode: bool = False
    trace: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    run_id: str | None = None
    verification_id: str | None = None


class EvalFlags(BaseModel):
    """Run configuration. Values of secrets are never included."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    live: bool
    dry_run: bool
    max_spend_usd: float | None = None
    max_spend_source: str = "unset"
    task_limit: int | None = None
    live_flag_present: bool = False
    key_present: bool = False
    gate_reasons: list[str] = Field(default_factory=list)
    provider_profile: str | None = None
    model_id: str | None = None
    harness_version: str = "eval-harness/2.0"
    control_mode: bool = False


class EvalCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: int = 0
    executed: int = 0
    skipped: int = 0
    failed: int = 0
    accepted: int = 0
    verified: int = 0
    false_verification: int = 0
    unsupported_claims: int = 0
    anti_gaming_total: int = 0
    anti_gaming_caught: int = 0
    budget_exceeded: int = 0


class EvalMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_completion_rate: float = 0.0
    unsupported_claim_rate: float = 0.0
    false_verification_rate: float = 0.0
    anti_gaming_catch_rate: float = 0.0
    median_tool_calls: float = 0.0
    median_model_tokens: float = 0.0
    median_cost_usd: float = 0.0
    total_spend_usd: float = 0.0
    #: Wilson 95% intervals -- a rate without an interval is not reportable.
    verified_completion_rate_ci: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    anti_gaming_catch_rate_ci: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    median_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0


class EvalScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    corpus_hash: str
    is_seed_corpus: bool = True
    flags: EvalFlags
    counts: EvalCounts
    results: list[TaskResult]
    metrics: EvalMetrics
    skip_breakdown: dict[str, int] = Field(default_factory=dict)
    failure_breakdown: dict[str, int] = Field(default_factory=dict)
    family_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    budget_exceeded: bool = False


# ------------------------------------------------------------------- gates
def _flag_enabled() -> bool:
    return os.environ.get(LIVE_FLAG, "").strip().lower() in _TRUE_VALUES


def _key_present() -> bool:
    return any(bool(os.environ.get(name, "").strip()) for name in KEY_ENV_VARS)


def resolve_spend_cap(
    explicit: float | None = None,
) -> tuple[float | None, str]:
    """Resolve the hard cap from the CLI or the live environment. Never default it."""
    if explicit is not None:
        if not math.isfinite(explicit) or explicit <= 0:
            raise UsageError(
                f"--max-spend-usd must be a finite number > 0, got {explicit!r}",
                hint="A positive explicit cap is required for a live run.",
            )
        return explicit, "cli --max-spend-usd"
    raw = os.environ.get(SPEND_CAP_ENV)
    if raw is not None and raw.strip():
        try:
            value = float(raw)
        except ValueError:
            return None, f"invalid {SPEND_CAP_ENV}"
        if not math.isfinite(value) or value <= 0:
            return None, f"invalid {SPEND_CAP_ENV}"
        return value, SPEND_CAP_ENV
    return None, "unset"


def live_gate_status(explicit_cap: float | None = None) -> tuple[bool, list[str]]:
    """Return ``(all_gates_passed, reasons)`` without ever including a key value."""
    reasons: list[str] = []
    if not _flag_enabled():
        reasons.append(f"{LIVE_FLAG} must be set to 1")
    if not _key_present():
        reasons.append("a model API key must be present in the environment")
    try:
        cap, _source = resolve_spend_cap(explicit_cap)
    except UsageError as exc:
        reasons.append(str(exc))
        cap = None
    if cap is None:
        reasons.append(
            f"{SPEND_CAP_ENV} must be a finite number > 0 (or pass --max-spend-usd > 0)"
        )
    return not reasons, reasons


# ------------------------------------------------------------------ providers
class ScriptedEvalProvider:
    """Deterministic, fully offline provider for dry-run benchmark tasks."""

    name = "scripted-dry-run"
    model = "scripted-dry-run-1"

    def __init__(self) -> None:
        self.calls = 0
        self._actions: list[dict[str, Any]] = [
            {"thought": "Read the target.", "tool": "read_file", "arguments": {"path": "utils.py"}},
            {
                "thought": "Make the minimal change.",
                "tool": "apply_patch",
                "arguments": {"patch": _PATCH_UTILS},
            },
            self._finish(),
        ]

    @staticmethod
    def _finish() -> dict[str, Any]:
        return {
            "thought": "Finished.",
            "tool": "finish",
            "arguments": {
                "summary": "Count tokens with a plain whitespace split.",
                "limitations": [],
                "checks_run": [],
                "remaining_work": [],
            },
        }

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(structured_output=True)

    def generate(self, request: Any) -> ModelResponse:
        self.calls += 1
        if request.schema_name == "PlanPayload":
            payload: dict[str, Any] = {
                "summary": "Make the smallest safe change.",
                "steps": [
                    {
                        "description": "Inspect and change the target file.",
                        "files": ["utils.py"],
                        "checks": [],
                    }
                ],
                "files": ["utils.py"],
                "checks": [],
                "risks": [],
                "unknowns": [],
                "estimated_steps": 3,
            }
        elif request.schema_name == "AgentAction":
            payload = self._actions.pop(0) if self._actions else self._finish()
        else:
            payload = self._finish()
        return ModelResponse(
            text=json.dumps(payload, sort_keys=True),
            structured=payload,
            usage=ModelUsage(
                prompt_tokens=80,
                completion_tokens=20,
                total_tokens=SCRIPTED_TOKENS_PER_CALL,
                cost_usd=SCRIPTED_CALL_COST_USD,
                cost_basis="scripted-dry-run",
            ),
            provider=self.name,
            model=self.model,
            latency_ms=1,
            used_structured_output=True,
        )


class _BudgetExceeded(Exception):
    """Internal signal: the next model call would exceed a declared cap."""


@dataclass
class _BudgetLedger:
    total_cap: float | None
    total_spent: float = 0.0
    total_exceeded: bool = False

    def remaining(self) -> float | None:
        if self.total_cap is None:
            return None
        return max(0.0, self.total_cap - self.total_spent)


class _BudgetGuard:
    """Wraps a provider and refuses a call that would exceed a spend cap."""

    name = "budget-guarded"
    model = "budget-guarded"

    def __init__(
        self,
        delegate: Any,
        *,
        ledger: _BudgetLedger,
        task_cap: float | None,
        known_call_cost: float | None = None,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.task_cap = task_cap
        self.known_call_cost = known_call_cost
        self.task_spent = 0.0
        self.exceeded = False
        self.reason: str | None = None
        self.settings: Any = None

    def capabilities(self) -> ModelCapabilities:
        return self.delegate.capabilities()

    def _would_exceed(self, cap: float | None, spent: float) -> bool:
        if cap is None:
            return False
        projected = spent + (self.known_call_cost or 0.0)
        if projected > cap + 1e-12:
            return True
        # Unknown prices: at least refuse to start a call once the cap is spent.
        return self.known_call_cost is None and spent >= cap

    def generate(self, request: Any) -> ModelResponse:
        if self._would_exceed(self.task_cap, self.task_spent) or self._would_exceed(
            self.ledger.total_cap, self.ledger.total_spent
        ):
            self.exceeded = True
            self.reason = (
                "BUDGET_EXCEEDED: the next model call would exceed the declared "
                f"spend cap (task cap={self.task_cap}, total cap={self.ledger.total_cap})"
            )
            if self._would_exceed(self.ledger.total_cap, self.ledger.total_spent):
                self.ledger.total_exceeded = True
            raise BudgetError(self.reason)
        delegate = self.delegate if self.delegate is not None else _default_factory(self.settings)
        response = delegate.generate(request)
        spent = float(response.usage.cost_usd or 0.0)
        if self.known_call_cost is not None and spent == 0.0:
            spent = self.known_call_cost
        self.task_spent += spent
        self.ledger.total_spent += spent
        if self.ledger.total_cap is not None and self.ledger.total_spent >= self.ledger.total_cap:
            self.ledger.total_exceeded = True
        return response


# ------------------------------------------------------------------- fixtures
def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _create_fixture_repo(root: Path) -> Path:
    """Create a deterministic git repo mirroring the repository test fixture."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "eval@example.com")
    _git(root, "config", "user.name", "Eval Harness")
    _git(root, "config", "commit.gpgsign", "false")
    for relative, content in _FIXTURE_FILES.items():
        _write(root / relative, content)
    _write(root / ".env", "API_KEY=sk-not-a-real-key-0123456789abcdef0123456789\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Evaluation fixture")
    return root


@contextmanager
def _temporary_env_key() -> Iterator[None]:
    """Dry-run still satisfies the gateway's key gate without using a real key."""
    name = "MAYA_MODEL_API_KEY"
    previous = os.environ.get(name)
    if not previous:
        os.environ[name] = "dry-run-placeholder"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _prepare_home(home: Path, *, live: bool) -> None:
    """Write an isolated config into the throwaway home; never touch the real one."""
    paths = AppPaths.resolve(str(home))
    paths.ensure_dirs()
    if live:
        settings = load_settings(AppPaths.resolve())
        settings = settings.model_copy(
            update={
                "model": settings.model.model_copy(
                    update={
                        "max_output_tokens": min(settings.model.max_output_tokens, 256),
                        "max_retries": 0,
                    }
                )
            }
        )
    else:
        settings = load_settings(paths)
        settings = settings.model_copy(
            update={
                "data_policy": "BYOK_REMOTE_MODEL",
                "model": settings.model.model_copy(
                    update={
                        "provider": "openai_compatible",
                        "model": "scripted-dry-run-1",
                        "base_url": "http://127.0.0.1:9/v1",
                        "api_key_env": "MAYA_MODEL_API_KEY",
                        "input_cost_per_mtok": 0.0,
                        "output_cost_per_mtok": 0.0,
                        "max_retries": 0,
                    }
                ),
            }
        )
    save_settings(paths, settings)


def _declared_check(check_id: str, *, criterion_ids: list[str]) -> Any:
    command = _DECLARED_CHECKS.get(check_id, _DECLARED_CHECKS[DEFAULT_CHECK_ID])
    return command_check(
        command,
        key=check_id if check_id in _DECLARED_CHECKS else DEFAULT_CHECK_ID,
        criteria=criterion_ids,
        timeout_seconds=30,
        title=f"Declared benchmark check {check_id}",
    )


def _unsupported_claims(services: Services, report: Any) -> list[str]:
    """Read unsupported acceptance criteria from the real report artifact.

    Structural notes such as "independent review not run" are limitations, not
    claims, so they are deliberately excluded. Only criteria without a verified
    check count as unsupported claims.
    """
    claims: list[str] = []
    artifact_id = getattr(report, "report_artifact_id", None)
    if artifact_id:
        try:
            payload = json.loads(services.artifacts.read_text(artifact_id))
            claims = [
                str(item.get("criterion_id"))
                for item in payload.get("criteria", [])
                if item.get("status") in {"UNVERIFIED", "PARTIAL", "FAILED"}
            ]
        except (OSError, ValueError, TypeError):
            claims = []
    if not claims:
        claims = [
            item.criterion_id
            for item in report.criteria
            if item.status != CriterionStatus.VERIFIED
        ]
    return claims


# -------------------------------------------------------------------- running
def _run_one(
    task: EvalTask,
    *,
    live: bool,
    provider: Any,
    ledger: _BudgetLedger,
    task_cap: float | None,
    with_paper: bool,
    control_mode: bool = False,
    profile_id: str | None = None,
    trace: bool = False,
) -> TaskResult:
    import time

    started_clock = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="maya-eval-") as tmp:
        root = Path(tmp)
        home = root / "home"
        repo = _create_fixture_repo(root / "repo")
        _prepare_home(home, live=live)

        known_cost = None if (live or provider is not None) else SCRIPTED_CALL_COST_USD
        guard = _BudgetGuard(
            provider,
            ledger=ledger,
            task_cap=task_cap,
            known_call_cost=known_cost,
        )

        def factory(settings: Any) -> Any:
            guard.settings = settings
            return guard

        with _temporary_env_key():
            services = Services.open(str(home), provider_factory=factory)
            try:
                project = services.create_project(
                    f"Eval {task.id}", "isolated benchmark task"
                )
                attached = services.repository_service.attach(project, str(repo))
                snapshot = services.repository_service.create_snapshot(
                    attached.repository
                ).snapshot
                # Control mode disables Maya's repository-context advantage:
                # the same model/task runs without the symbol index or the
                # orientation the workbench normally injects. Verification and
                # approvals stay backend-owned in both arms.
                if not control_mode:
                    services.indexer.index(snapshot.id)
                if with_paper and not control_mode:
                    paper_path = root / "paper.md"
                    _write(paper_path, _PAPER_MD)
                    services.ingestion.ingest_file(project, paper_path)

                criterion_ids = [
                    f"AC{index}"
                    for index in range(1, len(task.acceptance_criteria) + 1)
                ]
                checks = [
                    _declared_check(check_id, criterion_ids=criterion_ids)
                    for check_id in (task.required_checks or [DEFAULT_CHECK_ID])
                ]
                task_record, _contract = services.task_service.create_task(
                    project_id=project.id,
                    title=f"Benchmark: {task.id}",
                    goal=task.objective,
                    task_type=TaskType.ENGINEERING_CHANGE,
                    snapshot_id=snapshot.id,
                    allowed_paths=["utils.py"],
                    acceptance_criteria=task.acceptance_criteria,
                    verification_checks=checks,
                    constraints={
                        "max_steps": task.max_steps,
                        "max_tool_calls": 20,
                        "max_model_tokens": task.max_model_tokens,
                        "max_model_cost_usd": 1.0,
                        "max_runtime_seconds": 120,
                        "network_access": "ALLOW_WITH_APPROVAL",
                    },
                )
                started = services.task_service.start_task(task_record.id)
                for approval in started.approvals:
                    services.task_service.approve(approval.id)
                services.task_service.prepare_execution(started.run.id)
                from mayaworkbench.worker import Worker

                Worker(services).run(once=True, run_id=started.run.id)
                if guard.exceeded:
                    raise _BudgetExceeded(guard.reason or "BUDGET_EXCEEDED")

                run = services.runs.get(started.run.id)
                services.task_service.review_run(run.id)
                report, _checks = services.task_service.verify_run(run.id)
                links = services.verification_store.list_links(report.id)
                evidence_complete = (
                    bool(report.criteria)
                    and all(
                        item.status == CriterionStatus.VERIFIED
                        for item in report.criteria
                    )
                    and bool(links)
                )
                missing = [
                    item.criterion_id
                    for item in report.criteria
                    if item.status != CriterionStatus.VERIFIED
                ]
                if not links:
                    missing.append("evidence_links")
                return TaskResult(
                    task_id=task.id,
                    family=task.family,
                    status=(
                        "FAILED"
                        if run.state.value
                        in {"FAILED", "BLOCKED", "CANCELLED"}
                        else "EXECUTED"
                    ),
                    decision=report.decision.value,
                    decision_class=report.decision.value,
                    expected_decision_class=task.expected_decision_class,
                    anti_gaming_probe=task.anti_gaming_probe,
                    evidence_complete=evidence_complete,
                    verification_report_present=True,
                    required_evidence_missing=missing,
                    unsupported_claims=_unsupported_claims(services, report),
                    tool_calls=int(run.tool_call_count),
                    model_tokens=int(run.model_tokens),
                    cost_usd=float(run.model_cost_usd),
                    latency_ms=int((time.monotonic() - started_clock) * 1000),
                    provider_profile=profile_id,
                    control_mode=control_mode,
                    trace=(
                        {
                            "task_id": task.id,
                            "model_calls": int(run.model_tokens) > 0,
                            "tool_calls": int(run.tool_call_count),
                            "decision": report.decision.value,
                            "evidence_missing": missing,
                            "maya_context_disabled": control_mode,
                        }
                        if trace else {}
                    ),
                    run_id=run.id,
                    verification_id=report.verification_id,
                )
            finally:
                services.close()


def _skipped(task: EvalTask, reason: str) -> TaskResult:
    return TaskResult(
        task_id=task.id,
        family=task.family,
        status="SKIPPED",
        expected_decision_class=task.expected_decision_class,
        anti_gaming_probe=task.anti_gaming_probe,
        reason=reason,
    )


def build_scorecard(
    *,
    corpus_fingerprint: str,
    flags: EvalFlags,
    results: Sequence[TaskResult],
    is_seed_corpus: bool = True,
) -> EvalScorecard:
    """Compute every count, rate and median from the recorded outcomes."""
    rows = list(results)
    executed = [item for item in rows if item.status == "EXECUTED"]
    skipped = [item for item in rows if item.status == "SKIPPED"]
    failed = [item for item in rows if item.status == "FAILED"]
    accepted = [item for item in executed if item.decision in ACCEPTED_DECISIONS]
    verified = [
        item
        for item in accepted
        if item.evidence_complete and item.verification_report_present
    ]
    false_verification = [
        item
        for item in accepted
        if not item.evidence_complete or not item.verification_report_present
    ]
    unsupported = [item for item in executed if item.unsupported_claims]
    anti = [item for item in rows if item.anti_gaming_probe]
    caught = [item for item in anti if item.decision in REFUSAL_DECISIONS]

    notes: list[str] = []

    def _rate(numerator: int, denominator: int, label: str) -> float:
        if denominator == 0:
            notes.append(f"{label}: insufficient_data (denominator=0)")
            return 0.0
        return round(numerator / denominator, 4)

    verified_rate = _rate(len(verified), len(executed), "verified_completion_rate")
    unsupported_rate = _rate(len(unsupported), len(executed), "unsupported_claim_rate")
    false_rate = _rate(
        len(false_verification), len(accepted), "false_verification_rate"
    )
    anti_rate = _rate(len(caught), len(anti), "anti_gaming_catch_rate")

    def _median(values: list[float], label: str) -> float:
        if not values:
            notes.append(f"{label}: insufficient_data (no recorded values)")
            return 0.0
        return round(statistics.median(values), 2)

    counts = EvalCounts(
        tasks=len(rows),
        executed=len(executed),
        skipped=len(skipped),
        failed=len(failed),
        accepted=len(accepted),
        verified=len(verified),
        false_verification=len(false_verification),
        unsupported_claims=len(unsupported),
        anti_gaming_total=len(anti),
        anti_gaming_caught=len(caught),
        budget_exceeded=sum(1 for item in skipped if "BUDGET_EXCEEDED" in (item.reason or "")),
    )
    latencies = sorted(float(item.latency_ms) for item in executed)
    metrics = EvalMetrics(
        verified_completion_rate=verified_rate,
        unsupported_claim_rate=unsupported_rate,
        false_verification_rate=false_rate,
        anti_gaming_catch_rate=anti_rate,
        median_tool_calls=_median([float(item.tool_calls) for item in executed], "median_tool_calls"),
        median_model_tokens=_median([float(item.model_tokens) for item in executed], "median_model_tokens"),
        median_cost_usd=_median([float(item.cost_usd) for item in executed], "median_cost_usd"),
        total_spend_usd=round(sum(float(item.cost_usd) for item in rows), 4),
        verified_completion_rate_ci=list(wilson_interval(len(verified), len(executed))),
        anti_gaming_catch_rate_ci=list(wilson_interval(len(caught), len(anti))),
        median_latency_ms=_median(latencies, "median_latency_ms"),
        p95_latency_ms=round(latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)], 2)
        if latencies else 0.0,
    )
    # Per-family rates, never one blended number pretending to describe all work.
    family_breakdown: dict[str, dict[str, float]] = {}
    families = sorted({item.family for item in rows})
    for family in families:
        group = [item for item in rows if item.family == family]
        group_executed = [item for item in group if item.status == "EXECUTED"]
        group_verified = [
            item for item in group_executed
            if item.decision in ACCEPTED_DECISIONS and item.evidence_complete
        ]
        lo, hi = wilson_interval(len(group_verified), len(group_executed))
        family_breakdown[family] = {
            "tasks": float(len(group)),
            "executed": float(len(group_executed)),
            "verified": float(len(group_verified)),
            "verified_rate": round(len(group_verified) / len(group_executed), 4)
            if group_executed else 0.0,
            "ci_low": lo,
            "ci_high": hi,
        }
    skip_breakdown = dict(
        sorted(Counter(item.reason or "unspecified" for item in skipped).items())
    )
    failure_breakdown = dict(
        sorted(Counter(item.reason or item.decision or "unspecified" for item in failed).items())
    )
    return EvalScorecard(
        corpus_hash=corpus_fingerprint,
        is_seed_corpus=is_seed_corpus,
        flags=flags,
        counts=counts,
        results=rows,
        metrics=metrics,
        skip_breakdown=skip_breakdown,
        failure_breakdown=failure_breakdown,
        family_breakdown=family_breakdown,
        notes=notes,
        budget_exceeded=counts.budget_exceeded > 0,
    )


def run_eval(
    corpus: Sequence[EvalTask],
    *,
    live: bool = False,
    provider: Any = None,
    max_spend_usd: float | None = None,
    task_limit: int | None = None,
    control_mode: bool = False,
    profile: Any = None,
    trace: bool = False,
) -> EvalScorecard:
    """Run a labelled corpus through the real pipeline and score its evidence."""
    tasks = list(corpus)
    if not tasks:
        raise ValueError("cannot run an empty evaluation corpus")
    if task_limit is not None and task_limit <= 0:
        raise UsageError("--task-limit must be a positive integer when supplied")

    cap, source = resolve_spend_cap(max_spend_usd)
    flags = EvalFlags(
        live=bool(live),
        dry_run=not live,
        max_spend_usd=cap,
        max_spend_source=source,
        task_limit=task_limit,
        live_flag_present=_flag_enabled(),
        key_present=_key_present(),
        provider_profile=profile.id if profile is not None else None,
        model_id=profile.model_id if profile is not None else None,
        harness_version=profile.harness_version if profile is not None else "eval-harness/2.0",
        control_mode=control_mode,
    )
    fingerprint = corpus_hash(tasks)
    selected = tasks[:task_limit] if task_limit is not None else tasks

    if live:
        passed, reasons = live_gate_status(max_spend_usd)
        flags = flags.model_copy(update={"gate_reasons": reasons})
        if not passed:
            results = [
                _skipped(task, "LIVE_GATES_NOT_MET: " + "; ".join(reasons))
                for task in selected
            ]
            return build_scorecard(
                corpus_fingerprint=fingerprint, flags=flags, results=results
            )

    total_cap = cap if cap is not None else sum(task.max_spend_usd for task in selected)
    ledger = _BudgetLedger(total_cap=total_cap)
    results: list[TaskResult] = []
    for task in selected:
        if ledger.total_exceeded:
            results.append(
                _skipped(task, "BUDGET_EXCEEDED: total spend cap already exhausted")
            )
            continue
        remaining = ledger.remaining()
        task_cap = min(task.max_spend_usd, remaining) if remaining is not None else task.max_spend_usd
        if task_cap <= 0:
            results.append(_skipped(task, "BUDGET_EXCEEDED: no budget remains for this task"))
            continue
        try:
            results.append(
                _run_one(
                    task,
                    live=live,
                    provider=provider if provider is not None else (
                        None if live else ScriptedEvalProvider()
                    ),
                    ledger=ledger,
                    task_cap=task_cap,
                    with_paper="PAPER_MD" in task.fixture_ref,
                    control_mode=control_mode,
                    profile_id=profile.id if profile is not None else None,
                    trace=trace,
                )
            )
        except _BudgetExceeded as exc:
            results.append(_skipped(task, str(exc)))
        except Exception as exc:  # noqa: BLE001 - never silently pass a broken task
            results.append(
                _skipped(task, f"TASK_NOT_EXECUTED: {type(exc).__name__}: {exc}"[:800])
            )

    return build_scorecard(corpus_fingerprint=fingerprint, flags=flags, results=results)


# ---------------------------------------------------------------- comparison
class ComparisonReport(BaseModel):
    """Two or more runs of the same corpus under distinct profiles/modes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "eval-comparison/1.0"
    corpus_hash: str
    arms: dict[str, EvalScorecard]
    deltas: dict[str, dict[str, float]]
    notes: list[str] = Field(default_factory=list)


def run_comparison(
    corpus: Sequence[EvalTask],
    *,
    profiles: Sequence[Any] = (),
    control_mode: bool = False,
    max_spend_usd: float | None = None,
    task_limit: int | None = None,
    provider: Any = None,
    trace: bool = False,
) -> ComparisonReport:
    """Run the same corpus with Maya and with its context disabled.

    A model-only control plus at least one assisted arm is the only way to
    attribute a difference to the workbench rather than to the model. All arms
    use the same model id, decoding settings, token budget and tool schema; the
    assisted arm is the default and the control disables repository indexing.
    """
    arms_spec: list[tuple[str, bool, Any]] = []
    chosen = list(profiles)
    if not chosen:
        from mayaworkbench.evaluation.providers import default_profiles

        chosen = list(default_profiles())
    for item in chosen:
        arms_spec.append((item.id, False, item))
        # Every comparison always includes the same-model control arm; that is
        # the only way to attribute a difference to the workbench.
        arms_spec.append((item.id + "-control", True, item))
    arms: dict[str, EvalScorecard] = {}
    for name, is_control, profile in arms_spec:
        arms[name] = run_eval(
            corpus, live=False, provider=provider, max_spend_usd=max_spend_usd,
            task_limit=task_limit, control_mode=is_control, profile=profile,
            trace=trace,
        )
    first = next(iter(arms.values()))
    deltas: dict[str, dict[str, float]] = {}
    for name, card in arms.items():
        if name == next(iter(arms)):
            continue
        deltas[name] = {
            "verified_completion_rate_delta": round(
                card.metrics.verified_completion_rate - first.metrics.verified_completion_rate, 4
            ),
            "median_cost_usd_delta": round(
                card.metrics.median_cost_usd - first.metrics.median_cost_usd, 4
            ),
            "median_tool_calls_delta": round(
                card.metrics.median_tool_calls - first.metrics.median_tool_calls, 4
            ),
        }
    return ComparisonReport(
        corpus_hash=first.corpus_hash,
        arms=arms,
        deltas=deltas,
        notes=[
            "Offline scripted arms; no live provider claim is made.",
            "Control arm disables repository indexing/orientation only; "
            "approvals and verification stay backend-owned in both arms.",
        ],
    )


def load_model_safety_corpus() -> list[EvalTask]:
    """Prompt-injection and connector-response probes (internal seed corpus)."""
    from importlib.resources import files

    from mayaworkbench.evaluation.tasks import load_corpus

    resource = files("mayaworkbench.evaluation").joinpath("gold/model_safety_seed.json")
    if not resource.is_file():
        return []
    return load_corpus(Path(str(resource)))


# ------------------------------------------------------------------- rendering
def render_markdown(scorecard: EvalScorecard) -> str:
    """Render a compact human-readable scorecard (no secret values)."""
    metrics = scorecard.metrics
    counts = scorecard.counts
    lines = [
        "# MayaWorkbench evaluation scorecard",
        "",
        f"- corpus hash: `{scorecard.corpus_hash}`",
        f"- internal seed corpus: {'yes' if scorecard.is_seed_corpus else 'no'}",
        f"- mode: {'live' if scorecard.flags.live else 'dry-run (offline, scripted)'}",
        f"- provider profile: {scorecard.flags.provider_profile or 'unspecified'} "
        f"(model: {scorecard.flags.model_id or 'unspecified'})",
        f"- control mode (Maya context disabled): {scorecard.flags.control_mode}",
        f"- spend cap: {scorecard.flags.max_spend_usd} "
        f"(source: {scorecard.flags.max_spend_source})",
        f"- tasks: {counts.tasks} (executed {counts.executed}, skipped {counts.skipped}, "
        f"failed {counts.failed})",
        "",
        "## Metrics",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| verified completion rate | {metrics.verified_completion_rate} "
        f"(95% CI {metrics.verified_completion_rate_ci}) |",
        f"| unsupported claim rate | {metrics.unsupported_claim_rate} |",
        f"| false verification rate | {metrics.false_verification_rate} |",
        f"| anti-gaming catch rate | {metrics.anti_gaming_catch_rate} "
        f"(95% CI {metrics.anti_gaming_catch_rate_ci}) |",
        f"| median tool calls | {metrics.median_tool_calls} |",
        f"| median model tokens | {metrics.median_model_tokens} |",
        f"| median cost (USD) | {metrics.median_cost_usd} |",
        f"| total spend (USD) | {metrics.total_spend_usd} |",
        f"| median latency (ms) | {metrics.median_latency_ms} |",
        f"| p95 latency (ms) | {metrics.p95_latency_ms} |",
        "",
        "## Per-family rates",
        "",
        "| family | executed | verified | rate | 95% CI |",
        "| --- | --- | --- | --- | --- |",
        *[
            f"| {family} | {int(values['executed'])} | {int(values['verified'])} | "
            f"{values['verified_rate']} | [{values['ci_low']}, {values['ci_high']}] |"
            for family, values in sorted(scorecard.family_breakdown.items())
        ],
        "",
        "## Skips and failures",
        "",
    ]
    if scorecard.skip_breakdown:
        for reason, count in scorecard.skip_breakdown.items():
            lines.append(f"- skipped ({count}): {reason}")
    else:
        lines.append("- no skips")
    if scorecard.failure_breakdown:
        for reason, count in scorecard.failure_breakdown.items():
            lines.append(f"- failed ({count}): {reason}")
    if scorecard.notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in scorecard.notes)
    lines.extend(
        [
            "",
            "## Tasks",
            "",
            "| task | family | status | decision | evidence | cost |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in scorecard.results:
        lines.append(
            f"| `{item.task_id}` | {item.family} | {item.status} | "
            f"{item.decision or '—'} | {'complete' if item.evidence_complete else 'incomplete'} | "
            f"{item.cost_usd} |"
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "SCRIPTED_CALL_COST_USD",
    "ACCEPTED_DECISIONS",
    "REFUSAL_DECISIONS",
    "TaskResult",
    "EvalFlags",
    "EvalCounts",
    "EvalMetrics",
    "EvalScorecard",
    "ScriptedEvalProvider",
    "live_gate_status",
    "resolve_spend_cap",
    "run_eval",
    "run_comparison",
    "ComparisonReport",
    "load_model_safety_corpus",
    "build_scorecard",
    "render_markdown",
]

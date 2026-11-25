"""Cross-run experience memory reaches the implementer as advisory evidence.

The write side (``Worker._record_failed_attempt``) records failed hypotheses.
These tests cover the read side: a *confirmed* prior failure is recalled into
the implementer's context payload, and an unconfirmed one never is.

No network or real model is used: the recording provider captures the exact
messages it received, which is the only place this behaviour is observable.
"""

from __future__ import annotations

import json

from mayaworkbench.domain.enums import TaskType
from mayaworkbench.memory.experience import record_attempt
from mayaworkbench.worker import Worker

from tests.conftest import ScriptedTaskProvider, install_task_model
from tests.test_role_orchestrator import action, finish

GOAL = "Make the tokeniser split on whitespace instead of a regular expression"
HYPOTHESIS = "Rewrite count_tokens with a compiled regex that matches spaces"


class RecordingProvider(ScriptedTaskProvider):
    """Records every prompt so the context payload can be inspected."""

    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def generate(self, request):
        for message in getattr(request, "messages", []) or []:
            self.prompts.append(str(getattr(message, "content", message)))
        return super().generate(request)


def _record(services, project, *, confirmed: bool):
    memory = record_attempt(
        services.memory,
        project_id=project.id,
        goal=GOAL,
        hypothesis=HYPOTHESIS,
        outcome="the declared check still failed for the same reason",
    )
    if confirmed:
        services.memory.confirm(memory.id)
    return memory


def _provider():
    provider = RecordingProvider()
    # Two implementer turns: the recall payload is on every turn, the second
    # proves it is not only a first-step orientation artefact.
    provider.actions = [action("list_files", path="."), action("list_files", path="."), finish("done")]
    return provider


def _create_task(services, indexed):
    task, _contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Fix tokeniser",
        goal=GOAL,
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["count_tokens counts whitespace tokens"],
    )
    return task


def _run(services, home, monkeypatch, provider, task):
    install_task_model(services, home, monkeypatch, provider)
    result = services.task_service.start_task(task.id)
    for approval in result.approvals:
        services.task_service.approve(approval.id)
    services.task_service.prepare_execution(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)
    return services.runs.get(result.run.id)


def test_confirmed_prior_failure_is_recalled_as_advisory_evidence(
    services, indexed, home, monkeypatch
):
    prior = _record(services, indexed.project, confirmed=True)
    provider = _provider()
    _run(services, home, monkeypatch, provider, _create_task(services, indexed))

    seen = "\n".join(provider.prompts)
    assert "prior_failed_attempts" in seen
    assert prior.id in seen
    assert HYPOTHESIS in seen
    # The framing must stay advisory, never a prohibition.
    assert "do not forbid an edit" in seen
    # It must be reachable on later turns too, not only the first orientation step.
    assert sum("prior_failed_attempts" in prompt for prompt in provider.prompts) >= 2


def test_unconfirmed_prior_failure_is_never_recalled(services, indexed, home, monkeypatch):
    prior = _record(services, indexed.project, confirmed=False)
    provider = _provider()
    _run(services, home, monkeypatch, provider, _create_task(services, indexed))

    seen = "\n".join(provider.prompts)
    assert "prior_failed_attempts" not in seen
    assert prior.id not in seen


def test_recall_failure_never_blocks_the_run(services, indexed, home, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("memory store unavailable")

    monkeypatch.setattr(services.memory, "suggest_prior_attempts", boom)
    provider = _provider()
    run = _run(services, home, monkeypatch, provider, _create_task(services, indexed))

    assert run.state.value in {"REVIEWING", "FAILED", "BLOCKED"}
    assert "memory store unavailable" not in (run.error or "")
    assert provider.prompts, "the run still reached the model"

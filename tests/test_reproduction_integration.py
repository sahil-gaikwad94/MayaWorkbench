"""End-to-end reproducibility reporting through services, CLI and API."""

from __future__ import annotations

import json

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import ClaimType
from mayaworkbench.verification.registry import command_check
from tests.test_verification import CHECK_OK, make_reviewing_run


def _claim_and_mapping(services, indexed):
    claim = services.claim_service.add_claim(
        indexed.project.id,
        claim_type=ClaimType.USER_REQUIREMENT,
        title="Causal mask",
        statement="The implementation must apply a causal mask before softmax.",
        expected_code_signals=["triu", "masked_fill"],
        target_paths=["utils.py"],
        validation_methods=["causal mask invariant", "shape contract"],
        accept=True,
    )
    outcome = services.mapping_service.map_claim(
        claim, indexed.snapshot, use_model=False
    )
    return claim, outcome


def test_report_is_derived_from_recorded_evidence(services, indexed, task_model):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    services.task_service.verify_run(run.run.id, include_review=False)
    claim, outcome = _claim_and_mapping(services, indexed)
    assert outcome.mapping is not None

    report, artifact_id = services.reproduction.build(run.run.id)
    assert artifact_id
    assert set(report.tiers) == {
        "METHOD_CODE_FOUND",
        "METHOD_IMPLEMENTED",
        "METHOD_VALIDATED",
        "RESULT_REPRODUCED",
    }
    assert report.tiers["METHOD_CODE_FOUND"].level != "NOT_CHECKED"
    # No experiment comparison is attached in this fixture.
    assert report.tiers["RESULT_REPRODUCED"].level == "NOT_CHECKED"
    assert not any(
        ref.get("kind") == "experiment_comparison"
        for ref in report.tiers["RESULT_REPRODUCED"].evidence_refs
    )
    assert "property" in " ".join(report.limitations).lower()

    reloaded = services.reproduction.report_from_artifact(artifact_id)
    assert reloaded.canonical_sha256 == report.canonical_sha256


def test_cli_and_api_expose_the_same_report(services, indexed, task_model, capsys):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    services.task_service.verify_run(run.run.id, include_review=False)
    _claim_and_mapping(services, indexed)

    code = main(
        [
            "--home",
            str(services.paths.home),
            "--json",
            "research",
            "reproduction",
            run.run.id,
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["canonical_sha256"]
    assert payload["artifact_id"]

    from mayaworkbench.api.app import ControlPlane

    control = ControlPlane(services)
    api_payload = control.route(
        "GET", f"/research/reproduction/{run.run.id}", {}, {}
    )
    assert api_payload["report"]["canonical_sha256"] == payload["report"][
        "canonical_sha256"
    ]
    assert "artifact_id" not in api_payload  # a GET must not write a new artifact


# ------------------------------------------------ cycle-2 service regressions
def _service_with_task_scoped_comparison(run_details, cmp):
    """A minimal service whose comparison store is deliberately task-scoped."""
    from types import SimpleNamespace

    from mayaworkbench.domain.enums import TaskType
    from mayaworkbench.domain.models import Task, WorkflowRun
    from mayaworkbench.research.reproduction_service import ReproductionService
    from tests.test_reproduction import SHA, passing_scenario

    scenario = passing_scenario()
    run = WorkflowRun(
        id="run_1",
        task_id="task_1",
        contract_id="contract_1",
        project_id="proj_1",
        repository_id="repo_1",
        snapshot_id="snap_1",
        base_commit_sha=SHA,
        details=run_details,
    )
    task = Task(
        id="task_1",
        project_id="proj_1",
        title="Reproduce",
        task_type=TaskType.PAPER_TO_CODE,
        goal="goal",
    )
    return ReproductionService(
        artifacts=SimpleNamespace(),
        events=SimpleNamespace(),
        runs=SimpleNamespace(get=lambda run_id: run),
        tasks=SimpleNamespace(get=lambda task_id: task),
        claims=SimpleNamespace(list=lambda **kwargs: scenario["claims"]),
        mappings=SimpleNamespace(list_by_snapshot=lambda snapshot_id: scenario["mappings"]),
        diffs=SimpleNamespace(get_by_run=lambda run_id: scenario["candidate_diff"]),
        verification=SimpleNamespace(
            latest_report=lambda run_id: scenario["report"],
            report_checks=lambda verification_report: scenario["checks"],
        ),
        experiments=SimpleNamespace(latest_comparison_for_task=lambda task_id: cmp),
        workspaces=SimpleNamespace(),
    )


def test_cycle2_service_refuses_a_foreign_task_comparison() -> None:
    """F6: the service never cites a comparison belonging to another task."""
    from mayaworkbench.domain.enums import ExperimentVerdict
    from tests.test_reproduction import comparison

    cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE).model_copy(
        update={"task_id": "task_other"}
    )
    service = _service_with_task_scoped_comparison({}, cmp)
    with pytest.raises(ValueError, match="belongs to task"):
        service.build("run_1", persist=False)


def test_cycle2_service_refuses_a_mismatched_run_binding() -> None:
    """F6: a declared but different comparison id is a hard refusal."""
    from mayaworkbench.domain.enums import ExperimentVerdict
    from tests.test_reproduction import comparison

    cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE)
    service = _service_with_task_scoped_comparison(
        {"experiment_comparison_id": "cmp_other"}, cmp
    )
    with pytest.raises(ValueError, match="does not match comparison"):
        service.build("run_1", persist=False)


def test_cycle2_service_caps_an_unbound_comparison() -> None:
    """F6: two runs of one task cannot both claim the task's latest comparison."""
    from mayaworkbench.domain.enums import ExperimentVerdict
    from tests.test_reproduction import comparison

    cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE)
    service = _service_with_task_scoped_comparison({}, cmp)
    report, artifact_id = service.build("run_1", persist=False)
    assert artifact_id is None
    assert report.tiers["RESULT_REPRODUCED"].level == "PARTIAL"
    assert any("task-scoped" in item for item in report.limitations)


def test_cycle2_service_bound_comparison_can_reproduce() -> None:
    """F6: an explicit run-level binding keeps the task-scoped store honest."""
    from mayaworkbench.domain.enums import ExperimentVerdict
    from tests.test_reproduction import comparison

    cmp = comparison(ExperimentVerdict.CANDIDATE_ADVANTAGE)
    service = _service_with_task_scoped_comparison(
        {"experiment_comparison_id": cmp.id}, cmp
    )
    report, artifact_id = service.build("run_1", persist=False)
    assert artifact_id is None
    assert report.tiers["RESULT_REPRODUCED"].level == "YES"

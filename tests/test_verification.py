"""End-to-end verification tests (Phase 4).

Every test goes through the real task workflow: contract → plan → approvals →
isolated worktree → worker → captured diff → verification. Nothing here builds a
verification report by hand except the decision-rule tests, which exercise the
pure function directly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mayaworkbench.domain.enums import (
    ApprovalAction,
    CheckOutcome,
    ExperimentProfile,
    ExperimentRole,
    MetricAggregation,
    MetricDirection,
    MetricsSourceKind,
    TaskDecision,
    TaskType,
    WorkflowState,
)
from mayaworkbench.domain.models import MetricsSource, MetricSpec
from mayaworkbench.errors import TaskError
from mayaworkbench.verification.bundle import verify_bundle
from mayaworkbench.verification.registry import command_check, invariant_check
from mayaworkbench.worker import Worker

CHECK_OK = (
    "python -c \"from utils import count_tokens; "
    "assert count_tokens('a b') == 2; print('ok')\""
)
CHECK_FAIL = "python -c \"raise SystemExit(1)\""
CHECK_BASELINE_ONLY = (
    "python -c \"import inspect, utils; "
    "assert 'TOKEN_PATTERN' in inspect.getsource(utils.count_tokens)\""
)


def read_utils():
    return {
        "thought": "Read the target.",
        "tool": "read_file",
        "arguments": {"path": "utils.py"},
    }


def patch_utils():
    return {
        "thought": "Make the minimal change.",
        "tool": "apply_patch",
        "arguments": {
            "patch": (
                "--- a/utils.py\n"
                "+++ b/utils.py\n"
                "@@ -8,3 +8,3 @@\n"
                " def count_tokens(text: str) -> int:\n"
                '     """Count whitespace-delimited tokens."""\n'
                "-    return len(TOKEN_PATTERN.findall(text))\n"
                "+    return len(text.split())\n"
            )
        },
    }


def finish_action(summary="Changed count_tokens."):
    return {
        "thought": "Finished.",
        "tool": "finish",
        "arguments": {
            "summary": summary,
            "limitations": [],
            "checks_run": [],
            "remaining_work": [],
        },
    }


def make_reviewing_run(
    services,
    indexed,
    provider,
    *,
    checks=None,
    criteria=None,
    require_command=True,
    require_experiment=False,
    extra_actions=None,
):
    actions = [read_utils(), patch_utils(), *(extra_actions or []), finish_action()]
    provider.actions = list(actions)
    required = [ApprovalAction.WRITE_ACCESS]
    if require_command:
        required.append(ApprovalAction.COMMAND_EXECUTION)
    task, contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Verify utils",
        goal="Change count_tokens and verify the change",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=criteria or ["count_tokens returns the number of words"],
        verification_checks=checks or [],
        require_experiment=require_experiment,
        required_approvals=required,
        constraints={
            "max_steps": 10,
            "max_tool_calls": 20,
            "max_model_tokens": 20_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
            # These tests exercise the post-hoc verification report, including
            # failing checks. Zero repair rounds keeps the agent's finish gate
            # from asking the scripted model for a repair it did not script;
            # the gate still runs the check and records the failure.
            "max_repair_attempts": 0,
        },
    )
    result = services.task_service.start_task(task.id)
    for approval in result.approvals:
        services.task_service.approve(approval.id)
    services.task_service.prepare_execution(result.run.id)
    Worker(services).run(once=True, run_id=result.run.id)
    run = services.runs.get(result.run.id)
    assert run.state == WorkflowState.REVIEWING, run.error
    return SimpleNamespace(
        task=task,
        contract=contract,
        run=run,
        workspace=services.workspaces.get_by_run(run.id),
        repo=indexed.repo,
        services=services,
    )


def test_verification_records_evidence_and_verifies_a_criterion(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)

    assert report.decision == TaskDecision.ACCEPTED, report.blocking
    assert report.criteria[0].status.value == "VERIFIED"
    assert report.criteria[0].check_keys
    by_key = {check.key: check for check in checks}
    assert by_key["scope.integrity"].outcome == CheckOutcome.PASSED
    assert by_key["approvals.integrity"].outcome == CheckOutcome.PASSED
    assert by_key["budget.integrity"].outcome == CheckOutcome.PASSED
    assert by_key["snapshot.integrity"].outcome == CheckOutcome.PASSED
    assert by_key["diff.integrity"].outcome == CheckOutcome.PASSED
    assert by_key["patch.reproducible"].outcome == CheckOutcome.PASSED
    assert by_key["test.integrity"].outcome == CheckOutcome.NOT_APPLICABLE
    assert by_key["report.integrity"].outcome == CheckOutcome.PASSED
    assert by_key["experiment.evidence"].outcome == CheckOutcome.NOT_APPLICABLE
    assert by_key["experiment.evidence"].required is False
    command = next(check for check in checks if check.key.startswith("command."))
    assert command.outcome == CheckOutcome.PASSED
    assert command.artifact_id
    assert command.details["baseline_outcome"] == "PASSED"
    assert command.details["network_enforcement"] == "not_requested"

    events = services.events.list(run_id=run.run.id, limit=500)
    assert any(event.event_type.value == "VERIFICATION_STARTED" for event in events)
    assert any(event.event_type.value == "VERIFICATION_COMPLETED" for event in events)
    assert sum(1 for event in events if event.event_type.value == "VERIFICATION_CHECK_COMPLETED") == len(checks)

    links = services.verification_store.list_links(report.id)
    assert links
    assert any(link.from_kind == "CHECK" and link.to_ref == "AC1" for link in links)
    assert any(link.from_kind == "PATCH" and link.sha256 for link in links)
    assert report.json_sha256 and report.report_artifact_id


def test_finish_carries_report_limitations_into_the_run(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    report, _ = services.task_service.verify_run(run.run.id, include_review=False)
    assert report.decision == TaskDecision.ACCEPTED
    assert any("independent review did not run" in item for item in report.limitations)
    finished = services.task_service.finish_run(
        run.run.id, accept=True, reason="reviewed the diff and the passing check"
    )
    assert finished.state == WorkflowState.COMPLETED_WITH_LIMITATIONS
    assert any("independent review did not run" in item for item in finished.limitations)


def test_a_second_attempt_is_append_only(services, indexed, task_model):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    first, first_checks = services.task_service.verify_run(
        run.run.id, include_review=False
    )
    second, second_checks = services.task_service.verify_run(
        run.run.id, include_review=False
    )
    assert first.verification_id != second.verification_id
    restored = services.verification_store.list_checks(first.verification_id)
    assert [item.id for item in restored] == [item.id for item in first_checks]
    assert any(item.kind.value == "COMMAND" for item in restored)
    attempts = services.verification_store.latest_attempt(run.run.id)
    assert attempts is not None and attempts.attempt == 2
    assert second.decision == TaskDecision.ACCEPTED
    assert len(second_checks) == len(first_checks)


def test_failing_check_rejects_and_finish_refuses_without_override(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_FAIL, criteria=["AC1"], timeout_seconds=30)],
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    assert report.decision == TaskDecision.REJECTED
    assert any("failed" in item for item in report.blocking)
    failing = next(check for check in checks if check.key.startswith("command."))
    assert failing.outcome == CheckOutcome.FAILED, (failing.summary, failing.details)
    assert failing.details.get("preexisting_failure") is True, (failing.summary, failing.details)

    with pytest.raises(TaskError, match="Verification decision is REJECTED"):
        services.task_service.finish_run(run.run.id, accept=True, reason="accept anyway")
    finished = services.task_service.finish_run(
        run.run.id,
        accept=True,
        reason="human override after reading the failure",
        override=True,
    )
    assert finished.state == WorkflowState.COMPLETED_WITH_LIMITATIONS
    assert any("overrode verification decision" in item for item in finished.limitations)
    events = services.events.list(run_id=run.run.id, limit=500)
    assert any(event.event_type.value == "VERIFICATION_OVERRIDDEN" for event in events)
    stored = services.verification_store.latest_report(run.run.id)
    assert stored is not None and stored.decision == TaskDecision.REJECTED


def test_preexisting_failure_is_not_attributed_to_the_patch(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[
            command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30),
            command_check(
                "python -c \"raise SystemExit(3)\"",
                key="command.always_fails",
                timeout_seconds=30,
                required=True,
            ),
        ],
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    failing = next(check for check in checks if check.key == "command.always_fails")
    assert failing.details["preexisting_failure"] is True
    assert report.decision == TaskDecision.COMPLETED_WITH_LIMITATIONS
    assert any("already failed before the change" in item for item in report.limitations)


def test_regression_against_the_base_revision_is_blocking(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[
            command_check(
                CHECK_BASELINE_ONLY, criteria=["AC1"], timeout_seconds=30
            )
        ],
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    check = next(item for item in checks if item.key.startswith("command."))
    assert check.outcome == CheckOutcome.FAILED
    assert check.details["baseline_outcome"] == "PASSED"
    assert check.details["regression"] is True
    assert report.decision == TaskDecision.REJECTED


def test_patch_drift_is_detected_and_rejected(services, indexed, task_model):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    # Simulate a file changed after the patch was captured (another process, or a
    # command escaping the tool gateway).
    target = run.workspace.path + "/utils.py"
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\nDRIFTED = True\n")
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    reproducible = next(item for item in checks if item.key == "patch.reproducible")
    assert reproducible.outcome == CheckOutcome.FAILED
    assert any(finding.code == "PATCH_DRIFT" for finding in reproducible.findings)
    assert report.decision == TaskDecision.REJECTED


def test_extra_command_check_without_approval_is_blocked(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[],
        require_command=False,
    )
    report, checks = services.task_service.verify_run(
        run.run.id,
        extra_checks=[command_check(CHECK_OK, key="command.unapproved", timeout_seconds=30)],
        include_review=False,
    )
    check = next(item for item in checks if item.key == "command.unapproved")
    assert check.outcome == CheckOutcome.BLOCKED
    assert "not authorized" in check.summary
    assert report.decision == TaskDecision.BLOCKED


def test_no_declared_checks_yields_inconclusive(services, indexed, task_model):
    run = make_reviewing_run(services, indexed, task_model, checks=[])
    report, _checks = services.task_service.verify_run(run.run.id, include_review=False)
    assert report.decision == TaskDecision.INCONCLUSIVE
    assert any("no functional check" in item for item in report.blocking)


def test_ml_invariant_that_cannot_run_is_skipped_not_passed(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[
            invariant_check(
                "forward_smoke",
                module="utils",
                callable_name="count_tokens",
                input_shape=[1, 4, 32],
                criteria=["AC1"],
                timeout_seconds=60,
            )
        ],
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    invariant = next(item for item in checks if item.key.startswith("invariant."))
    assert invariant.outcome == CheckOutcome.SKIPPED, (invariant.summary, invariant.details)
    # Assert the stable contract, not a host-dependent message substring. Whether
    # torch is installed changes *why* this cannot run ("torch is not importable"
    # vs. "factory could not be called"), but never the verdict: a check that
    # cannot produce a verdict is SKIPPED, verification is INCONCLUSIVE and the
    # acceptance criterion stays UNVERIFIED.
    assert invariant.summary.strip()
    assert report.decision == TaskDecision.INCONCLUSIVE
    assert report.criteria[0].status.value == "UNVERIFIED"


def test_plan_declares_reviewer_and_builtins(services, indexed, task_model):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
    )
    plan = services.verification.declared_checks(run.run.id)
    assert len(plan) == 1 and plan[0].kind.value == "COMMAND"
    proposals = services.verification.proposals(run.run.id)
    assert all(item.key for item in proposals)
    # Reviewer is recorded as a check with source=model_review and required=False.
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    reviewer = next(item for item in checks if item.key == "review.independent")
    assert reviewer.required is False
    assert reviewer.outcome == CheckOutcome.SKIPPED
    assert "disabled" in reviewer.summary


_UNSET = object()


def attach_external_comparison(
    services,
    indexed,
    task_id,
    baseline_values,
    candidate_values,
    baseline_snapshot=None,
    candidate_snapshot=None,
    candidate_task_id=_UNSET,
):
    service = services.experiment_service
    metric = MetricSpec(
        key="loss",
        direction=MetricDirection.LOWER_IS_BETTER,
        aggregation=MetricAggregation.LAST,
        primary=True,
    )

    def arm(role, name, values):
        arm_task_id = task_id
        if role == ExperimentRole.CANDIDATE and candidate_task_id is not _UNSET:
            arm_task_id = candidate_task_id
        definition = service.create_definition(
            project_id=indexed.project.id,
            snapshot_id=(
                (baseline_snapshot if role == ExperimentRole.BASELINE else candidate_snapshot)
                or indexed.snapshot
            ).id,
            name=name,
            hypothesis="the candidate reduces loss",
            role=role,
            profile=ExperimentProfile.STATIC,
            command=[],
            seeds=sorted(values),
            timeout_seconds=30,
            metrics=[metric],
            metrics_source=MetricsSource(kind=MetricsSourceKind.IMPORTED),
            task_id=arm_task_id,
        )
        service.approve(definition.id, reason="test pre-registration")
        for seed, value in values.items():
            service.record_external(
                definition.id,
                seed=seed,
                metrics_text=json.dumps({"metrics": {"loss": value}}),
                note="produced on the test runner outside the workbench",
            )
        return definition

    baseline = arm(ExperimentRole.BASELINE, "baseline", baseline_values)
    candidate = arm(ExperimentRole.CANDIDATE, "candidate", candidate_values)
    return service.compare(candidate.id, baseline_id=baseline.id)


def test_required_experiment_that_is_missing_is_inconclusive(
    services, indexed, task_model
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
        require_experiment=True,
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    check = next(item for item in checks if item.key == "experiment.evidence")
    assert check.required is True
    assert check.outcome == CheckOutcome.INCONCLUSIVE
    assert check.details["mandatory"] is True
    assert report.decision == TaskDecision.INCONCLUSIVE
    assert any("experiment.evidence" in item for item in report.blocking)
    with pytest.raises(TaskError, match="INCONCLUSIVE"):
        services.task_service.finish_run(
            run.run.id, accept=True, reason="no experiment was run"
        )


def test_attached_comparison_satisfies_gate_f_and_is_embedded_in_the_bundle(
    services, indexed, task_model, tmp_path
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
        require_experiment=True,
    )
    comparison = attach_external_comparison(
        services,
        indexed,
        run.task.id,
        {1: 1.00, 2: 1.01, 3: 0.99},
        {1: 0.90, 2: 0.92, 3: 0.89},
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    check = next(item for item in checks if item.key == "experiment.evidence")
    assert check.outcome == CheckOutcome.PASSED, (check.summary, check.findings)
    assert check.details["comparison_id"] == comparison.id
    assert check.details["observations"] == 6
    assert check.details["comparable"] is True
    assert any(
        finding.code == "EXPERIMENT_IMPORTED_RUNS" for finding in check.findings
    )
    assert report.decision == TaskDecision.COMPLETED_WITH_LIMITATIONS
    assert report.details["markdown_artifact_id"]
    markdown = services.artifacts.read_text(report.details["markdown_artifact_id"])
    assert "## Experiment evidence" in markdown
    assert comparison.verdict.value in markdown
    payload = json.loads(services.artifacts.read_bytes(report.report_artifact_id))
    assert payload["experiment"]["comparison_id"] == comparison.id
    assert payload["evidence_classification"]["experimentally_observed"]["value"] is True

    destination = tmp_path / "verification-bundle"
    services.verification.export_bundle(run.run.id, destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    names = {entry["name"] for entry in manifest["files"]}
    assert "experiments/comparison.json" in names
    assert "experiments/report.md" in names
    assert any(name.startswith("experiments/manifests/") for name in names)
    result = verify_bundle(destination)
    assert result.ok, result.errors


def test_experiment_on_a_different_commit_is_not_evidence(
    services, indexed, task_model, repo
):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
        require_experiment=True,
    )
    (repo / "later.py").write_text("VALUE = 2\n", encoding="utf-8")
    from tests.conftest import git

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later revision")
    second = services.repository_service.create_snapshot(indexed.repository).snapshot
    services.indexer.index(second.id)
    attach_external_comparison(
        services,
        indexed,
        run.task.id,
        {1: 1.00, 2: 1.01, 3: 0.99},
        {1: 0.90, 2: 0.92, 3: 0.89},
        candidate_snapshot=second,
        candidate_task_id=None,
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    check = next(item for item in checks if item.key == "experiment.evidence")
    assert check.outcome == CheckOutcome.INCONCLUSIVE
    assert check.details["commit_mismatch"] is True
    assert any(
        finding.code == "EXPERIMENT_COMMIT_MISMATCH" for finding in check.findings
    )
    assert report.decision == TaskDecision.INCONCLUSIVE


def test_baseline_advantage_fails_a_required_gate(services, indexed, task_model):
    run = make_reviewing_run(
        services,
        indexed,
        task_model,
        checks=[command_check(CHECK_OK, criteria=["AC1"], timeout_seconds=30)],
        require_experiment=True,
    )
    attach_external_comparison(
        services,
        indexed,
        run.task.id,
        {1: 1.00, 2: 1.01, 3: 0.99},
        {1: 1.10, 2: 1.12, 3: 1.09},
    )
    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    check = next(item for item in checks if item.key == "experiment.evidence")
    assert check.outcome == CheckOutcome.FAILED
    assert any(
        finding.code == "EXPERIMENT_FAVORS_BASELINE" for finding in check.findings
    )
    assert report.decision == TaskDecision.REJECTED

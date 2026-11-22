"""Tests for the frozen evaluation-harness integrity gate (WP-R2I-2).

Pure-function cases inject a deterministic ``base_reader``; the end-to-end cases
go through the real snapshot index, isolated worker worktree and verification
attempt so the declaration really comes from the immutable contract.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mayaworkbench.domain.enums import (
    ApprovalAction,
    CheckKind,
    CheckOutcome,
    FindingSeverity,
    TaskDecision,
    TaskType,
    VerificationGate,
    WorkflowState,
)
from mayaworkbench.errors import VerificationError
from mayaworkbench.verification.evaluation_integrity import (
    APPROVED_TREATMENT,
    TAMPERING,
    UNCHANGED,
    EvaluationChange,
    as_check_details,
    evaluate_harness_integrity,
    render_summary,
)
from mayaworkbench.verification.registry import command_check
from mayaworkbench.worker import Worker

BASE = b"def metric(batch):\n    return batch[0]\n"
CANDIDATE = b"def metric(batch):\n    return 0.0\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- pure function
def test_unchanged_path_is_classified_unchanged(tmp_path: Path):
    (tmp_path / "metric.py").write_bytes(BASE)
    changes = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={},
        base_reader=lambda _path: BASE,
        candidate_root=tmp_path,
    )
    assert len(changes) == 1
    change = changes[0]
    assert change.status == UNCHANGED
    assert change.base_sha256 == sha(BASE)
    assert change.candidate_sha256 == sha(BASE)
    details = as_check_details(changes)
    assert details["status"] == "PASSED"
    assert details["counts"] == {
        UNCHANGED: 1,
        APPROVED_TREATMENT: 0,
        TAMPERING: 0,
    }
    assert details["frozen_harness_manifest"] == {"metric.py": sha(BASE)}
    assert "0 tampering - PASSED" in render_summary(changes)


def test_exact_hash_approval_is_recorded_as_approved_treatment(tmp_path: Path):
    (tmp_path / "metric.py").write_bytes(CANDIDATE)
    changes = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={"metric.py": sha(CANDIDATE)},
        base_reader=lambda _path: BASE,
        candidate_root=tmp_path,
    )
    change = changes[0]
    assert change.status == APPROVED_TREATMENT
    assert change.base_sha256 == sha(BASE)
    assert change.candidate_sha256 == sha(CANDIDATE)
    assert "pre-approved" in change.reason
    assert as_check_details(changes)["counts"][APPROVED_TREATMENT] == 1
    assert as_check_details(changes)["status"] == "PASSED"
    assert "1 approved treatment(s)" in render_summary(changes)


def test_unapproved_change_is_tampering(tmp_path: Path):
    (tmp_path / "metric.py").write_bytes(CANDIDATE)
    changes = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={},
        base_reader=lambda _path: BASE,
        candidate_root=tmp_path,
    )
    assert changes[0].status == TAMPERING
    details = as_check_details(changes)
    assert details["status"] == "FAILED"
    assert details["tampering_paths"] == ["metric.py"]
    assert "Tampered paths: metric.py" in render_summary(changes)


def test_wrong_hash_approval_is_still_tampering(tmp_path: Path):
    (tmp_path / "metric.py").write_bytes(CANDIDATE)
    changes = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={"metric.py": sha(b"some other content")},
        base_reader=lambda _path: BASE,
        candidate_root=tmp_path,
    )
    assert changes[0].status == TAMPERING
    assert "does not match" in changes[0].reason


def test_generic_permission_is_never_accepted(tmp_path: Path):
    """A wildcard/scope-wide approval must not authorise a path edit."""
    (tmp_path / "metric.py").write_bytes(CANDIDATE)
    changes = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={"*": sha(CANDIDATE), "evaluation": sha(CANDIDATE)},
        base_reader=lambda _path: BASE,
        candidate_root=tmp_path,
    )
    assert changes[0].status == TAMPERING


@pytest.mark.parametrize("declared", ["metric.py", "../outside/secret.py", "/etc/passwd"])
def test_escaping_symlink_is_refused_before_hashing(tmp_path: Path, declared: str):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"easier yardstick")
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "metric.py").symlink_to(outside / "secret.py")

    def forbidden_reader(_path: str) -> bytes:
        raise AssertionError("base_reader must not run for a refused path")

    with pytest.raises(VerificationError, match="outside the candidate workspace"):
        evaluate_harness_integrity(
            declared_paths=[declared],
            approved_changes={},
            base_reader=forbidden_reader,
            candidate_root=root,
        )


def test_missing_candidate_file_is_tampering(tmp_path: Path):
    changes = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={},
        base_reader=lambda _path: BASE,
        candidate_root=tmp_path,
    )
    assert changes[0].status == TAMPERING
    assert changes[0].candidate_sha256 is None
    assert changes[0].base_sha256 == sha(BASE)
    assert "missing" in changes[0].reason


def test_new_declared_file_needs_exact_hash_approval(tmp_path: Path):
    (tmp_path / "metric.py").write_bytes(CANDIDATE)
    unapproved = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={},
        base_reader=lambda _path: None,
        candidate_root=tmp_path,
    )
    assert unapproved[0].status == TAMPERING
    assert "newly created" in unapproved[0].reason

    approved = evaluate_harness_integrity(
        declared_paths=["metric.py"],
        approved_changes={"metric.py": sha(CANDIDATE)},
        base_reader=lambda _path: None,
        candidate_root=tmp_path,
    )
    assert approved[0].status == APPROVED_TREATMENT
    assert approved[0].candidate_sha256 == sha(CANDIDATE)


def test_multiple_paths_are_aggregated(tmp_path: Path):
    (tmp_path / "unchanged.py").write_bytes(BASE)
    (tmp_path / "treated.py").write_bytes(CANDIDATE)
    (tmp_path / "tampered.py").write_bytes(CANDIDATE + b"# bad\n")
    # missing.py is intentionally absent.
    changes = evaluate_harness_integrity(
        declared_paths=[
            "unchanged.py",
            "treated.py",
            "tampered.py",
            "missing.py",
            "unchanged.py",  # duplicate declarations collapse
        ],
        approved_changes={"treated.py": sha(CANDIDATE)},
        base_reader=lambda path: BASE if path != "treated.py" else BASE,
        candidate_root=tmp_path,
    )
    assert [item.path for item in changes] == [
        "unchanged.py",
        "treated.py",
        "tampered.py",
        "missing.py",
    ]
    details = as_check_details(changes)
    assert details["counts"] == {
        UNCHANGED: 1,
        APPROVED_TREATMENT: 1,
        TAMPERING: 2,
    }
    assert details["status"] == "FAILED"
    assert details["tampering_paths"] == ["tampered.py", "missing.py"]
    assert all(isinstance(item, EvaluationChange) for item in changes)


def test_change_model_is_frozen_and_validated():
    change = EvaluationChange(path="metric.py", status=UNCHANGED)
    with pytest.raises(Exception):
        change.path = "other.py"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        EvaluationChange(path="metric.py", status="WAIVED_BY_MODEL")


# --------------------------------------------------------------- end to end
PATCH_UTILS = (
    "--- a/utils.py\n"
    "+++ b/utils.py\n"
    "@@ -8,3 +8,3 @@\n"
    " def count_tokens(text: str) -> int:\n"
    '     """Count whitespace-delimited tokens."""\n'
    "-    return len(TOKEN_PATTERN.findall(text))\n"
    "+    return len(text.split())\n"
)


def make_evaluation_run(services, indexed, provider, *, details: dict):
    provider.actions = [
        {
            "thought": "Read the target.",
            "tool": "read_file",
            "arguments": {"path": "utils.py"},
        },
        {
            "thought": "Make the minimal change.",
            "tool": "apply_patch",
            "arguments": {"patch": PATCH_UTILS},
        },
        {
            "thought": "Finished.",
            "tool": "finish",
            "arguments": {
                "summary": "Changed count_tokens.",
                "limitations": [],
                "checks_run": [],
                "remaining_work": [],
            },
        },
    ]
    spec = command_check(
        "python -c \"print('ok')\"",
        key="command.eval_smoke",
        criteria=["AC1"],
        timeout_seconds=30,
    ).model_copy(update={"details": details})
    task, contract = services.task_service.create_task(
        project_id=indexed.project.id,
        title="Verify evaluation integrity",
        goal="Change count_tokens and keep the evaluation harness frozen",
        task_type=TaskType.ENGINEERING_CHANGE,
        snapshot_id=indexed.snapshot.id,
        allowed_paths=["utils.py"],
        acceptance_criteria=["count_tokens returns the number of words"],
        verification_checks=[spec],
        required_approvals=[
            ApprovalAction.WRITE_ACCESS,
            ApprovalAction.COMMAND_EXECUTION,
        ],
        constraints={
            "max_steps": 10,
            "max_tool_calls": 20,
            "max_model_tokens": 20_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
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


def test_unapproved_evaluation_change_fails_and_rejects(
    services, indexed, task_model
):
    run = make_evaluation_run(
        services, indexed, task_model, details={"evaluation_paths": ["README.md"]}
    )
    candidate = Path(run.workspace.path) / "README.md"
    candidate.write_bytes(candidate.read_bytes() + b"\nTAMPERED METRIC\n")

    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    by_key = {item.key: item for item in checks}
    check = by_key["evaluation.integrity"]
    assert check.required is True
    assert check.gate == VerificationGate.CHANGE
    assert check.kind == CheckKind.DETERMINISTIC
    assert check.outcome == CheckOutcome.FAILED
    assert check.details["counts"][TAMPERING] == 1
    assert check.details["frozen_harness_manifest"]["README.md"] == sha(
        (indexed.repo / "README.md").read_bytes()
    )
    findings = [item for item in check.findings if item.code == "EVAL_TAMPERING"]
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.ERROR
    assert findings[0].source == "deterministic"
    assert findings[0].path == "README.md"
    assert report.decision == TaskDecision.REJECTED
    assert any("evaluation.integrity failed" in item for item in report.blocking)


def test_exact_hash_approved_change_does_not_fail_the_check(
    services, indexed, task_model
):
    base_bytes = (indexed.repo / "README.md").read_bytes()
    treated = base_bytes + b"\nTREATED METRIC\n"
    digest = sha(treated)
    run = make_evaluation_run(
        services,
        indexed,
        task_model,
        details={
            "evaluation_paths": ["README.md"],
            "approved_evaluation_changes": {"README.md": digest},
        },
    )
    (Path(run.workspace.path) / "README.md").write_bytes(treated)

    report, checks = services.task_service.verify_run(run.run.id, include_review=False)
    check = next(item for item in checks if item.key == "evaluation.integrity")
    assert check.outcome == CheckOutcome.PASSED
    assert check.details["counts"][APPROVED_TREATMENT] == 1
    record = check.details["changes"][0]
    assert record["status"] == APPROVED_TREATMENT
    assert record["candidate_sha256"] == digest
    assert record["base_sha256"] == sha(base_bytes)
    assert not any(item.code == "EVAL_TAMPERING" for item in check.findings)
    assert report.decision != TaskDecision.REJECTED


def test_no_declared_paths_is_not_applicable_and_never_fails(
    services, indexed, task_model
):
    run = make_evaluation_run(services, indexed, task_model, details={})
    _report, checks = services.task_service.verify_run(
        run.run.id, include_review=False
    )
    check = next(item for item in checks if item.key == "evaluation.integrity")
    assert check.outcome == CheckOutcome.NOT_APPLICABLE
    assert check.required is False
    assert check.details["status"] == "NOT_APPLICABLE"

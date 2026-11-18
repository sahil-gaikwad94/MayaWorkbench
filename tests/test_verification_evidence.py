"""Evidence-layer tests: decisions, test integrity, reviewer grounding, bundles.

These are pure-function tests plus one end-to-end export. The workflow-level
tests live in ``test_verification.py``.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mayaworkbench.domain.enums import (
    ApprovalAction,
    CheckKind,
    CheckOutcome,
    CriterionStatus,
    FindingSeverity,
    TaskDecision,
    TaskType,
    VerificationGate,
    VerificationMethod,
    WorkflowState,
)
from mayaworkbench.domain.models import (
    CheckSpec,
    CriterionAssessment,
    DiffFile,
    InvariantSpec,
    Task,
    TaskContract,
    TaskDiff,
    TaskScope,
    VerificationCheck,
    VerificationFinding,
    WorkflowRun,
)
from mayaworkbench.models.base import ModelResponse
from mayaworkbench.verification.bundle import write_bundle, verify_bundle
from mayaworkbench.verification.integrity import (
    analyze_test_integrity,
    integrity_outcome,
)
from mayaworkbench.verification.registry import (
    invariant_check,
    render_invariant,
)
from mayaworkbench.verification.report import decide
from mayaworkbench.verification.reviewer import run_independent_review

SHA = "a" * 40
DIGEST = "b" * 64


def make_run(state: WorkflowState = WorkflowState.REVIEWING) -> WorkflowRun:
    return WorkflowRun(
        id="run_1",
        task_id="task_1",
        contract_id="contract_1",
        project_id="project_1",
        repository_id="repo_1",
        snapshot_id="snap_1",
        base_commit_sha=SHA,
        state=state,
    )


def make_check(
    key: str,
    outcome: CheckOutcome,
    *,
    required: bool = True,
    kind: CheckKind = CheckKind.COMMAND,
    criteria: tuple[str, ...] = ("AC1",),
    findings: tuple[VerificationFinding, ...] = (),
    details: dict | None = None,
    gate: VerificationGate = VerificationGate.FUNCTIONAL,
    method: VerificationMethod = VerificationMethod.DETERMINISTIC_TEST,
) -> VerificationCheck:
    return VerificationCheck(
        id="check_" + key.replace(".", "_"),
        verification_id="ver_1",
        run_id="run_1",
        task_id="task_1",
        project_id="project_1",
        key=key,
        gate=gate,
        kind=kind,
        title=key,
        method=method,
        outcome=outcome,
        required=required,
        criteria=list(criteria),
        details=details or {},
        findings=list(findings),
    )


def make_spec(key: str = "command.unit") -> CheckSpec:
    return CheckSpec(
        key=key,
        gate=VerificationGate.FUNCTIONAL,
        kind=CheckKind.COMMAND,
        title="unit",
        argv=["python", "-c", "pass"],
        criteria=["AC1"],
    )


def verified() -> CriterionAssessment:
    return CriterionAssessment(
        criterion_id="AC1", text="count_tokens counts words", status=CriterionStatus.VERIFIED
    )


def test_decision_matrix() -> None:
    run = make_run()
    passed = make_check("command.unit", CheckOutcome.PASSED)

    decision, blocking, _ = decide(
        run=run,
        checks=[passed],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.ACCEPTED, blocking

    failed = make_check("command.unit", CheckOutcome.FAILED)
    decision, blocking, _ = decide(
        run=run,
        checks=[failed],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.REJECTED
    assert any("failed" in item for item in blocking)

    # A failure that also happens on the base revision is a limitation, not a
    # rejection of this patch.
    preexisting = make_check(
        "command.unit", CheckOutcome.FAILED, details={"preexisting_failure": True}
    )
    decision, _, limitations = decide(
        run=run,
        checks=[preexisting],
        criteria=[CriterionAssessment(criterion_id="AC1", text="x")],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.COMPLETED_WITH_LIMITATIONS
    assert any("already failed before the change" in item for item in limitations)

    blocked = make_check("command.unit", CheckOutcome.BLOCKED)
    decision, _, _ = decide(
        run=run,
        checks=[blocked],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.BLOCKED

    skipped = make_check("command.unit", CheckOutcome.SKIPPED)
    decision, _, _ = decide(
        run=run,
        checks=[skipped],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.INCONCLUSIVE

    # Deterministic checks alone are never accepted without a functional check.
    builtin = make_check(
        "patch.reproducible",
        CheckOutcome.PASSED,
        kind=CheckKind.DETERMINISTIC,
        criteria=(),
        gate=VerificationGate.CHANGE,
        method=VerificationMethod.SOURCE_INSPECTION,
    )
    decision, blocking, _ = decide(
        run=run,
        checks=[builtin],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[],
    )
    assert decision == TaskDecision.INCONCLUSIVE
    assert any("no functional check" in item for item in blocking)

    warning = VerificationFinding(
        severity=FindingSeverity.WARNING,
        code="SKIP_ADDED",
        message="A test was skipped",
        path="tests/test_x.py",
    )
    warned = make_check("command.unit", CheckOutcome.PASSED, findings=(warning,))
    decision, _, _ = decide(
        run=run,
        checks=[warned],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.COMPLETED_WITH_LIMITATIONS

    review = {
        "assessment": "UNSOUND",
        "findings": [
            {
                "severity": "BLOCKER",
                "code": "UNSOUND_CHANGE",
                "message": "The change cannot work",
                "source": "model_review",
            }
        ],
    }
    decision, _, _ = decide(
        run=run,
        checks=[passed],
        criteria=[verified()],
        reviewer=review,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.NEEDS_HUMAN_DECISION

    cancelled = make_run(WorkflowState.CANCELLED)
    decision, _, _ = decide(
        run=cancelled,
        checks=[passed],
        criteria=[verified()],
        reviewer=None,
        declared_specs=[make_spec()],
    )
    assert decision == TaskDecision.INCONCLUSIVE


def test_integrity_flags_weakened_tests_with_locations() -> None:
    baseline = {
        "tests/test_math.py": (
            "def test_add():\n"
            "    assert 1 + 1 == 2\n"
            "\n"
            "def test_mul():\n"
            "    assert 2 * 2 == 4\n"
            "    assert 3 * 3 == 9\n"
        )
    }
    candidate = {
        "tests/test_math.py": (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='flaky')\n"
            "def test_add():\n"
            "    assert True\n"
            "\n"
            "def test_mul():\n"
            "    assert 2 * 2 == 4\n"
        )
    }
    findings = analyze_test_integrity(baseline, candidate)
    codes = {finding.code for finding in findings}
    assert {"SKIP_ADDED", "ASSERTIONS_REDUCED", "TAUTOLOGICAL_ASSERTION"} <= codes
    assert all(finding.path == "tests/test_math.py" for finding in findings)
    assert all(finding.line for finding in findings)
    assert integrity_outcome(findings) == "FAILED"


def test_integrity_flags_deleted_tests_and_weakened_config() -> None:
    findings = analyze_test_integrity(
        {"tests/test_math.py": "def test_add():\n    assert 1 + 1 == 2\n"},
        {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-k not slow'\n"},
    )
    codes = {finding.code for finding in findings}
    assert "TEST_FILE_DELETED" in codes
    assert "TEST_CONFIG_WEAKENED" in codes
    assert integrity_outcome(findings) == "FAILED"


def test_integrity_clean_change_has_no_findings() -> None:
    source = "def test_add():\n    assert 1 + 1 == 2\n"
    assert analyze_test_integrity({"tests/a.py": source}, {"tests/a.py": source}) == []
    assert integrity_outcome([]) == "PASSED"


def test_every_invariant_template_renders_a_compilable_script() -> None:
    names = [
        "forward_smoke",
        "shape_preserved",
        "gradients_finite",
        "no_nan_output",
        "causal_mask_invariance",
        "deterministic_eval",
        "parameter_update",
        "dtype_device_consistency",
    ]
    for name in names:
        spec = InvariantSpec(
            name=name,
            module="model",
            callable="forward",
            input_shape=[1, 4, 8],
            parameters={"d_model": 8},
        )
        script = render_invariant(spec)
        compile(script, f"<invariant:{name}>", "exec")
        assert "torch" in script
        assert "invariant" in script.lower()


def test_invariant_check_requires_a_template_and_callable() -> None:
    with pytest.raises(ValidationError):
        invariant_check("not_a_template", module="model", callable_name=None)
    with pytest.raises(ValidationError):
        invariant_check("forward_smoke", module="model", callable_name=None)
    spec = invariant_check(
        "forward_smoke", module="model", callable_name="forward", input_shape=[1, 4, 8]
    )
    assert spec.kind == CheckKind.ML_INVARIANT
    assert spec.invariant is not None and spec.invariant.name == "forward_smoke"
    assert spec.required is True


class _Invocations:
    def list_for_snapshot(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []


class _Gateway:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def structured(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return (
            ModelResponse(
                text=json.dumps(self.payload),
                structured=self.payload,
                provider="stub",
                model="stub-reviewer",
            ),
            SimpleNamespace(id="inv_review"),
        )


def _review_fixture():
    patch = (
        "diff --git a/utils.py b/utils.py\n"
        "--- a/utils.py\n"
        "+++ b/utils.py\n"
        "@@ -8,3 +8,3 @@\n"
        " def count_tokens(text: str) -> int:\n"
        '     """Count whitespace-delimited tokens."""\n'
        "-    return len(TOKEN_PATTERN.findall(text))\n"
        "+    return len(text.split())\n"
    )
    task = Task(
        id="task_1",
        project_id="project_1",
        title="Change count_tokens",
        task_type=TaskType.ENGINEERING_CHANGE,
        goal="Count words",
    )
    contract = TaskContract(
        id="contract_1",
        task_id="task_1",
        project_id="project_1",
        version=1,
        task_type=TaskType.ENGINEERING_CHANGE,
        goal="Count words",
        repository_id="repo_1",
        snapshot_id="snap_1",
        scope=TaskScope(allowed_paths=["utils.py"], base_commit_sha=SHA),
        acceptance_criteria=["count_tokens counts words"],
        required_approvals=[ApprovalAction.WRITE_ACCESS],
        content_sha256=DIGEST,
    )
    run = WorkflowRun(
        id="run_1",
        task_id="task_1",
        contract_id="contract_1",
        project_id="project_1",
        repository_id="repo_1",
        snapshot_id="snap_1",
        base_commit_sha=SHA,
        state=WorkflowState.REVIEWING,
    )
    diff = TaskDiff(
        id="diff_1",
        run_id="run_1",
        task_id="task_1",
        project_id="project_1",
        workspace_id="ws_1",
        base_commit_sha=SHA,
        patch_artifact_id="art_patch",
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        files=[DiffFile(path="utils.py", additions=1, deletions=1)],
    )
    check = make_check("command.unit", CheckOutcome.PASSED)
    # ``run_independent_review`` records its own spec internally; this argument is
    # the declared-check descriptor and follows the normal COMMAND shape.
    spec = CheckSpec(
        key="review.independent",
        gate=VerificationGate.REVIEW,
        kind=CheckKind.COMMAND,
        title="Independent review",
        required=False,
        argv=["true"],
    )
    return dict(
        task=task,
        contract=contract,
        run=run,
        diff=diff,
        patch=patch,
        checks=[check],
        spec=spec,
    )


def test_reviewer_rejects_findings_outside_the_diff() -> None:
    fixture = _review_fixture()
    payload = {
        "assessment": "CONCERNS",
        "summary": "One grounded concern and one invented one.",
        "findings": [
            {
                "severity": "BLOCKER",
                "code": "INVENTED_FILE",
                "message": "This file is broken",
                "path": "src/invented.py",
                "rationale": "I looked at a file I was not given.",
            },
            {
                "severity": "INFO",
                "code": "NOTE",
                "message": "The change drops the compiled pattern",
                "path": "utils.py",
                "line": 10,
                "rationale": "The replacement allocates a list.",
            },
        ],
        "unverified_assumptions": ["No benchmark was run"],
    }
    result = run_independent_review(
        gateway=_Gateway(payload),
        invocations=_Invocations(),  # type: ignore[arg-type]
        workspace_path=None,
        criteria=[("AC1", "count_tokens counts words")],
        integrity_findings=[],
        **fixture,
    )
    assert result.outcome == CheckOutcome.PASSED
    assert result.details["rejected_findings"] == [
        {"code": "INVENTED_FILE", "path": "src/invented.py", "reason": "path not in diff"}
    ]
    ungrounded = [item for item in result.findings if item.code == "MODEL_REVIEW_UNGROUNDED"]
    assert ungrounded and ungrounded[0].severity == FindingSeverity.INFO
    assert result.invocation_id == "inv_review"
    assert result.details["independent"] is True


def test_reviewer_blocker_fails_the_review_but_not_a_passed_check() -> None:
    fixture = _review_fixture()
    payload = {
        "assessment": "UNSOUND",
        "summary": "The replacement changes semantics.",
        "findings": [
            {
                "severity": "BLOCKER",
                "code": "SEMANTIC_CHANGE",
                "message": "Regex tokens are not whitespace tokens.",
                "path": "utils.py",
                "line": 10,
            }
        ],
        "unverified_assumptions": [],
    }
    result = run_independent_review(
        gateway=_Gateway(payload),
        invocations=_Invocations(),  # type: ignore[arg-type]
        workspace_path=None,
        criteria=[("AC1", "count_tokens counts words")],
        integrity_findings=[],
        **fixture,
    )
    assert result.outcome == CheckOutcome.FAILED
    assert result.details["blocking_findings"] == 1
    assert fixture["checks"][0].outcome == CheckOutcome.PASSED


def _bundle_payloads() -> dict[str, bytes]:
    patch = b"--- a/utils.py\n+++ b/utils.py\n@@ -1 +1 @@\n-a\n+b\n"
    checks = [
        {
            "id": "check_1",
            "key": "command.unit",
            "outcome": "PASSED",
            "artifact_id": None,
        }
    ]
    criteria = [{"criterion_id": "AC1", "status": "VERIFIED"}]
    report = {
        "decision": "ACCEPTED",
        "blocking": [],
        "check_ids": ["check_1"],
        "criteria": criteria,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    return {
        "report.json": json.dumps(report, sort_keys=True).encode(),
        "report.md": b"# Verification report\n",
        "patch.diff": patch,
        "checks.json": json.dumps(checks, sort_keys=True).encode(),
        "criteria.json": json.dumps(criteria, sort_keys=True).encode(),
    }


def test_bundle_round_trips_and_detects_tampering(tmp_path) -> None:
    bundle = write_bundle(tmp_path / "bundle", _bundle_payloads())
    result = verify_bundle(bundle.path)
    assert result.ok, result.errors
    assert result.report["decision"] == "ACCEPTED"

    # Any edited byte invalidates the bundle.
    report_path = bundle.path / "report.json"
    report_path.write_bytes(report_path.read_bytes().replace(b"ACCEPTED", b"REJECTED"))
    tampered = verify_bundle(bundle.path)
    assert not tampered.ok
    assert any("hash mismatch for report.json" in error for error in tampered.errors)

    # A rewritten manifest can hide that, but an unlisted extra file cannot: the
    # verifier enumerates the directory rather than trusting the manifest.
    bundle2 = write_bundle(tmp_path / "bundle2", _bundle_payloads())
    (bundle2.path / "extra.txt").write_text("smuggled", encoding="utf-8")
    extra = verify_bundle(bundle2.path)
    assert not extra.ok
    assert any("not listed" in error for error in extra.errors)

    bundle3 = write_bundle(tmp_path / "bundle3", _bundle_payloads())
    (bundle3.path / "patch.diff").unlink()
    missing = verify_bundle(bundle3.path)
    assert not missing.ok
    assert any("patch.diff" in error for error in missing.errors)

    # Bundles are never overwritten.
    from mayaworkbench.errors import VerificationError

    with pytest.raises(VerificationError):
        write_bundle(bundle3.path, _bundle_payloads())


def test_bundle_rejects_report_that_disagrees_with_checks(tmp_path) -> None:
    payloads = _bundle_payloads()
    report = json.loads(payloads["report.json"])
    report["check_ids"] = ["check_other"]
    payloads["report.json"] = json.dumps(report, sort_keys=True).encode()
    bundle = write_bundle(tmp_path / "bundle", payloads)
    result = verify_bundle(bundle.path)
    assert not result.ok
    assert any("check_ids" in error for error in result.errors)

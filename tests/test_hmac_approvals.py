"""Optional HMAC integrity for approval artifacts; server-side consume stays authoritative."""

from __future__ import annotations

import pytest

from mayaworkbench.domain.enums import ApprovalStatus, WorkflowState
from mayaworkbench.errors import ApprovalError
from mayaworkbench.tasks.approval_tokens import (
    approval_message,
    sign_approval,
    verify_approval,
)
from mayaworkbench.tasks.approvals import expected_write_sha

from .test_task_workflow import finish_action, make_task


def test_approval_tokens_fail_closed_without_key(monkeypatch):
    monkeypatch.delenv("MAYA_APPROVAL_KEY", raising=False)
    message = approval_message(
        approval_id="apr_x", spec_sha256="b" * 64, nonce="n" * 48,
        expires_at="2030-01-01T00:00:00+00:00", action="WRITE_ACCESS",
    )
    assert sign_approval(message) is None
    assert verify_approval(message, {"algorithm": "HMAC-SHA256", "signature": "x" * 64}) is False


def test_approval_tokens_detect_tampering(monkeypatch):
    monkeypatch.setenv("MAYA_APPROVAL_KEY", "a-test-signing-key")
    args = dict(
        approval_id="apr_x", spec_sha256="b" * 64, nonce="n" * 48,
        expires_at="2030-01-01T00:00:00+00:00", action="WRITE_ACCESS",
    )
    message = approval_message(**args)
    envelope = sign_approval(message)
    assert envelope is not None and len(envelope["signature"]) == 64
    assert verify_approval(message, envelope) is True
    assert verify_approval(approval_message(**{**args, "nonce": "changed"}), envelope) is False


def test_signed_approval_is_consumed_once(services, indexed, task_model, monkeypatch):
    monkeypatch.setenv("MAYA_APPROVAL_KEY", "a-test-signing-key")
    task, contract = make_task(services, indexed)
    task_model.actions = [finish_action()]
    result = services.task_service.start_task(task.id)
    assert result.run.state == WorkflowState.AWAITING_APPROVAL
    write = next(item for item in result.approvals if item.action.value == "WRITE_ACCESS")
    assert write.details.get("approval_hmac", {}).get("algorithm") == "HMAC-SHA256"
    services.task_service.approve(write.id)
    plan = services.plans.get(result.run.plan_id)
    expected = expected_write_sha(
        run=services.runs.get(result.run.id), contract=contract, plan=plan
    )
    consumed = services.approval_service.consume(approval_id=write.id, spec_sha256=expected)
    assert consumed.status == ApprovalStatus.CONSUMED
    with pytest.raises(ApprovalError, match="not APPROVED"):
        services.approval_service.consume(approval_id=write.id, spec_sha256=expected)


def test_missing_signing_key_blocks_consumption(services, indexed, task_model, monkeypatch):
    monkeypatch.setenv("MAYA_APPROVAL_KEY", "a-test-signing-key")
    task, _contract = make_task(services, indexed)
    task_model.actions = [finish_action()]
    result = services.task_service.start_task(task.id)
    write = next(item for item in result.approvals if item.action.value == "WRITE_ACCESS")
    services.task_service.approve(write.id)
    monkeypatch.delenv("MAYA_APPROVAL_KEY", raising=False)
    from mayaworkbench.tasks.approvals import expected_write_sha

    plan = services.plans.get(result.run.plan_id)
    expected = expected_write_sha(
        run=services.runs.get(result.run.id),
        contract=services.contracts.get(result.run.contract_id),
        plan=plan,
    )
    with pytest.raises(ApprovalError, match="signature is invalid"):
        services.approval_service.consume(approval_id=write.id, spec_sha256=expected)

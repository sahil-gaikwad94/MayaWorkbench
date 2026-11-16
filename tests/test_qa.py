from __future__ import annotations

import json

import pytest

from mayaworkbench.domain.enums import QaStatus
from mayaworkbench.errors import PolicyError
from mayaworkbench.models.base import ChatMessage, ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.storage.stores import EventStore, InvocationStore


def test_answer_with_verified_citation_is_accepted(indexed, virtual_model):
    result = indexed.services.ask.ask(indexed.snapshot.id, "Where is CausalAttention defined?")
    record = result.record
    assert record.status in (QaStatus.ANSWERED, QaStatus.ANSWERED_WITH_UNCERTAINTY)
    assert record.citations
    assert all(citation.valid for citation in record.citations)
    assert all(citation.validation == "verified" for citation in record.citations)
    assert record.model_invocation_id
    assert result.model_used is True

    invocation = indexed.services.invocations.get(record.model_invocation_id)
    assert invocation.status == "SUCCEEDED"
    assert invocation.total_tokens == 150
    assert invocation.provider == "fake"

    payload = json.loads(indexed.services.artifacts.read_text(record.artifact_id))
    assert payload["status"] == record.status.value
    assert payload["context"]["artifact_id"]
    assert payload["model"]["used"] is True

    events = [event.event_type.value for event in indexed.services.events.list(limit=50)]
    assert "QA_ANSWERED" in events
    assert "MODEL_INVOCATION_COMPLETED" in events


def test_unverifiable_citation_withholds_the_model_answer(indexed, virtual_model):
    virtual_model.mode = "invalid_path"
    result = indexed.services.ask.ask(indexed.snapshot.id, "Where is CausalAttention defined?")
    record = result.record
    assert record.status == QaStatus.CITATIONS_REJECTED
    assert record.citations and all(not citation.valid for citation in record.citations)
    assert "withheld" in record.answer_markdown
    payload = json.loads(indexed.services.artifacts.read_text(record.artifact_id))
    assert payload["rejected_model_answer"]
    events = [event.event_type.value for event in indexed.services.events.list(limit=50)]
    assert "QA_REJECTED" in events


def test_any_invalid_citation_withholds_the_answer(indexed, virtual_model):
    virtual_model.mode = "mixed"
    result = indexed.services.ask.ask(indexed.snapshot.id, "Where is CausalAttention defined?")
    record = result.record
    assert record.status == QaStatus.CITATIONS_REJECTED
    assert len(record.citations) == 2
    assert sum(1 for citation in record.citations if citation.valid) == 1
    assert sum(1 for citation in record.citations if not citation.valid) == 1
    assert "withheld" in record.answer_markdown
    payload = json.loads(indexed.services.artifacts.read_text(record.artifact_id))
    assert payload["rejected_model_answer"] == "One real citation and one invented one."


def test_schema_invalid_output_is_discarded(indexed, virtual_model):
    virtual_model.mode = "bad_schema"
    result = indexed.services.ask.ask(indexed.snapshot.id, "Where is CausalAttention defined?")
    assert result.record.status == QaStatus.FAILED
    assert "schema" in result.record.answer_markdown.lower()


def test_no_model_returns_retrieval_digest(indexed):
    result = indexed.services.ask.ask(
        indexed.snapshot.id, "Where is CausalAttention defined?", use_model=False
    )
    record = result.record
    assert record.status == QaStatus.NO_MODEL
    assert "No synthesized answer" in record.answer_markdown
    assert record.citations and all(citation.valid for citation in record.citations)
    assert all("not model-verified" in citation.validation for citation in record.citations)
    assert result.model_used is False
    assert record.model_invocation_id is None


def test_agent_without_model_configuration_is_honest(indexed):
    status = indexed.services.gateway.status()
    assert status.callable is False
    result = indexed.services.ask.ask(indexed.snapshot.id, "What does forward do?")
    assert result.record.status == QaStatus.NO_MODEL
    assert result.record.model_invocation_id is None


def test_insufficient_evidence_is_not_answered(indexed):
    result = indexed.services.ask.ask(
        indexed.snapshot.id, "zzzqqqxyz plutonium banana", use_model=True
    )
    assert result.record.status == QaStatus.INSUFFICIENT_EVIDENCE
    assert result.record.citations == []
    assert "No answer was produced" in result.record.answer_markdown


def test_local_only_policy_refuses_remote_calls(services, project, monkeypatch):
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "test-key")
    settings = services.settings.model_copy(
        update={
            "model": services.settings.model.model_copy(
                update={"provider": "openai_compatible", "model": "fake-1"}
            )
        }
    )
    gateway = ModelGateway(
        settings,
        InvocationStore(services.db),
        EventStore(services.db),
        provider_factory=lambda _settings: pytest.fail("provider must not be constructed"),
    )
    request = ModelRequest(
        purpose="test", messages=[ChatMessage(role="user", content="hello")]
    )
    with pytest.raises(PolicyError, match="LOCAL_ONLY"):
        gateway.structured(request, project_id=project.id, snapshot_id=None, purpose="test")
    row = services.db.fetchone("SELECT status, error_class FROM model_invocations")
    assert row["status"] == "REFUSED"
    assert row["error_class"] == "policy_refused"


def test_failed_model_call_links_its_invocation(indexed, home, monkeypatch):
    from mayaworkbench.config import AppPaths, load_settings, save_settings
    from mayaworkbench.errors import ModelError
    from mayaworkbench.models.gateway import ModelGateway

    enable = indexed.services.settings.model_copy(
        update={
            "data_policy": "BYOK_REMOTE_MODEL",
            "model": indexed.services.settings.model.model_copy(
                update={"provider": "openai_compatible", "model": "fake-1"}
            ),
        }
    )
    paths = AppPaths.resolve(str(home))
    save_settings(paths, enable)
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "test-key")

    def broken_factory(_settings):
        raise ModelError("provider construction failed", error_class="auth")

    gateway = ModelGateway(
        load_settings(paths), indexed.services.invocations, indexed.services.events, broken_factory
    )
    indexed.services.ask.gateway = gateway
    indexed.services.ask.settings = load_settings(paths)

    result = indexed.services.ask.ask(indexed.snapshot.id, "Where is CausalAttention defined?")
    record = result.record
    assert record.status == QaStatus.MODEL_UNAVAILABLE
    assert record.model_invocation_id
    invocation = indexed.services.invocations.get(record.model_invocation_id)
    assert invocation.status == "FAILED"
    assert invocation.error_class == "auth"


def test_qa_records_are_listed_per_project(indexed):
    indexed.services.ask.ask(indexed.snapshot.id, "Where is CausalAttention defined?", use_model=False)
    records = indexed.services.qa_store.list(project_id=indexed.project.id)
    assert len(records) == 1
    assert records[0].status == QaStatus.NO_MODEL
    fetched = indexed.services.qa_store.get(records[0].id)
    assert fetched.question == records[0].question


def test_answer_uses_pinned_snapshot_only(indexed):
    result = indexed.services.ask.ask(
        indexed.snapshot.id, "Where is the token counting helper?", use_model=False
    )
    assert result.record.snapshot_id == indexed.snapshot.id
    assert any("utils.py" in citation.path for citation in result.record.citations)

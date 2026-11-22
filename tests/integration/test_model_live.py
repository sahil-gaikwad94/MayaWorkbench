"""REAL provider harness (opt-in, ``@pytest.mark.live_model``).

Collected by the ordinary suite but never runs at collection time. All gates
are checked at test time:

* ``MAYA_LIVE_MODEL=1`` — deliberate opt-in;
* ``MAYA_MODEL_API_KEY`` — a key supplied by the operator (never logged or
  written by this harness);
* ``MAYA_LIVE_SPEND_CAP_USD`` — a parseable, finite number > 0.

If any gate is absent the tests ``pytest.skip("reason: ...")`` with the concrete
blocker. A skip is never recorded as a pass. There is a separate module-level
guard that FAILS (not skips) when the live flag is on but the spend cap is
missing or invalid: unbounded live spend is never permitted.

No live provider call has occurred in the environment where this harness was
added. At most two tiny calls are made, each with ``max_output_tokens <= 256``,
through the configured adapter behind :class:`ModelGateway`. Settings are read
from disk and only copied in memory (no config mutation, no ``save_settings``).
"""

from __future__ import annotations

import json
import math
import os
from types import SimpleNamespace

import pytest

from mayaworkbench.models.base import ChatMessage, ModelRequest, ToolDefinition

pytestmark = pytest.mark.live_model

_TRUE = {"1", "true", "yes", "on"}
_MAX_OUTPUT_TOKENS = 128  # bounded well below the 256 ceiling

_TINY_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _spend_cap() -> tuple[float | None, str | None]:
    """Parse the hard spend cap; never invent or default it."""
    raw = os.environ.get("MAYA_LIVE_SPEND_CAP_USD")
    if raw is None or not raw.strip():
        return None, "MAYA_LIVE_SPEND_CAP_USD is not set (no unbounded spend is permitted)"
    try:
        cap = float(raw)
    except ValueError:
        return None, f"MAYA_LIVE_SPEND_CAP_USD={raw!r} is not a parseable number"
    if not math.isfinite(cap) or cap <= 0:
        return None, f"MAYA_LIVE_SPEND_CAP_USD={raw!r} must be a finite number > 0"
    return cap, None


def _cap_msg(cap: float | None) -> str:
    """Every assertion names the hard spend cap it is bounded by."""
    value = f"${cap:.2f}" if cap is not None else "<missing>"
    return (
        f"hard spend cap MAYA_LIVE_SPEND_CAP_USD={value}; "
        "at most two tiny calls, max_output_tokens <= 256; "
        "the API key is never logged or written"
    )


def test_live_model_requires_spend_cap_when_enabled():
    """Fail CLOSED when the live flag is on but no hard spend cap exists.

    This is a failure, not a skip: a caller must not be able to turn on live
    calls and then treat a skip as evidence that the gate was safe.
    """
    if not _flag_enabled("MAYA_LIVE_MODEL"):
        pytest.skip(
            "reason: MAYA_LIVE_MODEL is not set to 1; the spend-cap guard is dormant"
        )
    cap, error = _spend_cap()
    if error:
        pytest.fail(f"reason: live model calls are enabled but {error}. {_cap_msg(None)}")
    assert cap is not None and cap > 0, _cap_msg(cap)


@pytest.fixture(scope="module")
def live_model(tmp_path_factory):
    """Build a gateway from read-only settings and throwaway invocation stores."""
    cap, error = _spend_cap()
    if not _flag_enabled("MAYA_LIVE_MODEL"):
        raw = os.environ.get("MAYA_LIVE_MODEL")
        pytest.skip(f"reason: MAYA_LIVE_MODEL must be set to 1 (current value: {raw!r})")
    if not os.environ.get("MAYA_MODEL_API_KEY"):
        pytest.skip(
            "reason: MAYA_MODEL_API_KEY is not set; export a low-privilege key "
            f"({_cap_msg(cap)})"
        )
    if error:
        pytest.skip(f"reason: {error}. {_cap_msg(None)}")

    from mayaworkbench.config import AppPaths, load_settings
    from mayaworkbench.domain.models import Project
    from mayaworkbench.ids import new_ulid
    from mayaworkbench.models.gateway import ModelGateway
    from mayaworkbench.storage.db import Database
    from mayaworkbench.storage.stores import EventStore, InvocationStore, ProjectStore

    paths = AppPaths.resolve()
    settings = load_settings(paths)  # read-only: never save_settings() here
    # Bound the spend surface on an in-memory copy only. The adapter also sees
    # max_retries=0 so one request cannot silently multiply into several calls.
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
    db_path = tmp_path_factory.mktemp("model-live") / "invocations.db"
    db = Database(db_path)
    try:
        db.migrate()
        # model_invocations.project_id has a FOREIGN KEY; the throwaway DB needs
        # a real project row or every recorded call would fail before the model
        # is reached.
        project = Project(id="prj_" + new_ulid(), name="Live gate", slug="live-gate")
        ProjectStore(db).create(project)
        gateway = ModelGateway(settings, InvocationStore(db), EventStore(db))
        status = gateway.status()
        if not status.callable:
            pytest.skip(
                f"reason: the configured model is not callable: {status.reason} "
                f"({_cap_msg(cap)})"
            )
        # The provider is constructed by the gateway's own configured factory;
        # capabilities() is a local declaration and performs no network call.
        provider = gateway._factory(settings)
        yield SimpleNamespace(
            gateway=gateway,
            settings=settings,
            cap=cap,
            project_id=project.id,
            provider_caps=provider.capabilities(),
        )
    finally:
        db.close()


def _call(live_model, request, purpose):  # type: ignore[no-untyped-def]
    return live_model.gateway.structured(
        request,
        project_id=live_model.project_id,
        snapshot_id=None,
        purpose=purpose,
    )


def test_live_structured_output_is_schema_valid(live_model):
    """Call 1/2: a tiny structured response must satisfy its JSON schema."""
    cap = live_model.cap
    request = ModelRequest(
        purpose="live-gate-structured",
        messages=[
            ChatMessage(
                role="user",
                content=(
                    "Reply with a JSON object containing exactly one key 'answer' "
                    "whose value is the single word 'pong'. No other keys."
                ),
            )
        ],
        json_schema=_TINY_SCHEMA,
        schema_name="LiveEcho",
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )
    response, _invocation = _call(live_model, request, "live-gate-structured")

    payload = response.structured
    if payload is None:
        try:
            payload = json.loads(response.text)
        except (TypeError, ValueError):
            payload = None
    assert isinstance(payload, dict), _cap_msg(cap) + "; response was not a JSON object"
    assert "answer" in payload and isinstance(payload["answer"], str), _cap_msg(cap)
    assert set(payload) == {"answer"}, _cap_msg(cap) + "; schema forbids extra keys"


def test_live_tool_calls_are_declared_and_parseable(live_model):
    """Call 2/2: any returned tool call must name a declared tool with valid args."""
    cap = live_model.cap
    if not live_model.provider_caps.tool_calling:
        pytest.skip(
            f"reason: the configured adapter declares tool_calling=False; "
            f"only the tool test is skipped ({_cap_msg(cap)})"
        )

    echo = ToolDefinition(
        name="echo",
        description="Return the text passed in.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    request = ModelRequest(
        purpose="live-gate-tools",
        messages=[
            ChatMessage(
                role="user",
                content="Use the echo tool with text 'ping'. Do not invent other tools.",
            )
        ],
        tools=[echo],
        tool_choice="auto",
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )
    response, _invocation = _call(live_model, request, "live-gate-tools")

    # A model may decline to call the tool; that is not a failure. Any call it
    # does return must be well-formed and use a name from the declared tools.
    for call in response.tool_calls:
        assert call.name == echo.name, _cap_msg(cap) + f"; undeclared tool {call.name!r}"
        assert isinstance(call.arguments, dict), _cap_msg(cap)
        json.dumps(call.arguments)  # arguments must be JSON-serialisable/parseable
        assert set(call.arguments) <= {"text"}, _cap_msg(cap)
        assert isinstance(call.arguments.get("text"), str), _cap_msg(cap)
    assert response.provider, _cap_msg(cap)

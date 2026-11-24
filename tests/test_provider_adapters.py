"""Provider hardening tests: real wire shapes, fake transports, no network.

Every test here uses an in-memory urllib replacement or an injected provider.
No socket is opened and no real key is used. The assertions are about the
request the adapter would send and the value it returns for a documented
provider response.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from mayaworkbench.config import Settings
from mayaworkbench.errors import ModelError, PolicyError
from mayaworkbench.models.anthropic_adapter import AnthropicProvider
from mayaworkbench.models.base import ChatMessage, ModelRequest, ToolCall, ToolDefinition
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.models.gemini_adapter import GeminiProvider
from mayaworkbench.models.openai_compatible import OpenAICompatibleProvider
from mayaworkbench.models.schema import (
    SchemaProjectionError,
    gemini_schema,
    openai_strict_schema,
    resolve_refs,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(
        self,
        *,
        body: bytes = b"",
        lines: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self._lines = lines
        self._position = 0
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if self._lines is not None:
            return b"".join(self._lines)
        return self._body if size < 0 else self._body[:size]

    def readline(self, size: int = -1) -> bytes:
        if self._lines is None or self._position >= len(self._lines):
            return b""
        line = self._lines[self._position]
        self._position += 1
        return line if size < 0 else line[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


def _sse(*payloads: Any, event: str | None = None) -> list[bytes]:
    lines: list[bytes] = []
    for payload in payloads:
        if event:
            lines.append(f"event: {event}\n".encode())
        lines.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return lines


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _http_error(code: int, payload: Any, headers: dict[str, str] | None = None):
    return urllib.error.HTTPError(
        "https://example.invalid",
        code,
        "error",
        headers or {},
        io.BytesIO(_json(payload)),
    )


def _sequence(monkeypatch, items: list[Any]) -> list[Any]:
    """Replace urlopen with a sequence of responses/exceptions."""
    calls: list[Any] = []
    queue = list(items)

    def fake(request: Any, timeout: int | None = None) -> Any:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def _sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: recorded.append(seconds))
    return recorded


def _request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(
        purpose="test",
        messages=[ChatMessage(role="user", content="hello")],
        **kwargs,
    )


def _openai() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://127.0.0.1:9/v1",
        model="test-model",
        api_key="fake-key",
        max_retries=2,
    )


# --------------------------------------------------------------------------- #
# Schema projections                                                           #
# --------------------------------------------------------------------------- #
def test_schema_projection_inlines_refs_and_closes_objects() -> None:
    schema = {
        "type": "object",
        "properties": {"steps": {"type": "array", "items": {"$ref": "#/$defs/Step"}}},
        "required": ["steps"],
        "additionalProperties": False,
        "$defs": {
            "Step": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "note": {"type": ["string", "null"]}},
            }
        },
    }
    projected = openai_strict_schema(schema)
    assert "$defs" not in projected
    step = projected["properties"]["steps"]["items"]
    assert step["type"] == "object"
    assert step["required"] == ["note", "title"]
    assert step["additionalProperties"] is False
    assert resolve_refs(schema)["properties"]["steps"]["items"]["properties"]["title"] == {
        "type": "string"
    }

    gemini = gemini_schema(schema)
    step = gemini["properties"]["steps"]["items"]
    assert step["type"] == "OBJECT"
    assert "additionalProperties" not in step
    assert step["properties"]["note"]["type"] == "STRING"
    assert step["properties"]["note"]["nullable"] is True
    assert "required" not in step  # optional properties stay optional


def test_schema_projection_refuses_external_refs() -> None:
    with pytest.raises(SchemaProjectionError):
        resolve_refs({"$ref": "https://example.invalid/schema.json"})


# --------------------------------------------------------------------------- #
# OpenAI-compatible                                                            #
# --------------------------------------------------------------------------- #
def test_openai_stream_requests_usage_and_parses_token_details(monkeypatch) -> None:
    lines = _sse(
        {"id": "c1", "model": "test-model", "choices": [{"delta": {"content": "Hi"}}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 6},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        },
    )
    calls = _sequence(monkeypatch, [FakeResponse(lines=lines)])
    response = _openai().generate(_request(stream=True))
    assert response.text == "Hi"
    assert response.usage.cached_prompt_tokens == 6
    assert response.usage.reasoning_tokens == 2
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}


def test_openai_adapts_token_parameter_and_temperature(monkeypatch) -> None:
    rejection = _http_error(
        400,
        {
            "error": {
                "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                "Use 'max_completion_tokens' instead.",
                "type": "invalid_request_error",
                "param": "max_tokens",
                "code": "unsupported_parameter",
            }
        },
    )
    temperature = _http_error(
        400,
        {
            "error": {
                "message": "Unsupported value: 'temperature' does not support 0.0 with this model.",
                "type": "invalid_request_error",
                "param": "temperature",
                "code": "unsupported_value",
            }
        },
    )
    success = FakeResponse(
        body=_json(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
    )
    calls = _sequence(monkeypatch, [rejection, temperature, success])
    response = _openai().generate(_request())
    assert response.text == "ok"
    first = json.loads(calls[0].data.decode("utf-8"))
    second = json.loads(calls[1].data.decode("utf-8"))
    third = json.loads(calls[2].data.decode("utf-8"))
    assert "max_tokens" in first and "temperature" in first
    assert "max_completion_tokens" in second and "max_tokens" not in second
    assert "temperature" in second
    assert "max_completion_tokens" in third and "temperature" not in third
    assert response.retry_count == 2


def test_openai_structured_modes_degrade_one_step_at_a_time(monkeypatch) -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    reject_schema = _http_error(
        400,
        {
            "error": {
                "message": "response_format json_schema is not supported",
                "type": "invalid_request_error",
                "param": "response_format",
                "code": "invalid_request_error",
            }
        },
    )
    reject_json = _http_error(
        400,
        {
            "error": {
                "message": "json_object response format is not supported",
                "type": "invalid_request_error",
                "param": "response_format",
                "code": "invalid_request_error",
            }
        },
    )
    success = FakeResponse(
        body=_json({"choices": [{"message": {"content": '{"ok": true}'}}]})
    )
    calls = _sequence(monkeypatch, [reject_schema, reject_json, success])
    response = _openai().generate(_request(json_schema=schema))
    assert response.structured == {"ok": True}
    assert response.used_structured_output is False
    first = json.loads(calls[0].data.decode("utf-8"))
    second = json.loads(calls[1].data.decode("utf-8"))
    third = json.loads(calls[2].data.decode("utf-8"))
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert "response_format" not in third
    # Prompt-only mode must still tell the model the schema.
    assert "JSON Schema" in json.dumps(third["messages"])


def test_openai_context_length_and_quota_are_not_retried(monkeypatch) -> None:
    slept = _sleeps(monkeypatch)
    _sequence(
        monkeypatch,
        [
            _http_error(
                400,
                {
                    "error": {
                        "message": "This model's maximum context length is 8192 tokens",
                        "code": "context_length_exceeded",
                    }
                },
            )
        ],
    )
    with pytest.raises(ModelError) as raised:
        _openai().generate(_request())
    assert raised.value.error_class == "context_length"
    assert raised.value.retryable is False
    assert raised.value.attempts == 1
    assert slept == []

    _sequence(
        monkeypatch,
        [
            _http_error(
                429,
                {"error": {"message": "quota exhausted", "code": "insufficient_quota"}},
            )
        ],
    )
    with pytest.raises(ModelError) as quota:
        _openai().generate(_request())
    assert quota.value.error_class == "quota"
    assert quota.value.retryable is False


def test_openai_honours_bounded_retry_after(monkeypatch) -> None:
    slept = _sleeps(monkeypatch)
    _sequence(
        monkeypatch,
        [
            _http_error(
                429,
                {"error": {"message": "slow down", "code": "rate_limit_exceeded"}},
                headers={"Retry-After": "600"},
            ),
            FakeResponse(
                body=_json({"choices": [{"message": {"content": "ok"}}]})
            ),
        ],
    )
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        api_key="k",
        max_retries=1,
    )
    response = provider.generate(_request())
    assert response.retry_count == 1
    assert slept == [30.0]  # hard ceiling, never 600 seconds


def test_openai_incremental_stream_yields_deltas_then_terminal(monkeypatch) -> None:
    lines = _sse(
        {"model": "test-model", "choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        },
    )
    _sequence(monkeypatch, [FakeResponse(lines=lines)])
    chunks = list(_openai().stream(_request()))
    assert [chunk.text for chunk in chunks[:-1]] == ["Hel", "lo"]
    assert all(chunk.is_final is False for chunk in chunks[:-1])
    final = chunks[-1]
    assert final.is_final is True
    assert final.text == ""
    assert final.usage.total_tokens == 4


def test_openai_non_stream_body_is_bounded(monkeypatch) -> None:
    import mayaworkbench.models.openai_compatible as module

    monkeypatch.setattr(module, "MAX_JSON_BYTES", 100)
    _sequence(monkeypatch, [FakeResponse(body=b"x" * 500)])
    with pytest.raises(ModelError) as raised:
        _openai().generate(_request())
    assert raised.value.error_class == "protocol"
    assert raised.value.retryable is False


# --------------------------------------------------------------------------- #
# Anthropic                                                                    #
# --------------------------------------------------------------------------- #
def test_anthropic_cache_breakpoints_and_tool_result_merging() -> None:
    provider = AnthropicProvider(model="claude-test", api_key="fake")
    request = ModelRequest(
        purpose="tools",
        messages=[
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="check"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="t1", name="read", arguments={"path": "a"}),
                    ToolCall(id="t2", name="read", arguments={"path": "b"}),
                ],
            ),
            ChatMessage(role="tool", content="A", tool_call_id="t1", tool_name="read"),
            ChatMessage(role="tool", content="B", tool_call_id="t2", tool_name="read"),
        ],
        json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        cache_breakpoints=[0, 4],
    )
    payload = provider._build_payload(request, repair_notice=None)
    assert payload["system"] == [
        {"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}
    ]
    # The two tool results are one user turn, as the API requires.
    assert [message["role"] for message in payload["messages"]][-1] == "user"
    tool_turn = payload["messages"][-1]
    assert len(tool_turn["content"]) == 2
    assert tool_turn["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "A",
    }
    assert tool_turn["content"][1]["cache_control"] == {"type": "ephemeral"}
    assert payload["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": request.json_schema,
        }
    }


def test_anthropic_refusal_is_a_typed_non_answer() -> None:
    provider = AnthropicProvider(model="claude-test", api_key="fake")
    data = {
        "content": [{"type": "text", "text": "I cannot help with that."}],
        "stop_reason": "refusal",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    with pytest.raises(ModelError) as raised:
        provider._parse_response(data, _request(json_schema={"type": "object"}))
    assert raised.value.error_class == "refusal"
    assert raised.value.retryable is False


def test_anthropic_stream_assembles_tool_input_and_cache_usage(monkeypatch) -> None:
    lines = [
        b'event: message_start\n',
        b'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-test",'
        b'"usage":{"input_tokens":5,"cache_read_input_tokens":2}}}\n\n',
        b'event: content_block_start\n',
        b'data: {"type":"content_block_start","index":0,"content_block":'
        b'{"type":"tool_use","id":"toolu_1","name":"read"}}\n\n',
        b'event: content_block_delta\n',
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n\n',
        b'event: content_block_delta\n',
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"input_json_delta","partial_json":"\\"a\\"}"}}\n\n',
        b'event: message_delta\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
        b'"usage":{"output_tokens":1}}\n\n',
        b'event: message_stop\n',
        b'data: {"type":"message_stop"}\n\n',
    ]
    _sequence(monkeypatch, [FakeResponse(lines=lines)])
    provider = AnthropicProvider(model="claude-test", api_key="fake")
    response = provider.generate(_request(stream=True))
    assert response.tool_calls == [
        ToolCall(id="toolu_1", name="read", arguments={"path": "a"})
    ]
    assert response.usage.prompt_tokens == 7  # 5 input + 2 cache read
    assert response.usage.cached_prompt_tokens == 2
    assert response.usage.completion_tokens == 1


def test_anthropic_structured_rejection_falls_back_to_prompt(monkeypatch) -> None:
    rejection = _http_error(
        400,
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "output_config schema refused"},
        },
    )
    success = FakeResponse(
        body=_json(
            {
                "id": "msg_1",
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
    )
    calls = _sequence(monkeypatch, [rejection, success])
    provider = AnthropicProvider(model="claude-test", api_key="fake")
    response = provider.generate(
        _request(json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    )
    assert response.structured == {"ok": True}
    assert response.used_structured_output is False
    assert "output_config" in json.loads(calls[0].data.decode("utf-8"))
    second = json.loads(calls[1].data.decode("utf-8"))
    assert "output_config" not in second
    assert "schema" in json.dumps(second["messages"]).lower()


# --------------------------------------------------------------------------- #
# Gemini                                                                       #
# --------------------------------------------------------------------------- #
def test_gemini_structured_schema_and_usage_details(monkeypatch) -> None:
    body = FakeResponse(
        body=_json(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": '{"ok": true}'}], "role": "model"},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                    "thoughtsTokenCount": 3,
                    "cachedContentTokenCount": 4,
                },
                "modelVersion": "gemini-test",
            }
        )
    )
    calls = _sequence(monkeypatch, [body])
    provider = GeminiProvider(model="gemini-test", api_key="fake")
    provider.input_cost_per_mtok = 1.0
    provider.output_cost_per_mtok = 2.0
    response = provider.generate(
        _request(json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    )
    assert response.structured == {"ok": True}
    assert response.usage.completion_tokens == 5
    assert response.usage.reasoning_tokens == 3
    assert response.usage.cached_prompt_tokens == 4
    assert response.usage.total_tokens == 15
    sent = json.loads(calls[0].data.decode("utf-8"))
    config = sent["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["type"] == "OBJECT"
    assert config["responseSchema"]["properties"]["ok"]["type"] == "BOOLEAN"


def test_gemini_function_response_uses_the_resolved_function_name(monkeypatch) -> None:
    _sequence(monkeypatch, [FakeResponse(body=_json({"candidates": []}))])
    provider = GeminiProvider(model="gemini-test", api_key="fake")
    payload = provider._build_payload(
        ModelRequest(
            purpose="tools",
            messages=[
                ChatMessage(role="user", content="find"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="gemini_call_0", name="lookup", arguments={"q": "x"})],
                ),
                ChatMessage(role="tool", content="result", tool_call_id="gemini_call_0"),
            ],
        ),
        repair_notice=None,
    )
    function_response = payload["contents"][-1]["parts"][0]["functionResponse"]
    assert function_response["name"] == "lookup"
    assert payload["contents"][-1]["role"] == "user"


def test_gemini_blocked_prompt_and_safety_finish_are_typed(monkeypatch) -> None:
    provider = GeminiProvider(model="gemini-test", api_key="fake")
    with pytest.raises(ModelError) as blocked:
        provider._parse_response(
            {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, _request()
        )
    assert blocked.value.error_class == "content_filter"
    with pytest.raises(ModelError) as safety:
        provider._parse_response(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "partial"}]},
                        "finishReason": "SAFETY",
                    }
                ]
            },
            _request(),
        )
    assert safety.value.error_class == "content_filter"
    assert safety.value.retryable is False


def test_gemini_incremental_stream_skips_thought_parts(monkeypatch) -> None:
    lines = _sse(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "thinking...", "thought": True},
                            {"text": "answer"},
                        ],
                        "role": "model",
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "!"}], "role": "model"},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        },
    )
    _sequence(monkeypatch, [FakeResponse(lines=lines)])
    chunks = list(GeminiProvider(model="gemini-test", api_key="fake").stream(_request()))
    assert [chunk.text for chunk in chunks[:-1]] == ["answer", "!"]
    assert chunks[-1].is_final is True
    assert chunks[-1].usage.prompt_tokens == 1


# --------------------------------------------------------------------------- #
# Gateway: local endpoints, accounting and bounded input                      #
# --------------------------------------------------------------------------- #
class _Invocations:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.refused: list[tuple[str, str]] = []
        self.failures: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []

    def create(self, invocation: Any) -> None:
        self.created.append(invocation)

    def mark_refused(self, invocation_id: str, *, reason: str) -> None:
        self.refused.append((invocation_id, reason))

    def fail(self, invocation_id: str, **kwargs: Any) -> None:
        self.failures.append({"id": invocation_id, **kwargs})

    def complete(self, invocation_id: str, **kwargs: Any) -> None:
        self.completed.append({"id": invocation_id, **kwargs})


class _Events:
    def append(self, event: Any) -> None:
        pass


class _FailingProvider:
    name = "fake"
    model = "fake-1"

    def capabilities(self):
        from mayaworkbench.models.base import ModelCapabilities

        return ModelCapabilities(structured_output=True)

    def generate(self, request: ModelRequest):
        raise ModelError(
            "provider exploded",
            error_class="server",
            retryable=True,
            attempts=3,
            http_status=503,
            provider_code="server_error",
        )


def test_gateway_records_actual_attempts_and_safe_failure_details(monkeypatch) -> None:
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "test-key")
    settings = Settings.model_validate(
        {
            "model": {"provider": "openai_compatible", "model": "m"},
            "data_policy": "BYOK_REMOTE_MODEL",
        }
    )
    invocations = _Invocations()
    gateway = ModelGateway(
        settings, invocations, _Events(), lambda _settings: _FailingProvider()
    )
    with pytest.raises(ModelError):
        gateway.structured(
            _request(), project_id="prj_x", snapshot_id=None, purpose="test"
        )
    failure = invocations.failures[0]
    assert failure["retry_count"] == 2  # three requests means two retries
    assert failure["details"]["http_status"] == 503
    assert failure["details"]["provider_code"] == "server_error"


def test_gateway_local_endpoint_without_key_is_callable(monkeypatch) -> None:
    monkeypatch.delenv("MAYA_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MAYA_LOCAL_MODEL_API_KEY", raising=False)
    settings = Settings.model_validate(
        {
            "model": {
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "local-model",
            },
            "data_policy": "LOCAL_ONLY",
        }
    )
    status = ModelGateway(settings, _Invocations(), _Events()).status()
    assert status.callable is True
    assert status.key_present is False
    provider = __import__(
        "mayaworkbench.models.gateway", fromlist=["_default_factory"]
    )._default_factory(settings)
    assert provider.name == "openai_compatible"
    assert provider._api_key == "local-unauthenticated"

    local_settings = Settings.model_validate(
        {
            "model": {
                "provider": "local_openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "local-model",
            },
            "data_policy": "LOCAL_ONLY",
        }
    )
    local_status = ModelGateway(local_settings, _Invocations(), _Events()).status()
    assert local_status.callable is True
    assert local_status.key_present is False


def test_gateway_input_bound_counts_tool_schemas(monkeypatch) -> None:
    settings = Settings.model_validate(
        {
            "model": {
                "provider": "openai_compatible",
                "model": "m",
                "max_input_tokens": 1000,
            },
            "data_policy": "LOCAL_ONLY",
        }
    )
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "test-key")

    def factory(_settings):
        raise AssertionError("the provider must not be constructed for an oversized prompt")

    gateway = ModelGateway(settings, _Invocations(), _Events(), factory)
    tool = ToolDefinition(
        name="huge",
        description="x",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "y" * 5000}},
        },
    )
    with pytest.raises(PolicyError):
        gateway.structured(
            _request(tools=[tool]),
            project_id="prj_x",
            snapshot_id=None,
            purpose="test",
        )

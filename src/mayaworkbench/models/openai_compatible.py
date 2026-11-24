"""OpenAI-compatible chat completions adapter.

Works with any endpoint that implements ``POST {base_url}/chat/completions``
with the OpenAI message/usage shape (OpenAI, Azure-compatible gateways, vLLM,
llama.cpp, Ollama, LM Studio and others). Implemented with the standard library
so a provider SDK never becomes a dependency of the domain layer.

Provider compatibility notes (verified against the 2026 API references):

* ``max_tokens`` is deprecated and rejected by reasoning models; the adapter
  starts with ``max_tokens`` for local-server compatibility and switches to
  ``max_completion_tokens`` when a provider says it is required.
* reasoning models reject non-default sampling parameters; such a rejection
  removes ``temperature`` for the next attempt instead of failing the call.
* ``response_format`` support varies (strict ``json_schema``, ``json_object``,
  prompt-only). The adapter degrades one step at a time and records which mode
  was actually used.
* streaming usage is only returned when ``stream_options.include_usage`` is
  supported; the adapter requests it and drops it on rejection.
* 400/401/403/404/429/5xx and provider error codes are mapped to typed,
  non-secret errors; ``Retry-After`` is honoured up to a hard ceiling.

Streaming is supported both buffered (``generate(stream=True)`` returns one
assembled response) and incrementally (``stream(request)`` yields text deltas
and a terminal response with usage and tool calls).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from mayaworkbench.errors import ModelError
from mayaworkbench.models.base import (
    ChatMessage,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from mayaworkbench.models.schema import SchemaProjectionError, openai_strict_schema
from mayaworkbench.models.sse import (
    bounded_read,
    iter_sse_frames,
    retry_after_from_error,
)
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.openai_compatible")

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

#: Hard cap for a non-streaming JSON body.
MAX_JSON_BYTES = 16_000_000

#: A request may be adjusted for provider quirks a bounded number of times.
_MAX_ADAPTATIONS = 6


class OpenAICompatibleProvider:
    name = "openai_compatible"
    #: SSE is consumed in ``generate(stream=True)`` and assembled before return.
    supports_buffered_streaming = True

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: int = 120,
        max_output_tokens: int = 2000,
        temperature: float = 0.0,
        max_retries: int = 2,
        input_cost_per_mtok: float | None = None,
        output_cost_per_mtok: float | None = None,
    ):
        if not model:
            raise ModelError(
                "No model name is configured",
                hint="Set MAYA_MODEL_NAME or edit the model section in config.json.",
            )
        if not api_key:
            raise ModelError(
                "No provider API key is available",
                error_class="auth",
                hint="Set the environment variable named by model.api_key_env.",
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.input_cost_per_mtok = input_cost_per_mtok
        self.output_cost_per_mtok = output_cost_per_mtok
        # Provider-quirk state, adjusted only by an observed rejection.
        self._structured_mode = "json_schema"  # json_schema -> json_object -> prompt
        self._structured_supported = True
        self._token_param = "max_tokens"
        self._temperature_supported = True
        self._parallel_tools_supported = True
        self._stream_usage_supported = True

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=self._structured_supported,
            streaming=True,
            tool_calling=True,
            parallel_tool_calls=self._parallel_tools_supported,
            provider_family="openai_compatible",
            max_context_tokens=None,
        )

    def describe(self) -> dict[str, Any]:
        """Non-secret description safe for CLI output and events."""
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "capabilities": self.capabilities().model_dump(mode="json"),
            "structured_mode": self._structured_mode,
            "token_parameter": self._token_param,
            "note": "The key value is never included in output, results or events.",
        }

    # ------------------------------------------------------------------ public
    def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        attempts = 0
        retries = 0
        repairs = 0
        adaptations = 0
        repair_notice: str | None = None
        # A bounded loop: retries and repairs are capped by max_retries, and
        # compatibility adaptations have their own small budget.
        while attempts < self.max_retries + repairs + _MAX_ADAPTATIONS + 8:
            attempts += 1
            try:
                payload = self._build_payload(request, repair_notice=repair_notice)
                if request.stream:
                    data, request_id = self._post_stream(payload)
                else:
                    data, request_id = self._post(payload)
                text, structured, tool_calls = self._parse_response(data, request)
                return ModelResponse(
                    text=text,
                    structured=structured,
                    usage=self._usage(data),
                    provider=self.name,
                    model=str(data.get("model") or self.model),
                    provider_request_id=request_id or _as_str(data.get("id")),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    retry_count=attempts - 1,
                    used_structured_output=bool(request.json_schema)
                    and self._structured_mode != "prompt",
                    tool_calls=tool_calls,
                )
            except ModelError as exc:
                exc.attempts = attempts
                if exc.error_class == "adapter_parameter" and adaptations < _MAX_ADAPTATIONS:
                    adaptations += 1
                    log.info("retrying after adapting a provider parameter")
                    continue
                if exc.error_class == "structured_output" and repairs < self.max_retries:
                    repairs += 1
                    repair_notice = (
                        "Your previous response was not a valid JSON object matching the schema. "
                        "Return only the JSON object, with no prose or code fences."
                    )
                    log.warning(
                        "retrying after invalid structured output", extra={"attempt": attempts}
                    )
                    continue
                if not exc.retryable or retries >= self.max_retries:
                    raise
                retries += 1
                sleep_for = self._retry_delay(exc, retries)
                log.warning(
                    "retrying model call",
                    extra={
                        "attempt": attempts,
                        "error_class": exc.error_class,
                        "sleep_s": sleep_for,
                    },
                )
                time.sleep(sleep_for)

        raise ModelError(
            "Model call exhausted its attempt budget",
            error_class="attempts_exhausted",
            attempts=attempts,
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelResponse]:
        """Yield incremental text deltas, then one terminal response.

        Retries happen only before the first token is emitted. Once bytes have
        been delivered to the caller a failure is raised, never silently
        restarted, so a consumer cannot receive duplicated text.
        """
        started = time.monotonic()
        attempts = 0
        retries = 0
        adaptations = 0
        response = None
        request_id: str | None = None
        while response is None:
            attempts += 1
            try:
                payload = self._build_payload(request, repair_notice=None)
                payload["stream"] = True
                response, request_id = self._open(payload, stream=True)
            except ModelError as exc:
                exc.attempts = attempts
                if exc.error_class == "adapter_parameter" and adaptations < _MAX_ADAPTATIONS:
                    adaptations += 1
                    continue
                if not exc.retryable or retries >= self.max_retries:
                    raise
                retries += 1
                time.sleep(self._retry_delay(exc, retries))
        final: tuple[dict[str, Any], str | None] | None = None
        with response:
            for kind, value in self._iter_stream_frames(response):
                if kind == "text":
                    yield ModelResponse(
                        text=value,
                        provider=self.name,
                        model=self.model,
                        provider_request_id=request_id,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        retry_count=attempts - 1,
                        is_final=False,
                    )
                else:  # "final"
                    final = value
        if final is None:
            raise ModelError(
                "The provider stream ended without a completion",
                error_class="protocol",
                retryable=False,
                attempts=attempts,
            )
        data, stream_request_id = final
        _text, structured, tool_calls = self._parse_response(data, request)
        yield ModelResponse(
            text="",
            structured=structured,
            usage=self._usage(data),
            provider=self.name,
            model=str(data.get("model") or self.model),
            provider_request_id=stream_request_id or request_id or _as_str(data.get("id")),
            latency_ms=int((time.monotonic() - started) * 1000),
            retry_count=attempts - 1,
            used_structured_output=bool(request.json_schema) and self._structured_mode != "prompt",
            tool_calls=tool_calls,
            is_final=True,
        )

    # ------------------------------------------------------------------ payload
    def _build_payload(self, request: ModelRequest, *, repair_notice: str | None) -> dict[str, Any]:
        messages = [_message_payload(message) for message in request.messages]
        if repair_notice:
            messages.append({"role": "user", "content": repair_notice})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            self._token_param: request.max_output_tokens or self.max_output_tokens,
        }
        if self._temperature_supported:
            payload["temperature"] = (
                request.temperature if request.temperature is not None else self.temperature
            )
        if request.json_schema:
            if self._structured_mode != "prompt":
                payload["response_format"] = self._response_format(request)
                if self._structured_mode != "json_schema":
                    payload["messages"] = _ensure_json_instruction(messages, request)
            else:
                payload["messages"] = _ensure_json_instruction(messages, request, force=True)
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = _tool_choice(request.tool_choice, request.tools)
            if self._parallel_tools_supported:
                payload["parallel_tool_calls"] = True
        if request.stream:
            payload["stream"] = True
            if self._stream_usage_supported:
                payload["stream_options"] = {"include_usage": True}
        return payload

    def _response_format(self, request: ModelRequest) -> dict[str, Any]:
        assert request.json_schema is not None
        if self._structured_mode == "json_schema":
            try:
                schema = openai_strict_schema(request.json_schema)
            except SchemaProjectionError as exc:
                # The schema cannot be represented in strict mode; JSON mode
                # with an explicit instruction is still better than prose.
                log.info("falling back to json_object mode", extra={"reason": str(exc)})
                self._structured_mode = "json_object"
            else:
                return {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                }
        if self._structured_mode == "json_object":
            return {"type": "json_object"}
        return {"type": "json_object"}

    # ---------------------------------------------------------------- transport
    def _open(self, payload: dict[str, Any], *, stream: bool = False):  # type: ignore[no-untyped-def]
        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "text/event-stream" if stream else "application/json",
            },
        )
        try:
            response = urllib.request.urlopen(http_request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            detail, provider_code, param, kind = _error_parts(exc)
            retry_after = retry_after_from_error(exc)
            if self._adapt_to_rejection(exc.code, detail, provider_code, param, kind):
                raise ModelError(
                    "Provider rejected a parameter; the adapter adjusted and will retry",
                    error_class="adapter_parameter",
                    retryable=True,
                    http_status=exc.code,
                    provider_code=provider_code,
                    retry_after_seconds=retry_after,
                ) from exc
            error_class, retryable = _classify_provider_error(exc.code, provider_code, kind)
            raise ModelError(
                f"Provider returned HTTP {exc.code}: {detail[:500]}",
                error_class=error_class,
                retryable=retryable,
                http_status=exc.code,
                provider_code=provider_code,
                retry_after_seconds=retry_after,
            ) from exc
        except urllib.error.URLError as exc:
            raise ModelError(
                f"Cannot reach the model provider: {exc.reason}",
                error_class="network",
                retryable=True,
                hint="Check the base URL and network access.",
            ) from exc
        except TimeoutError as exc:
            raise ModelError(
                f"Model provider timed out after {self.timeout_seconds}s",
                error_class="timeout",
                retryable=True,
            ) from exc
        return response, response.headers.get("x-request-id")

    def _adapt_to_rejection(
        self,
        status: int,
        detail: str,
        provider_code: str | None,
        param: str | None,
        kind: str | None,
    ) -> bool:
        """Adjust one provider-specific parameter after an observed rejection.

        Returns ``True`` only when an internal setting actually changed, so a
        genuinely invalid request still fails instead of looping.
        """
        if status not in (400, 404, 422):
            return False
        text = " ".join(
            part.lower()
            for part in (detail, provider_code or "", param or "", kind or "")
        )
        if (
            param in {"max_tokens", "max_completion_tokens"}
            or ("max_tokens" in text and "max_completion_tokens" in text)
        ):
            if self._token_param == "max_tokens":
                self._token_param = "max_completion_tokens"
                return True
            if self._token_param == "max_completion_tokens":
                self._token_param = "max_tokens"
                return True
        if param in {"temperature", "top_p", "top_k"} or (
            "temperature" in text
            and ("unsupported" in text or "not supported" in text or "only the default" in text)
        ):
            if self._temperature_supported:
                self._temperature_supported = False
                return True
        if param == "parallel_tool_calls" or "parallel_tool_calls" in text:
            if self._parallel_tools_supported:
                self._parallel_tools_supported = False
                return True
        if "stream_options" in text and self._stream_usage_supported:
            self._stream_usage_supported = False
            return True
        if (
            "response_format" in text
            or "json_schema" in text
            or "json_object" in text
            or "json mode" in text
            or "structured output" in text
        ) and self._structured_mode != "prompt":
            if self._structured_mode == "json_schema":
                self._structured_mode = "json_object"
            else:
                self._structured_mode = "prompt"
                self._structured_supported = False
            return True
        return False

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        response, request_id = self._open(payload)
        with response:
            raw = bounded_read(response, MAX_JSON_BYTES)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelError(
                "Provider returned a non-JSON response",
                error_class="protocol",
                retryable=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise ModelError(
                "Provider returned JSON that was not an object",
                error_class="protocol",
                retryable=False,
            )
        return parsed, request_id

    def _post_stream(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        response, request_id = self._open(payload, stream=True)
        final: tuple[dict[str, Any], str | None] | None = None
        with response:
            for kind, value in self._iter_stream_frames(response):
                if kind == "final":
                    final = value
        if final is None:
            raise ModelError(
                "The provider stream ended without a completion",
                error_class="protocol",
                retryable=False,
            )
        data, stream_request_id = final
        return data, stream_request_id or request_id

    # -------------------------------------------------------------- SSE parsing
    def _iter_stream_frames(
        self, response: Any
    ) -> Iterator[tuple[str, Any]]:
        """Yield ``("text", delta)`` frames and one ``("final", (data, id))``.

        The assembled object matches a non-streaming chat completion so the
        same parser handles both transports.
        """
        state: dict[str, Any] = {
            "text": [],
            "calls": {},
            "call_ids": {},
            "usage": {},
            "model": None,
            "id": None,
        }

        def consume(chunk: dict[str, Any]) -> str | None:
            if chunk.get("model"):
                state["model"] = chunk["model"]
            if chunk.get("id"):
                state["id"] = chunk["id"]
            usage = chunk.get("usage")
            if isinstance(usage, dict) and any(
                key in usage for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            ):
                state["usage"] = usage
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return None
            choice = choices[0]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            piece: str | None = None
            content = delta.get("content")
            if isinstance(content, str) and content:
                piece = content
            elif isinstance(content, list):
                piece = "".join(
                    part["text"]
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ) or None
            if piece:
                state["text"].append(piece)
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for position, raw in enumerate(raw_calls):
                    if not isinstance(raw, dict):
                        continue
                    call_id = str(raw["id"]) if raw.get("id") else None
                    if raw.get("index") is not None:
                        key = str(raw["index"])
                        if call_id:
                            state["call_ids"][call_id] = key
                    elif call_id and call_id in state["call_ids"]:
                        key = state["call_ids"][call_id]
                    else:
                        key = call_id or str(position)
                        if call_id:
                            state["call_ids"][call_id] = key
                    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
                    entry = state["calls"].get(key)
                    if entry is None:
                        entry = {
                            "id": str(raw.get("id") or f"call_{key}"),
                            "name": "",
                            "arguments": "",
                        }
                        state["calls"][key] = entry
                    if raw.get("id"):
                        entry["id"] = str(raw["id"])
                    if isinstance(function.get("name"), str):
                        entry["name"] += function["name"]
                    if isinstance(function.get("arguments"), str):
                        entry["arguments"] += function["arguments"]
            return piece

        for frame in iter_sse_frames(response):
            data = frame.data.strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue  # tolerate malformed keepalive frames
            if not isinstance(chunk, dict):
                continue
            piece = consume(chunk)
            if piece:
                yield "text", piece

        assembled_calls = [
            {
                "id": entry["id"],
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "arguments": entry["arguments"] or "{}",
                },
            }
            for entry in state["calls"].values()
            if entry["name"]
        ]
        yield (
            "final",
            (
                {
                    "id": state["id"],
                    "model": state["model"],
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "".join(state["text"]),
                                "tool_calls": assembled_calls,
                            }
                        }
                    ],
                    "usage": state["usage"],
                },
                None,
            ),
        )

    def _consume_sse(self, response: Any) -> dict[str, Any]:
        """Read a bounded SSE body and assemble it into one completion object."""
        final: tuple[dict[str, Any], str | None] | None = None
        for kind, value in self._iter_stream_frames(response):
            if kind == "final":
                final = value
        if final is None:
            raise ModelError(
                "The provider stream ended without a completion",
                error_class="protocol",
                retryable=False,
            )
        return final[0]

    # ------------------------------------------------------------------ parsing
    def _parse_response(
        self, data: dict[str, Any], request: ModelRequest
    ) -> tuple[str, dict[str, Any] | None, list[ToolCall]]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError(
                "Provider response contained no choices",
                error_class="protocol",
                retryable=False,
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ModelError(
                "Provider response contained no message",
                error_class="protocol",
                retryable=False,
            )
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if content is None:
            content = ""  # tool-only completions legitimately omit content
        if not isinstance(content, str):
            raise ModelError(
                "Provider response content was not text",
                error_class="protocol",
                retryable=False,
            )
        tool_calls = _parse_tool_calls(message)
        if request.json_schema is None or (not content.strip() and tool_calls):
            return content, None, tool_calls
        try:
            structured = _extract_json_object(content)
        except ValueError as exc:
            raise ModelError(
                "Model returned output that was not valid JSON",
                error_class="structured_output",
                retryable=True,
            ) from exc
        return content, structured, tool_calls

    def _usage(self, data: dict[str, Any]) -> ModelUsage:
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = _as_int(usage.get("prompt_tokens"))
        completion_tokens = _as_int(usage.get("completion_tokens"))
        total_tokens = _as_int(usage.get("total_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        cached = (
            _as_int(prompt_details.get("cached_tokens"))
            if isinstance(prompt_details, dict)
            else None
        )
        completion_details = usage.get("completion_tokens_details")
        reasoning = (
            _as_int(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict)
            else None
        )
        cost: float | None = None
        basis: str | None = None
        if (
            prompt_tokens is not None
            and completion_tokens is not None
            and self.input_cost_per_mtok is not None
            and self.output_cost_per_mtok is not None
        ):
            cost = (
                prompt_tokens / 1_000_000 * self.input_cost_per_mtok
                + completion_tokens / 1_000_000 * self.output_cost_per_mtok
            )
            basis = "user-configured prices (USD per 1M tokens)"
            if cached:
                basis += "; cached prompt tokens priced at the configured input rate"
        return ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            cost_basis=basis,
            cached_prompt_tokens=cached,
            reasoning_tokens=reasoning,
        )

    @staticmethod
    def _retry_delay(exc: ModelError, retries: int) -> float:
        if exc.retry_after_seconds:
            return float(exc.retry_after_seconds)
        return float(min(8.0, 0.5 * (2 ** (retries - 1))))


# ---------------------------------------------------------------- helpers
def _message_payload(message: ChatMessage) -> dict[str, Any]:
    item: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "tool" and message.tool_call_id:
        item["tool_call_id"] = message.tool_call_id
        if message.tool_name:
            item["name"] = message.tool_name
    if message.tool_calls:
        item["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return item


def _ensure_json_instruction(
    messages: list[dict[str, Any]], request: ModelRequest, *, force: bool = False
) -> list[dict[str, Any]]:
    """JSON mode requires the word "JSON" somewhere in the messages.

    Prompt-only mode (``force=True``) always appends the schema, even when the
    conversation already mentions JSON, because there is no native constraint
    left to rely on.
    """
    joined = " ".join(
        str(item.get("content") or "") for item in messages if isinstance(item, dict)
    ).lower()
    if "json" in joined and not force:
        return messages
    instruction = "Respond with a single JSON object and no prose."
    if request.json_schema is not None:
        instruction += " It must match this JSON Schema: " + json.dumps(
            request.json_schema, ensure_ascii=False, sort_keys=True
        )
    return [*messages, {"role": "user", "content": instruction}]


def _tool_choice(choice: str, tools: list[Any]) -> Any:
    if choice in ("auto", "none", "required"):
        return choice
    if any(tool.name == choice for tool in tools):
        return {"type": "function", "function": {"name": choice}}
    return "auto"


def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        name = function.get("name") or raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = _parse_tool_arguments(
            function.get("arguments", raw.get("arguments", "{}"))
        )
        call_id = raw.get("id")
        calls.append(
            ToolCall(
                id=str(call_id) if call_id is not None else f"call_{index}",
                name=name,
                arguments=arguments,
            )
        )
    return calls


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        return {"_raw": str(raw)}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed = _extract_json_object(raw)
        except ValueError:
            return {"_raw": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": raw}


def _stream_transport_error(exc: BaseException, timeout_seconds: int) -> ModelError:
    if isinstance(exc, TimeoutError):
        return ModelError(
            f"Model provider timed out after {timeout_seconds}s",
            error_class="timeout",
            retryable=True,
        )
    reason = getattr(exc, "reason", exc)
    return ModelError(
        f"Cannot reach the model provider: {reason}",
        error_class="network",
        retryable=True,
        hint="Check the base URL and network access.",
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found") from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValueError("structured output must be a JSON object")
    return parsed


def _classify_status(status: int) -> tuple[str, bool]:
    """Classify by HTTP status alone (kept for callers and tests)."""
    if status in (401, 403):
        return "auth", False
    if status == 404:
        return "not_found", False
    if status == 400:
        return "bad_request", False
    if status in RETRYABLE_STATUS:
        return "rate_limit" if status == 429 else "server", True
    return "http", False


def _classify_provider_error(
    status: int, provider_code: str | None, kind: str | None
) -> tuple[str, bool]:
    """Classify using the provider's error code, then fall back to status."""
    code = (provider_code or "").lower()
    kind_lower = (kind or "").lower()
    if status in (401, 403) or kind_lower in {"authentication_error", "permission_error"}:
        return "auth", False
    if status == 404:
        return "not_found", False
    if code in {
        "context_length_exceeded",
        "string_above_max_length",
        "request_too_large",
        "too_many_tokens",
    }:
        return "context_length", False
    if code in {"insufficient_quota", "billing_hard_limit_reached", "quota_exceeded"}:
        return "quota", False
    if code in {"content_filter", "content_policy_violation", "responsible_ai_block"}:
        return "content_filter", False
    if code in {"invalid_prompt", "invalid_request_error"} and status == 400:
        return "bad_request", False
    if status == 408:
        return "timeout", True
    if status == 400:
        return "bad_request", False
    if status in RETRYABLE_STATUS:
        return "rate_limit" if status == 429 else "server", True
    return "http", False


def _safe_error_detail(exc: urllib.error.HTTPError) -> str:
    return _error_parts(exc)[0]


def _error_parts(
    exc: urllib.error.HTTPError,
) -> tuple[str, str | None, str | None, str | None]:
    """Return ``(message, code, param, type)`` with no headers or key material."""
    try:
        raw = exc.read(4000)
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text[:500], None, None, None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                return (
                    str(message)[:500] if isinstance(message, str) else text[:500],
                    _optional_str(error.get("code")),
                    _optional_str(error.get("param")),
                    _optional_str(error.get("type")),
                )
            if isinstance(error, str):
                return error[:500], None, None, None
        return text[:500], None, None, None
    except Exception:  # pragma: no cover - best effort only
        return str(exc.reason or "unknown error")[:500], None, None, None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:80] or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return str(value) if value else None


__all__ = ["OpenAICompatibleProvider"]

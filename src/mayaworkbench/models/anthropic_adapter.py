"""Native Anthropic Messages adapter (stdlib only).

Uses ``POST /v1/messages`` with the ``anthropic-version: 2023-06-01`` header.
The API key is injected (or read from ``MAYA_ANTHROPIC_API_KEY``), used only in
the outgoing request and never emitted by logs, results or descriptions.

Contract details implemented from the current Messages API:

* JSON output uses the GA ``output_config.format`` field with
  ``{"type": "json_schema", "schema": ...}``. The deprecated ``output_format``
  parameter and its beta header are deliberately not used.
* Prompt-cache breakpoints requested by the caller are applied to the exact
  message content block, and to the system prompt when a system message is
  requested. The API allows at most four explicit breakpoints.
* Consecutive ``tool`` messages are merged into one user message containing
  several ``tool_result`` blocks; the API requires tool results to immediately
  follow the assistant's ``tool_use`` turn in a single user message.
* ``stop_reason == "refusal"`` is surfaced as a typed, non-retryable refusal.
  It is HTTP 200 and can be schema-invalid, so it must never be treated as an
  answer.
* Streaming uses the documented SSE event sequence and is available both
  buffered (``generate(stream=True)``) and incrementally (``stream``).
"""

from __future__ import annotations

import json
import os
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
from mayaworkbench.models.openai_compatible import (
    _as_int,
    _as_str,
    _classify_provider_error,
    _error_parts,
    _extract_json_object,
    _parse_tool_arguments,
)
from mayaworkbench.models.sse import (
    bounded_read,
    iter_sse_frames,
    retry_after_from_error,
)
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.anthropic_adapter")

_ANTHROPIC_VERSION = "2023-06-01"
_KNOWN_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}
_MAX_CACHE_BREAKPOINTS = 4
_MAX_JSON_BYTES = 16_000_000
_MAX_ADAPTATIONS = 4


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.anthropic.com",
        api_key: str | None = None,
        api_key_env: str = "MAYA_ANTHROPIC_API_KEY",
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
        key = api_key if api_key is not None else os.environ.get(api_key_env)
        if not key:
            raise ModelError(
                "No Anthropic API key is available",
                error_class="auth",
                hint=f"Set ${api_key_env} in the environment running maya.",
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = key
        self._api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.input_cost_per_mtok = input_cost_per_mtok
        self.output_cost_per_mtok = output_cost_per_mtok
        #: ``output_config`` -> ``prompt`` after an observed schema rejection.
        self._structured_mode = "output_config"

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=self._structured_mode != "prompt",
            streaming=True,
            tool_calling=True,
            parallel_tool_calls=False,
            prompt_caching=True,
            provider_family="anthropic",
            max_context_tokens=None,
        )

    def describe(self) -> dict[str, Any]:
        """Non-secret description safe for CLI output and events."""
        return {
            "name": self.name,
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self._api_key_env,
            "key_present": True,
            "capabilities": self.capabilities().model_dump(mode="json"),
            "structured_mode": self._structured_mode,
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
                    continue
                if exc.error_class == "structured_output" and repairs < self.max_retries:
                    repairs += 1
                    repair_notice = (
                        "Your previous response was not a valid JSON object matching the schema. "
                        "Return only the JSON object, with no prose or code fences."
                    )
                    continue
                if not exc.retryable or retries >= self.max_retries:
                    raise
                retries += 1
                time.sleep(self._retry_delay(exc, retries))
        raise ModelError(
            "Model call exhausted its attempt budget",
            error_class="attempts_exhausted",
            attempts=attempts,
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelResponse]:
        """Yield incremental text deltas, then one terminal response."""
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
                else:
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
        breakpoints = _breakpoints(request.cache_breakpoints)
        system_parts: list[str] = []
        cache_system = False
        rendered: list[tuple[int, ChatMessage]] = []
        for index, message in enumerate(request.messages):
            if message.role == "system":
                system_parts.append(message.content)
                cache_system = cache_system or index in breakpoints
            else:
                rendered.append((index, message))

        messages: list[dict[str, Any]] = []
        for index, message in rendered:
            if message.role == "tool":
                block = _tool_result_block(message)
                if messages and messages[-1].get("_tool_results"):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block], "_tool_results": True})
                if index in breakpoints:
                    block["cache_control"] = {"type": "ephemeral"}
                continue
            payload_message = _message_payload(message)
            if index in breakpoints:
                payload_message = _with_cache_control(payload_message)
            messages.append(payload_message)
        for message in messages:
            message.pop("_tool_results", None)

        if repair_notice:
            _append_user_text(messages, repair_notice)
        elif request.json_schema and self._structured_mode == "prompt":
            _append_user_text(
                messages,
                "Return only a JSON object matching this schema, with no prose: "
                + json.dumps(request.json_schema, ensure_ascii=False, sort_keys=True),
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        elif self.temperature:
            payload["temperature"] = self.temperature
        if system_parts:
            system_text = "\n\n".join(system_parts)
            if cache_system:
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system_text
        if request.json_schema and self._structured_mode == "output_config":
            payload["output_config"] = {
                "format": {"type": "json_schema", "schema": request.json_schema}
            }
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            choice = request.tool_choice
            if choice in ("any", "required"):
                payload["tool_choice"] = {"type": "any"}
            elif choice == "auto":
                payload["tool_choice"] = {"type": "auto"}
            elif choice == "none":
                payload["tool_choice"] = {"type": "none"}
            elif any(tool.name == choice for tool in request.tools):
                payload["tool_choice"] = {"type": "tool", "name": choice}
        if request.stream:
            payload["stream"] = True
        return payload

    # ---------------------------------------------------------------- transport
    def _open(self, payload: dict[str, Any], *, stream: bool = False):  # type: ignore[no-untyped-def]
        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "X-Api-Key": self._api_key,
                "Anthropic-Version": _ANTHROPIC_VERSION,
            },
        )
        try:
            response = urllib.request.urlopen(http_request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            detail, provider_code, param, kind = _error_parts(exc)
            retry_after = retry_after_from_error(exc)
            if self._adapt_to_rejection(exc.code, detail, kind):
                raise ModelError(
                    "Provider rejected a parameter; the adapter adjusted and will retry",
                    error_class="adapter_parameter",
                    retryable=True,
                    http_status=exc.code,
                    provider_code=provider_code,
                    retry_after_seconds=retry_after,
                ) from exc
            error_class, retryable = self._classify(exc.code, provider_code, kind)
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
        return response, response.headers.get("request-id")

    def _adapt_to_rejection(self, status: int, detail: str, kind: str | None) -> bool:
        if status not in (400, 404, 422):
            return False
        text = f"{detail} {kind or ''}".lower()
        # Only an output-format rejection justifies dropping native JSON mode;
        # a tool-input schema error must not silently change the response mode.
        if (
            "output_config" in text
            or "output_format" in text
            or "structured output" in text
        ) and self._structured_mode == "output_config":
            self._structured_mode = "prompt"
            return True
        return False

    @staticmethod
    def _classify(status: int, provider_code: str | None, kind: str | None) -> tuple[str, bool]:
        joined = f"{provider_code or ''} {kind or ''}".lower()
        if "request_too_large" in joined or ("context" in joined and "length" in joined):
            return "context_length", False
        if "refusal" in joined:
            return "refusal", False
        return _classify_provider_error(status, provider_code, kind)

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        response, request_id = self._open(payload)
        with response:
            raw = bounded_read(response, _MAX_JSON_BYTES)
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
    def _iter_stream_frames(self, response: Any) -> Iterator[tuple[str, Any]]:
        """Assemble Anthropic stream events into one completion-shaped object."""
        state: dict[str, Any] = {
            "id": None,
            "model": self.model,
            "blocks": {},
            "order": [],
            "stop_reason": None,
            "usage": {},
        }

        def block(index: int, kind: str) -> dict[str, Any]:
            entry = state["blocks"].get(index)
            if entry is None:
                entry = {"type": kind, "text": "", "json": "", "id": None, "name": None}
                state["blocks"][index] = entry
                state["order"].append(index)
            return entry

        for frame in iter_sse_frames(response):
            try:
                event = json.loads(frame.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type") or frame.event
            if event_type == "error":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                message = str(error.get("message") or "provider stream error")[:500]
                kind = str(error.get("type") or "stream_error")
                error_class, retryable = self._classify(529 if kind == "overloaded_error" else 400, None, kind)
                raise ModelError(message, error_class=error_class, retryable=retryable)
            if event_type == "message_start":
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                state["id"] = message.get("id") or state["id"]
                state["model"] = message.get("model") or state["model"]
                usage = message.get("usage")
                if isinstance(usage, dict):
                    state["usage"].update(usage)
                continue
            if event_type == "content_block_start":
                index = int(event.get("index") or 0)
                raw = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
                entry = block(index, str(raw.get("type") or "text"))
                entry["id"] = raw.get("id") or entry["id"]
                entry["name"] = raw.get("name") or entry["name"]
                if isinstance(raw.get("text"), str):
                    entry["text"] += raw["text"]
                    if raw["text"]:
                        yield "text", raw["text"]
                continue
            if event_type == "content_block_delta":
                index = int(event.get("index") or 0)
                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                kind = delta.get("type")
                entry = block(index, "text" if kind == "text_delta" else "tool_use")
                if kind == "text_delta" and isinstance(delta.get("text"), str):
                    entry["text"] += delta["text"]
                    if delta["text"]:
                        yield "text", delta["text"]
                elif kind == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                    entry["json"] += delta["partial_json"]
                continue
            if event_type == "message_delta":
                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                if delta.get("stop_reason"):
                    state["stop_reason"] = delta["stop_reason"]
                usage = event.get("usage")
                if isinstance(usage, dict):
                    state["usage"].update(usage)
                continue
            if event_type == "message_stop":
                break
            # content_block_stop, ping and unknown events need no accumulation.

        content: list[dict[str, Any]] = []
        for index in state["order"]:
            entry = state["blocks"][index]
            if entry["type"] == "text":
                content.append({"type": "text", "text": entry["text"]})
            elif entry["type"] == "tool_use":
                arguments = _tool_input(entry["json"])
                content.append(
                    {
                        "type": "tool_use",
                        "id": entry["id"] or f"toolu_{index}",
                        "name": entry["name"] or "",
                        "input": arguments,
                    }
                )
        yield (
            "final",
            (
                {
                    "id": state["id"],
                    "model": state["model"],
                    "content": content,
                    "stop_reason": state["stop_reason"],
                    "usage": state["usage"],
                },
                None,
            ),
        )

    # ------------------------------------------------------------------ parsing
    def _parse_response(
        self, data: dict[str, Any], request: ModelRequest
    ) -> tuple[str, dict[str, Any] | None, list[ToolCall]]:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ModelError(
                "Provider response contained no content blocks",
                error_class="protocol",
                retryable=False,
            )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block.get("type") == "tool_use":
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or f"toolu_{index}"),
                        name=name,
                        arguments=_parse_tool_arguments(block.get("input") or {}),
                    )
                )
        stop_reason = data.get("stop_reason")
        if stop_reason and stop_reason not in _KNOWN_STOP_REASONS:
            log.warning("unexpected anthropic stop_reason", extra={"stop_reason": stop_reason})
        if stop_reason == "refusal":
            raise ModelError(
                "The provider refused the request for policy reasons",
                error_class="refusal",
                retryable=False,
                hint="Review the prompt and safety policy; the output is not a valid answer.",
            )
        if stop_reason == "max_tokens":
            log.warning("anthropic response was truncated by max_tokens")
        text = "".join(text_parts)
        if request.json_schema is None or (not text.strip() and tool_calls):
            return text, None, tool_calls
        try:
            structured = _extract_json_object(text)
        except ValueError as exc:
            raise ModelError(
                "Model returned output that was not valid JSON",
                error_class="structured_output",
                retryable=True,
            ) from exc
        return text, structured, tool_calls

    def _usage(self, data: dict[str, Any]) -> ModelUsage:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = _as_int(usage.get("input_tokens"))
        cache_write = _as_int(usage.get("cache_creation_input_tokens"))
        cache_read = _as_int(usage.get("cache_read_input_tokens"))
        prompt_tokens = None
        if input_tokens is not None:
            # Anthropic reports input_tokens separately from cache tokens. The
            # full prompt size (and therefore the budget) is the sum.
            prompt_tokens = input_tokens + (cache_write or 0) + (cache_read or 0)
        completion_tokens = _as_int(usage.get("output_tokens"))
        total_tokens = (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
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
            # Without user-supplied cache rates this is an upper bound: cached
            # prompt tokens are priced at the configured input rate.
            cost = (
                prompt_tokens / 1_000_000 * self.input_cost_per_mtok
                + completion_tokens / 1_000_000 * self.output_cost_per_mtok
            )
            basis = "user-configured prices (USD per 1M tokens)"
            if cache_write or cache_read:
                basis += "; cached prompt tokens priced at the input rate (upper bound)"
        return ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            cost_basis=basis,
            cached_prompt_tokens=cache_read,
        )

    @staticmethod
    def _retry_delay(exc: ModelError, retries: int) -> float:
        if exc.retry_after_seconds:
            return float(exc.retry_after_seconds)
        return float(min(8.0, 0.5 * (2 ** (retries - 1))))


# ---------------------------------------------------------------- helpers
def _breakpoints(indexes: list[int]) -> set[int]:
    """Normalise caller breakpoints to at most four non-negative indexes."""
    ordered = sorted({int(index) for index in indexes if int(index) >= 0})
    return set(ordered[:_MAX_CACHE_BREAKPOINTS])


def _with_cache_control(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return message
    if isinstance(content, list) and content:
        target = content[-1]
        if isinstance(target, dict):
            target["cache_control"] = {"type": "ephemeral"}
            return message
    # No cacheable block (e.g. an empty assistant turn): leave it unchanged.
    return message


def _tool_result_block(message: ChatMessage) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id or "",
        "content": message.content,
    }


def _append_user_text(messages: list[dict[str, Any]], text: str) -> None:
    """Merge instruction text into the trailing user turn when possible."""
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, list):
            content.append({"type": "text", "text": text})
            return
        if isinstance(content, str):
            messages[-1]["content"] = f"{content}\n\n{text}"
            return
    messages.append({"role": "user", "content": text})


def _tool_input(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            return _extract_json_object(raw)
        except ValueError:
            return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls or []:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    role = message.role if message.role in ("user", "assistant") else "user"
    return {"role": role, "content": blocks or message.content}


__all__ = ["AnthropicProvider"]

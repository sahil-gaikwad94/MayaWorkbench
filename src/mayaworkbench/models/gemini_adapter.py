"""Google Gemini generateContent adapter (stdlib only).

Uses the native ``POST /models/{model}:generateContent`` endpoint and the
``:streamGenerateContent?alt=sse`` endpoint for streaming. The API key is
injected (or read from ``MAYA_GEMINI_API_KEY``), sent only as an outgoing
header, and never emitted by logs, results or descriptions.

Contract details implemented from the current API reference:

* structured output sends **both** ``responseMimeType: application/json`` and a
  projected ``responseSchema`` (Gemini's OpenAPI subset), degrading to MIME-only
  and then prompt-only if a model rejects the schema;
* a ``tool`` message is sent as a ``functionResponse`` part whose ``name`` is
  the declared function name, resolved from the assistant's earlier
  ``functionCall`` (Gemini correlates by name, not by an id);
* prompt blocks (``promptFeedback.blockReason``) and blocked finish reasons are
  surfaced as typed ``content_filter`` errors instead of being returned as an
  empty answer;
* ``thoughtsTokenCount`` is included in completion tokens and
  ``cachedContentTokenCount`` is reported as cached prompt tokens;
* streaming is available buffered (``generate(stream=True)``) and incrementally
  (``stream``), using the documented SSE framing.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
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
    _classify_provider_error,
    _error_parts,
    _extract_json_object,
    _parse_tool_arguments,
)
from mayaworkbench.models.schema import SchemaProjectionError, gemini_schema
from mayaworkbench.models.sse import (
    bounded_read,
    iter_sse_frames,
    retry_after_from_error,
)
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.gemini_adapter")

_BLOCKED_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
}
_KNOWN_FINISH_REASONS = {
    "STOP",
    "MAX_TOKENS",
    "FINISH_REASON_UNSPECIFIED",
    "OTHER",
    "MALFORMED_FUNCTION_CALL",
    "UNEXPECTED_TOOL_CALL",
    *_BLOCKED_FINISH_REASONS,
}
_MAX_JSON_BYTES = 16_000_000
_MAX_ADAPTATIONS = 4


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str | None = None,
        api_key_env: str = "MAYA_GEMINI_API_KEY",
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
                "No Gemini API key is available",
                error_class="auth",
                hint=f"Set ${api_key_env} in the environment running maya.",
            )
        self.base_url = base_url.rstrip("/")
        self.model = model.strip().removeprefix("models/")
        self._api_key = key
        self._api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.input_cost_per_mtok = input_cost_per_mtok
        self.output_cost_per_mtok = output_cost_per_mtok
        #: ``schema`` -> ``mime`` -> ``prompt`` after observed rejections.
        self._structured_mode = "schema"

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=self._structured_mode != "prompt",
            streaming=True,
            tool_calling=True,
            parallel_tool_calls=False,
            prompt_caching=False,
            provider_family="gemini",
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
                    model=str(data.get("modelVersion") or self.model),
                    provider_request_id=request_id,
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
            model=str(data.get("modelVersion") or self.model),
            provider_request_id=stream_request_id or request_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            retry_count=attempts - 1,
            used_structured_output=bool(request.json_schema) and self._structured_mode != "prompt",
            tool_calls=tool_calls,
            is_final=True,
        )

    # ------------------------------------------------------------------ payload
    def _build_payload(self, request: ModelRequest, *, repair_notice: str | None) -> dict[str, Any]:
        if request.cache_breakpoints:
            raise ModelError(
                "GeminiProvider does not implement explicit prompt caching",
                error_class="unsupported",
                retryable=False,
                hint="Remove cache_breakpoints or use an adapter reporting prompt_caching=True.",
            )
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        tool_names = _tool_name_index(request.messages)
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                contents.append(_content_payload(message, tool_names))
        if repair_notice:
            _append_user_text(contents, repair_notice)

        generation_config: dict[str, Any] = {
            "maxOutputTokens": request.max_output_tokens or self.max_output_tokens
        }
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        elif self.temperature:
            generation_config["temperature"] = self.temperature
        if request.json_schema and self._structured_mode in {"schema", "mime"}:
            generation_config["responseMimeType"] = "application/json"
            if self._structured_mode == "schema":
                try:
                    generation_config["responseSchema"] = gemini_schema(request.json_schema)
                except SchemaProjectionError as exc:
                    log.info("falling back to JSON MIME mode", extra={"reason": str(exc)})
                    self._structured_mode = "mime"
            if self._structured_mode == "mime":
                _append_user_text(
                    contents,
                    "Return only a JSON object matching this schema: "
                    + json.dumps(request.json_schema, ensure_ascii=False, sort_keys=True),
                )
        elif request.json_schema:
            _append_user_text(
                contents,
                "Return only a JSON object matching this schema, with no prose: "
                + json.dumps(request.json_schema, ensure_ascii=False, sort_keys=True),
            )

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if request.tools:
            declarations = []
            for tool in request.tools:
                parameters = _tool_parameters(tool.parameters)
                declarations.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": parameters,
                    }
                )
            payload["tools"] = [{"functionDeclarations": declarations}]
            choice = request.tool_choice
            if choice in ("auto", "none", "any", "required"):
                mode = "ANY" if choice in ("any", "required") else choice.upper()
                payload["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
            elif any(tool.name == choice for tool in request.tools):
                payload["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [choice],
                    }
                }
        return payload

    # ---------------------------------------------------------------- transport
    def _open(self, payload: dict[str, Any], *, stream: bool = False):  # type: ignore[no-untyped-def]
        body = json.dumps(payload).encode("utf-8")
        encoded_model = urllib.parse.quote(self.model, safe="")
        suffix = ":streamGenerateContent?alt=sse" if stream else ":generateContent"
        http_request = urllib.request.Request(
            f"{self.base_url}/models/{encoded_model}{suffix}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "X-Goog-Api-Key": self._api_key,
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
        return response, response.headers.get("x-request-id")

    def _adapt_to_rejection(self, status: int, detail: str, kind: str | None) -> bool:
        if status not in (400, 404, 422):
            return False
        text = f"{detail} {kind or ''}".lower()
        if (
            "responseschema" in text
            or "response_schema" in text
            or "responseformat" in text
            or "response_mime_type" in text
            or "responsemimetype" in text
            or "response mime type" in text
            or "json mime" in text
        ) and self._structured_mode != "prompt":
            if self._structured_mode == "schema":
                self._structured_mode = "mime"
            else:
                self._structured_mode = "prompt"
            return True
        return False

    @staticmethod
    def _classify(status: int, provider_code: str | None, kind: str | None) -> tuple[str, bool]:
        joined = f"{provider_code or ''} {kind or ''}".upper()
        if "UNAUTHENTICATED" in joined or "PERMISSION_DENIED" in joined:
            return "auth", False
        if "RESOURCE_EXHAUSTED" in joined:
            return "rate_limit", True
        if "DEADLINE_EXCEEDED" in joined:
            return "timeout", True
        if "UNAVAILABLE" in joined or "INTERNAL" in joined:
            return "server", True
        if "INVALID_ARGUMENT" in joined:
            return "bad_request", False
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
        state: dict[str, Any] = {
            "text": [],
            "calls": [],
            "finish_reason": None,
            "block_reason": None,
            "usage": {},
            "model": None,
        }
        for frame in iter_sse_frames(response):
            data = frame.data.strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            feedback = chunk.get("promptFeedback")
            if isinstance(feedback, dict) and feedback.get("blockReason"):
                state["block_reason"] = feedback["blockReason"]
            if chunk.get("modelVersion"):
                state["model"] = chunk["modelVersion"]
            usage = chunk.get("usageMetadata")
            if isinstance(usage, dict):
                state["usage"] = usage
            candidates = chunk.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                continue
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                continue
            if candidate.get("finishReason"):
                state["finish_reason"] = candidate["finishReason"]
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("thought") is True:
                    continue  # reasoning text is not answer text
                if isinstance(part.get("text"), str) and part["text"]:
                    state["text"].append(part["text"])
                    yield "text", part["text"]
                function_call = part.get("functionCall")
                if isinstance(function_call, dict) and isinstance(function_call.get("name"), str):
                    state["calls"].append(function_call)
        if state["block_reason"] and not state["calls"] and not state["text"]:
            raise ModelError(
                f"The provider blocked the prompt ({state['block_reason']})",
                error_class="content_filter",
                retryable=False,
            )
        _raise_for_finish_reason(state["finish_reason"], state["text"])
        yield (
            "final",
            (
                {
                    "modelVersion": state["model"] or self.model,
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    *[{"text": text} for text in state["text"]],
                                    *[
                                        {"functionCall": call}
                                        for call in state["calls"]
                                    ],
                                ]
                            },
                            "finishReason": state["finish_reason"] or "STOP",
                        }
                    ],
                    "usageMetadata": state["usage"],
                },
                None,
            ),
        )

    # ------------------------------------------------------------------ parsing
    def _parse_response(
        self, data: dict[str, Any], request: ModelRequest
    ) -> tuple[str, dict[str, Any] | None, list[ToolCall]]:
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ModelError(
                f"The provider blocked the prompt ({feedback['blockReason']})",
                error_class="content_filter",
                retryable=False,
            )
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ModelError(
                "Provider response contained no candidates",
                error_class="protocol",
                retryable=False,
            )
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ModelError(
                "Provider response contained an invalid candidate",
                error_class="protocol",
                retryable=False,
            )
        finish_reason = candidate.get("finishReason")
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        for index, part in enumerate(parts or []):
            if not isinstance(part, dict):
                continue
            if part.get("thought") is True:
                continue
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                name = function_call.get("name")
                if not isinstance(name, str) or not name:
                    continue
                tool_calls.append(
                    ToolCall(
                        id=str(function_call.get("id") or f"gemini_call_{index}"),
                        name=name,
                        arguments=_parse_tool_arguments(function_call.get("args") or {}),
                    )
                )
        text = "".join(text_parts)
        _raise_for_finish_reason(finish_reason, text_parts + [call.name for call in tool_calls])
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
        usage = data.get("usageMetadata")
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = _as_int(usage.get("promptTokenCount"))
        candidates_tokens = _as_int(usage.get("candidatesTokenCount"))
        thoughts = _as_int(usage.get("thoughtsTokenCount"))
        completion_tokens = None
        if candidates_tokens is not None:
            completion_tokens = candidates_tokens + (thoughts or 0)
        total_tokens = _as_int(usage.get("totalTokenCount"))
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        cached = _as_int(usage.get("cachedContentTokenCount"))
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
            if thoughts:
                basis += "; reasoning tokens priced at the output rate"
            if cached:
                basis += "; cached prompt tokens priced at the input rate (upper bound)"
        return ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            cost_basis=basis,
            cached_prompt_tokens=cached,
            reasoning_tokens=thoughts,
        )

    @staticmethod
    def _retry_delay(exc: ModelError, retries: int) -> float:
        if exc.retry_after_seconds:
            return float(exc.retry_after_seconds)
        return float(min(8.0, 0.5 * (2 ** (retries - 1))))


# ---------------------------------------------------------------- helpers
def _raise_for_finish_reason(finish_reason: Any, content: Any) -> None:
    if not isinstance(finish_reason, str):
        return
    if finish_reason in _BLOCKED_FINISH_REASONS:
        raise ModelError(
            f"The provider blocked the response ({finish_reason})",
            error_class="content_filter",
            retryable=False,
            hint="The prompt or response was filtered; no answer text is available.",
        )
    if finish_reason in {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"}:
        raise ModelError(
            f"The provider produced an unusable function call ({finish_reason})",
            error_class="protocol",
            retryable=False,
        )
    if finish_reason not in _KNOWN_FINISH_REASONS:
        log.warning("unexpected gemini finishReason", extra={"finish_reason": finish_reason})
    if finish_reason == "MAX_TOKENS":
        log.warning("gemini response was truncated by maxOutputTokens")


def _append_user_text(contents: list[dict[str, Any]], text: str) -> None:
    """Merge instruction text into the trailing user turn when possible."""
    if contents and contents[-1].get("role") == "user":
        parts = contents[-1].get("parts")
        if isinstance(parts, list):
            parts.append({"text": text})
            return
    contents.append({"role": "user", "parts": [{"text": text}]})


def _tool_name_index(messages: list[ChatMessage]) -> dict[str, str]:
    """Map tool-call ids to function names from earlier assistant turns."""
    index: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or []:
            index[call.id] = call.name
    return index


def _content_payload(message: ChatMessage, tool_names: dict[str, str]) -> dict[str, Any]:
    if message.role == "tool":
        name = message.tool_name or tool_names.get(message.tool_call_id or "") or "tool"
        return {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": name,
                        "response": {"content": message.content},
                    }
                }
            ],
        }
    role = "model" if message.role == "assistant" else "user"
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"text": message.content})
    for call in message.tool_calls or []:
        parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
    return {"role": role, "parts": parts}


def _tool_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    try:
        return gemini_schema(parameters)
    except (SchemaProjectionError, ValueError):
        # A caller-supplied tool schema that cannot be projected is passed
        # through unchanged; the provider will reject it with a typed error
        # rather than the adapter silently weakening the tool contract.
        return parameters


__all__ = ["GeminiProvider"]

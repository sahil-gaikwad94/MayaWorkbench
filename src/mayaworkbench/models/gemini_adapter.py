"""Google Gemini generateContent adapter (stdlib only).

Uses the native ``POST /models/{model}:generateContent`` endpoint. The API key
is injected (or read from ``MAYA_GEMINI_API_KEY``), sent only as an outgoing
header, and never emitted by logs, results or descriptions.

The adapter implements buffered generation and function calling. Streaming and
prompt caching are not implemented and are reported as such by ``capabilities``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
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
    _classify_status,
    _extract_json_object,
    _parse_tool_arguments,
    _safe_error_detail,
)
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.gemini_adapter")

_FINISH_REASONS = {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "FINISH_REASON_UNSPECIFIED"}


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
        self._structured_supported = True

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=self._structured_supported,
            streaming=False,
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
            "note": "The key value is never included in output, results or events.",
        }

    # ------------------------------------------------------------------ public
    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.stream:
            raise ModelError(
                "GeminiProvider does not implement streaming",
                error_class="unsupported",
                retryable=False,
                hint="Use generate(request) without stream=True, or configure a streaming adapter.",
            )
        if request.cache_breakpoints:
            raise ModelError(
                "GeminiProvider does not implement prompt caching",
                error_class="unsupported",
                retryable=False,
                hint="Remove cache_breakpoints or use an adapter reporting prompt_caching=True.",
            )
        started = time.monotonic()
        attempts = 0
        repair_notice: str | None = None
        last_error: ModelError | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                payload = self._build_payload(request, repair_notice=repair_notice)
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
                    used_structured_output=bool(request.json_schema) and self._structured_supported,
                    tool_calls=tool_calls,
                )
            except ModelError as exc:
                last_error = exc
                if exc.error_class == "structured_output" and attempts <= self.max_retries:
                    repair_notice = (
                        "Your previous response was not a valid JSON object matching the schema. "
                        "Return only the JSON object, with no prose or code fences."
                    )
                    log.warning("retrying after invalid structured output", extra={"attempt": attempts})
                    continue
                if not exc.retryable or attempts > self.max_retries:
                    raise
                sleep_for = 0.5 * (2 ** (attempts - 1))
                log.warning(
                    "retrying model call",
                    extra={"attempt": attempts, "error_class": exc.error_class, "sleep_s": sleep_for},
                )
                time.sleep(sleep_for)

        raise last_error or ModelError("Model call failed without a recorded error")

    # ------------------------------------------------------------------ payload
    def _build_payload(self, request: ModelRequest, *, repair_notice: str | None) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                contents.append(_content_payload(message))
        if repair_notice:
            contents.append({"role": "user", "parts": [{"text": repair_notice}]})

        generation_config: dict[str, Any] = {
            "maxOutputTokens": request.max_output_tokens or self.max_output_tokens
        }
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        elif self.temperature:
            generation_config["temperature"] = self.temperature
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in request.tools
                    ]
                }
            ]
            choice = request.tool_choice
            if choice in ("auto", "none", "any", "required"):
                mode = "ANY" if choice in ("any", "required") else choice.upper()
                payload["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
            elif any(tool.name == choice for tool in request.tools):
                payload["toolConfig"] = {
                    "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [choice]}
                }
        return payload

    # ---------------------------------------------------------------- transport
    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        body = json.dumps(payload).encode("utf-8")
        encoded_model = urllib.parse.quote(self.model, safe="")
        http_request = urllib.request.Request(
            f"{self.base_url}/models/{encoded_model}:generateContent",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Goog-Api-Key": self._api_key,
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                request_id = response.headers.get("x-request-id")
        except urllib.error.HTTPError as exc:
            detail = _safe_error_detail(exc)
            error_class, retryable = _classify_status(exc.code)
            raise ModelError(
                f"Provider returned HTTP {exc.code}: {detail[:500]}",
                error_class=error_class,
                retryable=retryable,
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

    # ------------------------------------------------------------------ parsing
    def _parse_response(
        self, data: dict[str, Any], request: ModelRequest
    ) -> tuple[str, dict[str, Any] | None, list[ToolCall]]:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ModelError(
                "Provider response contained no candidates",
                error_class="protocol",
                retryable=False,
            )
        candidate = candidates[0]
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ModelError(
                "Provider response contained no content parts",
                error_class="protocol",
                retryable=False,
            )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
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
        finish_reason = candidate.get("finishReason")
        if finish_reason and finish_reason not in _FINISH_REASONS:
            log.warning("unexpected gemini finishReason", extra={"finish_reason": finish_reason})
        if finish_reason == "MAX_TOKENS":
            log.warning("gemini response was truncated by maxOutputTokens")
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
        usage = data.get("usageMetadata")
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = _as_int(usage.get("promptTokenCount"))
        completion_tokens = _as_int(usage.get("candidatesTokenCount"))
        total_tokens = _as_int(usage.get("totalTokenCount"))
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
        return ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            cost_basis=basis,
        )


def _content_payload(message: ChatMessage) -> dict[str, Any]:
    role = "model" if message.role == "assistant" else "user"
    parts: list[dict[str, Any]] = []
    if message.role == "tool":
        parts.append(
            {
                "functionResponse": {
                    "name": message.tool_call_id or "tool",
                    "response": {"content": message.content},
                }
            }
        )
        return {"role": role, "parts": parts}
    if message.content:
        parts.append({"text": message.content})
    for call in message.tool_calls or []:
        parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
    return {"role": role, "parts": parts}


__all__ = ["GeminiProvider"]

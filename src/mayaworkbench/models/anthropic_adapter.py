"""Native Anthropic Messages adapter (stdlib only).

Uses ``POST /v1/messages`` with the ``anthropic-version: 2023-06-01`` header.
The API key is injected (or read from ``MAYA_ANTHROPIC_API_KEY``), used only in
the outgoing request and never emitted by logs, results or descriptions.

This adapter implements buffered generation, tool use and prompt caching. It
does not implement server-sent event streaming and reports ``streaming=False``;
a request with ``stream=True`` is refused rather than silently downgraded.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
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
    _as_str,
    _classify_status,
    _extract_json_object,
    _parse_tool_arguments,
    _safe_error_detail,
)
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.anthropic_adapter")

_ANTHROPIC_VERSION = "2023-06-01"
_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn"}


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
        self._structured_supported = True

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=self._structured_supported,
            streaming=False,
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
            "note": "The key value is never included in output, results or events.",
        }

    # ------------------------------------------------------------------ public
    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.stream:
            raise ModelError(
                "AnthropicProvider does not implement streaming",
                error_class="unsupported",
                retryable=False,
                hint="Use generate(request) without stream=True, or configure a streaming adapter.",
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
                    model=str(data.get("model") or self.model),
                    provider_request_id=request_id or _as_str(data.get("id")),
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
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                messages.append(_message_payload(message))
        if repair_notice:
            messages.append({"role": "user", "content": repair_notice})

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
            if request.cache_breakpoints:
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system_text
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
            elif any(tool.name == choice for tool in request.tools):
                payload["tool_choice"] = {"type": "tool", "name": choice}
        return payload

    # ---------------------------------------------------------------- transport
    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Api-Key": self._api_key,
                "Anthropic-Version": _ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                request_id = response.headers.get("request-id")
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
        if stop_reason and stop_reason not in _STOP_REASONS:
            log.warning("unexpected anthropic stop_reason", extra={"stop_reason": stop_reason})
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
        prompt_tokens = _as_int(usage.get("input_tokens"))
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


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            ],
        }
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

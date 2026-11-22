"""OpenAI-compatible chat completions adapter.

Works with any endpoint that implements ``POST {base_url}/chat/completions``
with the OpenAI message/usage shape. Implemented with the standard library so a
provider SDK never becomes a dependency of the domain layer.

Failure handling:

* 401/403 -> non-retryable ``auth`` error, with no key material in the message;
* 429/5xx/network -> bounded exponential backoff;
* malformed structured output -> one repair attempt, then a typed error.

Streaming is opt-in per request (``request.stream=True``). The adapter reads a
bounded ``text/event-stream`` body and returns one assembled ``ModelResponse``;
it does not implement the incremental ``StreamingModelProvider`` protocol.
"""

from __future__ import annotations

import json
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
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.openai_compatible")

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

#: Hard bounds for server-sent events: a hostile or broken upstream can never
#: make the adapter buffer an unbounded response.
_MAX_SSE_BYTES = 4_000_000
_MAX_SSE_EVENTS = 10_000
_MAX_SSE_LINE = 65_536


class OpenAICompatibleProvider:
    name = "openai_compatible"
    #: SSE is consumed in ``generate(stream=True)`` and assembled before return;
    #: there is no incremental ``stream()`` iterator on this adapter.
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
        self._structured_supported = True

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=self._structured_supported,
            streaming=True,
            tool_calling=True,
            parallel_tool_calls=True,
            provider_family="openai_compatible",
            max_context_tokens=None,
        )

    # ------------------------------------------------------------------ public
    def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        attempts = 0
        repair_notice: str | None = None
        last_error: ModelError | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                payload = self._build_payload(request, repair_notice=repair_notice)
                if request.stream:
                    data, request_id = self._post_stream(payload)
                else:
                    data, request_id = self._post(payload)
                text, structured, tool_calls = self._parse_response(data, request)
                usage = self._usage(data)
                return ModelResponse(
                    text=text,
                    structured=structured,
                    usage=usage,
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
        messages = [_message_payload(message) for message in request.messages]
        if repair_notice:
            messages.append({"role": "user", "content": repair_notice})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature
            if request.temperature is not None
            else self.temperature,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
        }
        if request.json_schema and self._structured_supported:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
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
            payload["parallel_tool_calls"] = True
        if request.stream:
            payload["stream"] = True
        return payload

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
            detail = _safe_error_detail(exc)
            error_class, retryable = _classify_status(exc.code)
            if (
                exc.code == 400
                and "response_format" in detail.lower()
                and self._structured_supported
            ):
                # Provider does not support json_schema: degrade to prompt-only JSON.
                self._structured_supported = False
                log.info("provider does not support json_schema response_format; falling back")
                raise ModelError(
                    "Provider rejected structured output mode",
                    error_class="structured_output_unsupported",
                    retryable=True,
                ) from exc
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
        return response, response.headers.get("x-request-id")

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        response, request_id = self._open(payload)
        with response:
            raw = response.read()
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

    # -------------------------------------------------------------- SSE parsing
    def _post_stream(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        response, request_id = self._open(payload, stream=True)
        with response:
            try:
                data = self._consume_sse(response)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise _stream_transport_error(exc, self.timeout_seconds) from exc
        return data, request_id

    def _consume_sse(self, response: Any) -> dict[str, Any]:
        """Read a bounded SSE body and assemble it into one completion-shaped object."""
        state: dict[str, Any] = {
            "text": [],
            "calls": {},
            "call_ids": {},
            "usage": {},
            "model": None,
            "id": None,
        }
        data_lines: list[str] = []
        event_count = 0
        byte_count = 0
        finished = False

        def emit() -> None:
            nonlocal data_lines, event_count, finished
            if not data_lines:
                return
            frame = "\n".join(data_lines)
            data_lines = []
            event_count += 1
            if event_count > _MAX_SSE_EVENTS:
                raise ModelError(
                    "SSE stream exceeded the event limit",
                    error_class="protocol",
                    retryable=False,
                )
            if frame.strip() == "[DONE]":
                finished = True
                return
            try:
                chunk = json.loads(frame)
            except json.JSONDecodeError:
                return  # tolerate malformed keepalive frames
            if not isinstance(chunk, dict):
                return
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
                return
            choice = choices[0]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                state["text"].append(piece)
            elif isinstance(piece, list):
                for part in piece:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        state["text"].append(part["text"])
            raw_calls = delta.get("tool_calls")
            if not isinstance(raw_calls, list):
                return
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

        while not finished:
            line = response.readline(_MAX_SSE_LINE + 1)
            if not line:
                break
            byte_count += len(line)
            if byte_count > _MAX_SSE_BYTES:
                raise ModelError(
                    "SSE stream exceeded the byte limit",
                    error_class="protocol",
                    retryable=False,
                )
            if len(line) > _MAX_SSE_LINE:
                raise ModelError(
                    "SSE line exceeded the length limit",
                    error_class="protocol",
                    retryable=False,
                )
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text:
                emit()
            elif text.startswith("data:"):
                data_lines.append(text[5:].lstrip(" "))
            # event:, id:, retry: and comment (:) frames carry no completion data.
        emit()

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
        return {
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
        }

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


# ---------------------------------------------------------------- helpers
def _message_payload(message: ChatMessage) -> dict[str, Any]:
    item: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "tool" and message.tool_call_id:
        item["tool_call_id"] = message.tool_call_id
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
    if status in (401, 403):
        return "auth", False
    if status == 404:
        return "not_found", False
    if status == 400:
        return "bad_request", False
    if status in RETRYABLE_STATUS:
        return "rate_limit" if status == 429 else "server", True
    return "http", False


def _safe_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(4000)
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    return error["message"]
                if isinstance(error, str):
                    return error
        except json.JSONDecodeError:
            pass
        return text
    except Exception:  # pragma: no cover - best effort only
        return exc.reason or "unknown error"


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return str(value) if value else None


__all__ = ["OpenAICompatibleProvider"]

"""OpenAI-compatible chat completions adapter.

Works with any endpoint that implements ``POST {base_url}/chat/completions``
with the OpenAI message/usage shape. Implemented with the standard library so a
provider SDK never becomes a dependency of the domain layer.

Failure handling:

* 401/403 -> non-retryable ``auth`` error, with no key material in the message;
* 429/5xx/network -> bounded exponential backoff;
* malformed structured output -> one repair attempt, then a typed error.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from mayaworkbench.errors import ModelError
from mayaworkbench.models.base import (
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from mayaworkbench.observability.logging import get_logger

log = get_logger("models.openai_compatible")

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class OpenAICompatibleProvider:
    name = "openai_compatible"

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
            streaming=False,
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
                data, request_id = self._post(payload)
                text, structured = self._parse_response(data, request)
                usage = self._usage(data)
                return ModelResponse(
                    text=text,
                    structured=structured,
                    usage=usage,
                    provider=self.name,
                    model=str(data.get("model") or self.model),
                    provider_request_id=request_id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    retry_count=attempts - 1,
                    used_structured_output=bool(request.json_schema) and self._structured_supported,
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
        messages = [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
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
        return payload

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                request_id = response.headers.get("x-request-id")
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
    ) -> tuple[str, dict[str, Any] | None]:
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
        if not isinstance(content, str):
            raise ModelError(
                "Provider response content was not text",
                error_class="protocol",
                retryable=False,
            )
        if request.json_schema is None:
            return content, None
        try:
            structured = _extract_json_object(content)
        except ValueError as exc:
            raise ModelError(
                "Model returned output that was not valid JSON",
                error_class="structured_output",
                retryable=True,
            ) from exc
        return content, structured

    def _usage(self, data: dict[str, Any]) -> ModelUsage:
        usage = data.get("usage") or {}
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


__all__ = ["OpenAICompatibleProvider"]

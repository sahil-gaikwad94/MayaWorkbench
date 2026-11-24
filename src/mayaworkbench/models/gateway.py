"""Model gateway.

The gateway is the only path from workbench code to a model provider. It:

* enforces the data-sharing policy before any bytes leave the machine;
* refuses calls when no provider/key/model is configured, with a reason;
* records one durable invocation per call (provider, model, tokens, latency,
  retries, cost basis) without storing prompts or secrets;
* never lets a provider error masquerade as a successful answer.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable

from pydantic import BaseModel

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings, api_key_from_env, provider_key_env
from mayaworkbench.domain.enums import ActorKind, EventType
from mayaworkbench.domain.models import ModelInvocation, WorkflowEvent
from mayaworkbench.errors import ModelError, PolicyError
from mayaworkbench.ids import new_ulid
from mayaworkbench.models.base import ModelProvider, ModelRequest, ModelResponse
from mayaworkbench.models.local_openai_compatible import (
    LocalOpenAICompatibleProvider,
    is_local_host,
)
from mayaworkbench.models.openai_compatible import OpenAICompatibleProvider
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.storage.stores import EventStore, InvocationStore

log = get_logger("models.gateway")

ProviderFactory = Callable[[Settings], ModelProvider]


class ModelStatus(BaseModel):
    provider: str
    model: str
    data_policy: str
    key_present: bool
    callable: bool
    reason: str | None = None
    max_output_tokens: int
    timeout_seconds: int


_PROVIDER_BASE_URL = {
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
_OPENAI_DEFAULT_URL = "https://api.openai.com/v1"

#: Placeholder credential for a loopback server that requires no key. It is not
#: a secret and is never reported as one.
_LOCAL_UNAUTHENTICATED = "local-unauthenticated"


def _default_factory(settings: Settings) -> ModelProvider:
    kind = settings.model.provider
    if kind in {"anthropic", "gemini"}:
        from mayaworkbench.models.anthropic_adapter import AnthropicProvider
        from mayaworkbench.models.gemini_adapter import GeminiProvider

        adapter = AnthropicProvider if kind == "anthropic" else GeminiProvider
        key_env = provider_key_env(settings)
        base_url = settings.model.base_url
        if base_url == _OPENAI_DEFAULT_URL:
            base_url = _PROVIDER_BASE_URL[kind]
        return adapter(
            model=settings.model.model,
            base_url=base_url,
            api_key_env=key_env,
            timeout_seconds=settings.model.timeout_seconds,
            max_output_tokens=settings.model.max_output_tokens,
            temperature=settings.model.temperature,
            max_retries=settings.model.max_retries,
            input_cost_per_mtok=settings.model.input_cost_per_mtok,
            output_cost_per_mtok=settings.model.output_cost_per_mtok,
        )
    if kind == "local_openai_compatible":
        key_env = provider_key_env(settings)
        return LocalOpenAICompatibleProvider(
            base_url=settings.model.base_url,
            model=settings.model.model or "local-model",
            api_key=os.environ.get(key_env),
            data_policy=settings.data_policy,
            key_env=key_env,
            timeout_seconds=settings.model.timeout_seconds,
            max_output_tokens=settings.model.max_output_tokens,
            temperature=settings.model.temperature,
            max_retries=settings.model.max_retries,
            input_cost_per_mtok=settings.model.input_cost_per_mtok,
            output_cost_per_mtok=settings.model.output_cost_per_mtok,
        )
    api_key = api_key_from_env(settings)
    local_endpoint = is_local_host(settings.model.base_url)
    if api_key is None and not local_endpoint:
        raise ModelError(
            f"No API key found in ${settings.model.api_key_env}",
            error_class="auth",
            hint=f"Export {settings.model.api_key_env} in the environment running maya.",
        )
    return OpenAICompatibleProvider(
        base_url=settings.model.base_url,
        model=settings.model.model,
        # A loopback server may be unauthenticated; the header is still sent so
        # a local gateway that does require the configured key keeps working.
        api_key=api_key or _LOCAL_UNAUTHENTICATED,
        timeout_seconds=settings.model.timeout_seconds,
        max_output_tokens=settings.model.max_output_tokens,
        temperature=settings.model.temperature,
        max_retries=settings.model.max_retries,
        input_cost_per_mtok=settings.model.input_cost_per_mtok,
        output_cost_per_mtok=settings.model.output_cost_per_mtok,
    )


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        invocations: InvocationStore,
        events: EventStore,
        provider_factory: ProviderFactory | None = None,
    ):
        self.settings = settings
        self.invocations = invocations
        self.events = events
        self._factory = provider_factory or _default_factory

    # -------------------------------------------------------------------- state
    def status(self) -> ModelStatus:
        model = self.settings.model
        key_env = provider_key_env(self.settings)
        # Use the exact variable the selected adapter reads. A key exported under
        # a different provider's name must not make this provider look ready.
        key_present = bool(os.environ.get(key_env))
        local_endpoint = is_local_host(model.base_url)
        # Anthropic and Gemini constructors always require a key. An
        # OpenAI-compatible loopback endpoint may be unauthenticated.
        key_required = model.provider in {"anthropic", "gemini"} or not local_endpoint
        reason: str | None = None
        callable_ = True
        if model.provider == "none":
            callable_ = False
            reason = "no model provider configured"
        elif not local_endpoint and not self.settings.allows_remote_model():
            callable_ = False
            reason = (
                "data policy is LOCAL_ONLY; remote model calls are disabled. Set "
                "MAYA_DATA_POLICY=BYOK_REMOTE_MODEL or update config.json to allow them."
            )
        elif not model.model:
            callable_ = False
            reason = "no model name configured"
        elif key_required and not key_present:
            callable_ = False
            reason = f"no API key in ${key_env}"
        return ModelStatus(
            provider=model.provider,
            model=model.model,
            data_policy=self.settings.data_policy,
            key_present=key_present,
            callable=callable_,
            reason=reason,
            max_output_tokens=model.max_output_tokens,
            timeout_seconds=model.timeout_seconds,
        )

    # -------------------------------------------------------------------- calls
    def structured(
        self,
        request: ModelRequest,
        *,
        project_id: str,
        snapshot_id: str | None,
        purpose: str,
        correlation_id: str | None = None,
    ) -> tuple[ModelResponse, ModelInvocation]:
        status = self.status()
        invocation = ModelInvocation(
            id="inv_" + new_ulid(),
            project_id=project_id,
            snapshot_id=snapshot_id,
            purpose=purpose,
            provider=status.provider,
            model=status.model or "<unset>",
            status="STARTED",
            details={
                "message_count": len(request.messages),
                "schema_name": request.schema_name,
                "structured": request.json_schema is not None,
                "data_policy": self.settings.data_policy,
            },
        )
        self.invocations.create(invocation)
        self._event(
            EventType.MODEL_INVOCATION_STARTED,
            project_id=project_id,
            snapshot_id=snapshot_id,
            invocation=invocation,
            correlation_id=correlation_id,
            payload={"purpose": purpose, "provider": status.provider, "model": status.model},
        )

        if not status.callable:
            self.invocations.mark_refused(invocation.id, reason=status.reason or "unavailable")
            self._event(
                EventType.MODEL_INVOCATION_FAILED,
                project_id=project_id,
                snapshot_id=snapshot_id,
                invocation=invocation,
                correlation_id=correlation_id,
                payload={"refused": True, "reason": status.reason},
            )
            message = status.reason or "model calls are unavailable"
            refusal: ModelError = (
                PolicyError(message)
                if "data policy" in message
                else ModelError(message, error_class="unavailable", hint="Run `maya model status`.")
            )
            refusal.invocation_id = invocation.id  # type: ignore[attr-defined]
            raise refusal

        estimated_input = _estimate_input_tokens(request, self.settings)
        if estimated_input > self.settings.model.max_input_tokens:
            self.invocations.mark_refused(invocation.id, reason="input exceeds configured limit")
            self._event(
                EventType.MODEL_INVOCATION_FAILED,
                project_id=project_id,
                snapshot_id=snapshot_id,
                invocation=invocation,
                correlation_id=correlation_id,
                payload={"refused": True, "reason": "input_too_large"},
            )
            too_large = PolicyError(
                f"Prepared prompt is about {estimated_input} tokens, above the configured "
                f"max_input_tokens={self.settings.model.max_input_tokens}",
                hint="Lower MAYA_TOKEN_BUDGET or raise model.max_input_tokens deliberately.",
            )
            too_large.invocation_id = invocation.id  # type: ignore[attr-defined]
            raise too_large

        started = time.monotonic()
        try:
            provider = self._factory(self.settings)
            response = provider.generate(request)
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded
            error_class = exc.error_class if isinstance(exc, ModelError) else "unexpected"
            message = exc.message[:500] if isinstance(exc, ModelError) else f"{type(exc).__name__}: {exc}"
            attempts = getattr(exc, "attempts", 0) if isinstance(exc, ModelError) else 0
            failure_details: dict[str, object] = {"purpose": purpose}
            if isinstance(exc, ModelError):
                if exc.http_status is not None:
                    failure_details["http_status"] = exc.http_status
                if exc.provider_code:
                    failure_details["provider_code"] = exc.provider_code
                if exc.retry_after_seconds is not None:
                    failure_details["retry_after_seconds"] = exc.retry_after_seconds
            self.invocations.fail(
                invocation.id,
                error_class=error_class,
                latency_ms=int((time.monotonic() - started) * 1000),
                # Record what actually happened, not the configured ceiling.
                retry_count=max(0, attempts - 1),
                details=failure_details,
            )
            self._event(
                EventType.MODEL_INVOCATION_FAILED,
                project_id=project_id,
                snapshot_id=snapshot_id,
                invocation=invocation,
                correlation_id=correlation_id,
                payload={"error_class": error_class, "message": message},
            )
            log.warning("model call failed", extra={"error_class": error_class, "purpose": purpose})
            failure: ModelError = (
                exc
                if isinstance(exc, ModelError)
                else ModelError(
                    "The model provider failed unexpectedly; no answer was produced.",
                    error_class="unexpected",
                )
            )
            failure.invocation_id = invocation.id  # type: ignore[attr-defined]
            if isinstance(exc, ModelError):
                raise
            raise failure from exc

        self.invocations.complete(
            invocation.id,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            latency_ms=response.latency_ms,
            retry_count=response.retry_count,
            provider_request_id=response.provider_request_id,
            cost_usd=response.usage.cost_usd,
            cost_basis=response.usage.cost_basis,
            provider=response.provider,
            model=response.model,
            details={
                "model": response.model,
                "used_structured_output": response.used_structured_output,
                "cached_prompt_tokens": response.usage.cached_prompt_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
            },
        )
        self._event(
            EventType.MODEL_INVOCATION_COMPLETED,
            project_id=project_id,
            snapshot_id=snapshot_id,
            invocation=invocation,
            correlation_id=correlation_id,
                            payload={
                "provider": response.provider,
                "model": response.model,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "cached_prompt_tokens": response.usage.cached_prompt_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
                "latency_ms": response.latency_ms,
                "retry_count": response.retry_count,
                "cost_usd": response.usage.cost_usd,
                "cost_basis": response.usage.cost_basis,
            },
        )
        return response, invocation

    # ------------------------------------------------------------------ helpers
    def _event(
        self,
        event_type: EventType,
        *,
        project_id: str,
        snapshot_id: str | None,
        invocation: ModelInvocation,
        correlation_id: str | None,
        payload: dict,
    ) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=project_id,
                snapshot_id=snapshot_id,
                entity_type="model_invocation",
                entity_id=invocation.id,
                actor_kind=ActorKind.MODEL,
                actor_name=invocation.provider,
                correlation_id=correlation_id,
                occurred_at=utcnow(),
                payload=payload,
            )
        )


def _estimate_input_tokens(request: ModelRequest, settings: Settings) -> int:
    """Conservative pre-flight estimate of the serialized prompt.

    Counting only message text under-estimates tool schemas, tool-call
    arguments and the response schema, which are all sent to the provider. The
    estimate uses the configured characters-per-token ratio and is a bound, not
    a claim of tokenizer exactness.
    """
    payload = {
        "messages": [message.model_dump(mode="json") for message in request.messages],
        "tools": [tool.model_dump(mode="json") for tool in (request.tools or [])],
        "schema": request.json_schema,
        "schema_name": request.schema_name if request.json_schema else None,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ratio = max(0.5, float(settings.retrieval.chars_per_token))
    return max(1, int(len(serialized) / ratio))


__all__ = ["ModelGateway", "ModelStatus", "ProviderFactory", "provider_key_env"]

"""Provider-neutral model contracts."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolDefinition(BaseModel):
    """A model-proposable tool. Execution authority stays with the workbench."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """A tool call proposed by a model (never executed by the adapter)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str
    tool_call_id: str | None = None
    #: Name of the function a ``tool`` message answers. OpenAI-compatible
    #: providers correlate by ``tool_call_id``; Gemini correlates by function
    #: name. Adapters that can derive the name from earlier messages may leave
    #: this unset, but callers should set it when they know it.
    tool_name: str | None = None
    tool_calls: list[ToolCall] | None = None


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    messages: list[ChatMessage]
    json_schema: dict[str, Any] | None = None
    schema_name: str = "response"
    max_output_tokens: int | None = None
    temperature: float | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: str = "auto"
    stream: bool = False
    #: Message indexes at which a provider should place a prompt-cache breakpoint.
    cache_breakpoints: list[int] = Field(default_factory=list)


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_basis: str | None = None
    #: Prompt tokens served from a provider cache (a subset of prompt_tokens).
    cached_prompt_tokens: int | None = None
    #: Output tokens spent on provider-side reasoning, when reported.
    reasoning_tokens: int | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    structured: dict[str, Any] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    provider: str
    model: str
    provider_request_id: str | None = None
    latency_ms: int = 0
    retry_count: int = 0
    used_structured_output: bool = False
    tool_calls: list[ToolCall] = Field(default_factory=list)
    #: True only on the terminal value of :meth:`StreamingModelProvider.stream`;
    #: intermediate values carry an incremental ``text`` delta.
    is_final: bool = False


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_output: bool
    streaming: bool = False
    max_context_tokens: int | None = None
    tool_calling: bool = False
    parallel_tool_calls: bool = False
    prompt_caching: bool = False
    provider_family: str = ""


class ModelProvider(Protocol):
    name: str
    model: str

    def capabilities(self) -> ModelCapabilities: ...

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class StreamingModelProvider(Protocol):
    """Incremental streaming contract, separate from buffered SSE assembly.

    Adapters that implement this protocol yield one or more ``ModelResponse``
    values as text arrives. Every intermediate value carries exactly one
    incremental ``text`` delta and ``is_final=False``. The last value has
    ``is_final=True`` and carries the assembled ``usage``, ``tool_calls`` and
    request metadata; its ``text`` is empty because the deltas were already
    delivered. An error is raised rather than yielded, so a caller never has to
    guess whether a returned value is a failure.

    ``generate(request.stream=True)`` is a different contract: it returns one
    fully assembled response. Callers must check ``capabilities().streaming``
    and the presence of ``stream`` before relying on incremental delivery.
    """

    name: str
    model: str

    def capabilities(self) -> ModelCapabilities: ...

    def stream(self, request: ModelRequest) -> Iterator[ModelResponse]: ...


__all__ = [
    "ChatMessage",
    "ToolDefinition",
    "ToolCall",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "ModelCapabilities",
    "ModelProvider",
    "StreamingModelProvider",
    "Role",
]

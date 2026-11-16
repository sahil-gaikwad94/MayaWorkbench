"""Provider-neutral model contracts."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    messages: list[ChatMessage]
    json_schema: dict[str, Any] | None = None
    schema_name: str = "response"
    max_output_tokens: int | None = None
    temperature: float | None = None


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_basis: str | None = None


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


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_output: bool
    streaming: bool = False
    max_context_tokens: int | None = None


class ModelProvider(Protocol):
    name: str
    model: str

    def capabilities(self) -> ModelCapabilities: ...

    def generate(self, request: ModelRequest) -> ModelResponse: ...


__all__ = [
    "ChatMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "ModelCapabilities",
    "ModelProvider",
    "Role",
]

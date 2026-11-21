"""Typed model capability interfaces and deterministic local fallbacks (WP7).

Five capabilities are separated behind narrow contracts:

* :class:`ChatModel` — extends the existing ``ModelProvider`` contract with an
  explicit structured-output flag and optional streaming.
* :class:`EmbeddingModel` — document and query vectors, with a deterministic
  hashing implementation that needs no provider at all.
* :class:`Reranker` — candidate ranking, with a deterministic lexical fallback.
* :class:`VisionModel` — an interface with an explicit ``unsupported``
  implementation. No visual capability is claimed until an adapter exists.
* :class:`ToolConnector` — an external-system adapter plus an in-process
  registry that is the policy authority for which operations may run.

The model is always a *proposer*. A structured model answer, a ranked list or a
suggested connector call is never executed on the model's word alone: the
workbench-owned registry/routing policy decides what is permitted. Secrets are
never part of any result, description or event produced here.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.errors import MayaError, ModelError
from mayaworkbench.models.base import ModelCapabilities, ModelProvider, ModelRequest, ModelResponse

#: Task names used by the routing policy. Defined here (not in ``domain/``) so
#: WP7 owns its own vocabulary without editing another work package's enums.
TaskType = Literal[
    "general",
    "repository_summarisation",
    "architecture_design",
    "routine_code_edit",
    "paper_reasoning",
    "research_search_synthesis",
    "code_review",
    "result_analysis",
    "embedding_retrieval",
]

TASK_TYPES: tuple[str, ...] = (
    "general",
    "repository_summarisation",
    "architecture_design",
    "routine_code_edit",
    "paper_reasoning",
    "research_search_synthesis",
    "code_review",
    "result_analysis",
    "embedding_retrieval",
)

_WORD_RE = re.compile(r"[^\W]+", re.UNICODE)


def terms(text: str) -> list[str]:
    """Deterministic, provider-free lexical tokens used by both fallbacks."""
    return [match.group(0).lower() for match in _WORD_RE.finditer(text or "")]


# --------------------------------------------------------------------------- #
# Chat                                                                         #
# --------------------------------------------------------------------------- #
class StreamChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    finished: bool = False


@runtime_checkable
class ChatModel(Protocol):
    """Text generation with structured output (``ModelProvider`` extended)."""

    name: str
    model: str

    def capabilities(self) -> ModelCapabilities: ...

    def generate(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class StreamingChatModel(ChatModel, Protocol):
    """A chat model that also emits incremental chunks.

    Streaming is opt-in: callers must check ``capabilities().streaming`` and
    use this protocol only when the adapter actually implements ``stream``.
    """

    def stream(self, request: ModelRequest) -> Iterator[StreamChunk]: ...


def supports_streaming(model: ChatModel) -> bool:
    """Report streaming only when the flag *and* the method are present."""
    return bool(model.capabilities().streaming) and callable(
        getattr(model, "stream", None)
    )


# --------------------------------------------------------------------------- #
# Embeddings                                                                   #
# --------------------------------------------------------------------------- #
@runtime_checkable
class EmbeddingModel(Protocol):
    name: str
    dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbeddingModel:
    """Deterministic local embedding fallback using signed feature hashing.

    This is a *lexical* vector, not a semantic embedding: no model, download or
    network is involved. It exists so retrieval works offline and so tests can
    assert exact, reproducible vectors. Providers can implement the same
    protocol later without changing callers.
    """

    name = "local_hashing_v1"
    mode = "LOCAL_DETERMINISTIC"

    def __init__(self, dimensions: int = 256):
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self.dimensions = int(dimensions)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in terms(text):
            first = int.from_bytes(
                hashlib.blake2b(token.encode("utf-8"), digest_size=8, person=b"maya-a").digest(),
                "big",
            )
            second = int.from_bytes(
                hashlib.blake2b(token.encode("utf-8"), digest_size=8, person=b"maya-b").digest(),
                "big",
            )
            sign = 1.0 if first & 1 else -1.0
            vector[first % self.dimensions] += sign
            vector[second % self.dimensions] -= sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [round(value / norm, 10) for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "dimensions": self.dimensions,
            "semantic": False,
            "note": "Deterministic lexical hashing; no provider, no network.",
        }


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity; zero for empty or mismatched vectors."""
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


# --------------------------------------------------------------------------- #
# Reranking                                                                    #
# --------------------------------------------------------------------------- #
class RankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    text: str
    score: float
    matched_terms: list[str] = Field(default_factory=list)


@runtime_checkable
class Reranker(Protocol):
    name: str

    def score(self, query: str, candidate: str) -> float: ...

    def rank(
        self, query: str, candidates: Sequence[str], *, top_k: int | None = None
    ) -> list[RankedCandidate]: ...


class LexicalOverlapReranker:
    """Deterministic overlap scorer used when no provider reranker is configured.

    Score is ``|query ∩ candidate| / sqrt(|query| * |candidate|)`` over lexical
    tokens. Ties break on the original index, so ordering is fully reproducible.
    """

    name = "local_lexical_overlap_v1"
    mode = "LOCAL_DETERMINISTIC"

    def score(self, query: str, candidate: str) -> float:
        query_terms = set(terms(query))
        candidate_terms = set(terms(candidate))
        if not query_terms or not candidate_terms:
            return 0.0
        return len(query_terms & candidate_terms) / math.sqrt(
            len(query_terms) * len(candidate_terms)
        )

    def rank(
        self, query: str, candidates: Sequence[str], *, top_k: int | None = None
    ) -> list[RankedCandidate]:
        query_terms = set(terms(query))
        ranked: list[RankedCandidate] = []
        for index, candidate in enumerate(candidates):
            candidate_terms = set(terms(candidate))
            ranked.append(
                RankedCandidate(
                    index=index,
                    text=candidate,
                    score=round(self.score(query, candidate), 10),
                    matched_terms=sorted(query_terms & candidate_terms),
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.index))
        if top_k is not None and top_k >= 0:
            return ranked[:top_k]
        return ranked

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "algorithm": "token-set overlap / sqrt(|q|*|c|)",
            "semantic": False,
        }


# --------------------------------------------------------------------------- #
# Vision                                                                       #
# --------------------------------------------------------------------------- #
class VisionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported: bool
    screenshots: bool = False
    diagrams: bool = False
    charts: bool = False


class VisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_ref: str
    question: str
    media_type: str | None = None


class VisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    findings: list[str] = Field(default_factory=list)
    provider: str
    model: str


@runtime_checkable
class VisionModel(Protocol):
    name: str

    def capabilities(self) -> VisionCapabilities: ...

    def inspect(self, request: VisionRequest) -> VisionResult: ...


class UnsupportedVisionModel:
    """Explicit no-op implementation: fail closed, never fabricate a description."""

    name = "unsupported"
    mode = "UNSUPPORTED"

    def capabilities(self) -> VisionCapabilities:
        return VisionCapabilities(
            supported=False, screenshots=False, diagrams=False, charts=False
        )

    def inspect(self, request: VisionRequest) -> VisionResult:
        raise ModelError(
            "No vision provider is configured; visual inspection is unsupported.",
            error_class="unsupported",
            retryable=False,
            hint=(
                "A real VisionModel adapter must be configured before screenshots, "
                "diagrams or charts can be interpreted. No result is invented."
            ),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "capabilities": self.capabilities().model_dump(mode="json"),
            "note": "Interface only; no provider adapter is shipped in WP7.",
        }


# --------------------------------------------------------------------------- #
# Connectors                                                                   #
# --------------------------------------------------------------------------- #
class ConnectorError(MayaError):
    """A connector operation was refused or is not available."""

    code = "CONNECTOR_ERROR"
    exit_code = 3


class ConnectorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str
    operation: str
    target: str
    data: Any
    request_hash: str
    response_hash: str
    retrieved_at: str
    read_only: bool = True
    policy_decision: str = "ALLOWED_BY_REGISTRY_ALLOWLIST"


@runtime_checkable
class ToolConnector(Protocol):
    name: str

    def operations(self) -> tuple[str, ...]: ...

    def supports(self, operation: str) -> bool: ...

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult: ...

    def revoke(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...


#: A gate is a policy hook: it may raise to refuse a proposed call. The model
#: never supplies or bypasses it.
PolicyGate = Callable[[str, str, dict[str, Any]], None]


class ConnectorRegistry:
    """In-process connector registry and the authority for connector calls."""

    def __init__(self, *, policy_gate: PolicyGate | None = None):
        self._connectors: dict[str, ToolConnector] = {}
        self._policy_gate = policy_gate

    def register(self, connector: ToolConnector) -> None:
        name = getattr(connector, "name", "")
        if not name or "/" in name:
            raise ConnectorError("A connector needs a simple non-empty name")
        if name in self._connectors:
            raise ConnectorError(f"Connector {name!r} is already registered")
        self._connectors[name] = connector

    def get(self, name: str) -> ToolConnector:
        connector = self._connectors.get(name)
        if connector is None:
            raise ConnectorError(
                f"Unknown connector {name!r}",
                hint="Run `maya connector list` to see registered connectors.",
            )
        return connector

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))

    def supports(self, name: str, operation: str) -> bool:
        try:
            return self.get(name).supports(operation)
        except ConnectorError:
            return False

    def invoke(
        self, name: str, operation: str, params: dict[str, Any] | None = None
    ) -> ConnectorResult:
        connector = self.get(name)
        payload = dict(params or {})
        # The gate is workbench policy, not model output. A model-proposed
        # operation that is absent from the connector allowlist is refused here.
        if not connector.supports(operation):
            raise ConnectorError(
                f"Connector {name!r} does not permit operation {operation!r}",
                hint=f"Allowed: {', '.join(connector.operations()) or '(none)'}",
            )
        if self._policy_gate is not None:
            self._policy_gate(name, operation, payload)
        result = connector.invoke(operation, payload)
        return result.model_copy(update={"policy_decision": "ALLOWED_BY_REGISTRY_ALLOWLIST"})

    def revoke(self, name: str) -> None:
        self.get(name).revoke()
        del self._connectors[name]

    def revoke_all(self) -> None:
        for connector in list(self._connectors.values()):
            connector.revoke()
        self._connectors.clear()

    def describe(self) -> list[dict[str, Any]]:
        return [self._connectors[name].describe() for name in self.names()]


__all__ = [
    "TaskType",
    "TASK_TYPES",
    "terms",
    "StreamChunk",
    "ChatModel",
    "StreamingChatModel",
    "supports_streaming",
    "EmbeddingModel",
    "HashingEmbeddingModel",
    "cosine",
    "RankedCandidate",
    "Reranker",
    "LexicalOverlapReranker",
    "VisionCapabilities",
    "VisionRequest",
    "VisionResult",
    "VisionModel",
    "UnsupportedVisionModel",
    "ConnectorError",
    "ConnectorResult",
    "ToolConnector",
    "PolicyGate",
    "ConnectorRegistry",
]

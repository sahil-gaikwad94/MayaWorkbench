"""Optional deterministic and pluggable retrieval adapters (WP5/6.2, P2).

Dependency-free defaults:

* ``HashingEmbeddingModel`` produces a deterministic hashed-token vector. It is
  lexical hashing, **not** a semantic embedding, and describes itself as such;
* ``LexicalReranker`` scores candidate spans by query-term coverage and exact
  identifier matches;
* ``ppr_scores`` implements damped Personalized PageRank over a symbol graph;
* ``reciprocal_rank_fusion`` deterministically fuses ranked channels.

Pluggable, opt-in providers:

* ``EmbeddingProvider`` is the common protocol;
* ``ApiEmbeddingProvider`` calls an OpenAI-compatible ``/embeddings`` endpoint
  with stdlib ``urllib`` and fails closed on any error. Its API key comes from
  an injected callable (or ``MAYA_MODEL_API_KEY``) and is never logged.

Everything here is **off by default**. The audit requires a measured gain over
the lexical/symbol baseline before any semantic mode becomes the default; see
``evaluate_hybrid`` and ``tests/test_semantic_retrieval.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from collections import defaultdict
from typing import Any, Callable, Iterable, Protocol, Sequence, runtime_checkable

_TOKEN_RE = re.compile(r"[A-Za-z_][\w-]{2,}")
DEFAULT_DIM = 256
DEFAULT_DAMPING = 0.85
STOPWORDS = frozenset({"the", "and", "for", "with", "that", "this", "from", "into", "when", "then"})


def tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in STOPWORDS}


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal contract for a pluggable embedding provider.

    ``mode`` and ``describe()`` are mandatory so callers can always tell a
    deterministic lexical hasher apart from a remote semantic model.
    """

    name: str
    mode: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...

    def describe(self) -> dict[str, Any]: ...


class HashingEmbeddingModel:
    """Deterministic *lexical* token hashing. Not a trained/semantic model."""

    name = "hashing-sha256-token"
    mode = "LOCAL_DETERMINISTIC"

    def __init__(self, dim: int = DEFAULT_DIM):
        if dim < 8:
            raise ValueError("embedding dimension must be at least 8")
        self.dim = dim

    @property
    def dimension(self) -> int:
        return self.dim

    def describe(self) -> dict[str, Any]:
        """Self-description that never claims semantic understanding."""
        return {
            "name": self.name,
            "mode": self.mode,
            "dimension": self.dimension,
            "semantic": False,
            "kind": "lexical_hashing",
            "note": "SHA-256 token hashing; lexical overlap only, not semantic meaning.",
        }

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % self.dim
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    def similarity(self, left: str, right: str) -> float:
        a, b = self.embed(left), self.embed(right)
        return round(sum(x * y for x, y in zip(a, b)), 6)


class LexicalReranker:
    """Bounded, deterministic reranking by query coverage and exact identifiers."""

    def __init__(self, model: HashingEmbeddingModel | None = None):
        self.model = model or HashingEmbeddingModel()

    def score(self, query: str, candidate: str, base_score: float = 0.0) -> float:
        q, c = tokens(query), tokens(candidate)
        if not q:
            return base_score
        coverage = len(q & c) / len(q)
        exact = 0.25 if any(token in c for token in q) else 0.0
        semantic = max(0.0, self.model.similarity(query, candidate))
        return round(base_score + 0.5 * coverage + exact + 0.25 * semantic, 6)

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, limit: int = 10
    ) -> list[dict[str, Any]]:
        scored = []
        for item in candidates:
            text = f"{item.get('name', '')} {item.get('path', '')} {item.get('reason', '')}"
            new = self.score(query, text, float(item.get("score", 0.0)))
            scored.append({**item, "score": new, "rerank_reason": "lexical+hash-coverage"})
        scored.sort(key=lambda item: (-item["score"], str(item.get("path", ""))))
        return scored[:limit]


def ppr_scores(
    graph: dict[str, Iterable[str]],
    seeds: Iterable[str],
    *,
    damping: float = DEFAULT_DAMPING,
    iterations: int = 20,
) -> dict[str, float]:
    """Damped Personalized PageRank over an adjacency mapping."""
    if not 0.0 < damping < 1.0:
        raise ValueError("damping must be between 0 and 1")
    nodes = set(graph)
    for neighbours in graph.values():
        nodes.update(neighbours)
    if not nodes:
        return {}
    seed_set = [seed for seed in seeds if seed in nodes]
    if not seed_set:
        return {}
    scores = {node: 0.0 for node in nodes}
    for seed in seed_set:
        scores[seed] = 1.0 / len(seed_set)
    for _ in range(max(1, iterations)):
        # Restart distributes (1-d); transition distributes d. Their masses
        # already sum to one, so no extra uniform term is added.
        nxt = {node: 0.0 for node in nodes}
        restart = (1 - damping) / len(seed_set)
        for seed in seed_set:
            nxt[seed] += restart
        for source, neighbours in graph.items():
            targets = list(neighbours)
            if not targets:
                continue
            share = damping * scores.get(source, 0.0) / len(targets)
            for target in targets:
                nxt[target] = nxt.get(target, 0.0) + share
        scores = nxt
    return {node: round(score, 6) for node, score in sorted(scores.items())}


class ApiEmbeddingError(RuntimeError):
    """Raised when the optional API embedding provider cannot return vectors."""


def _redact(message: str, secret: str) -> str:
    return message.replace(secret, "[REDACTED]") if secret else message


class ApiEmbeddingProvider:
    """OpenAI-compatible ``/embeddings`` client using only the stdlib.

    The API key is obtained from ``api_key`` (an injected callable) or, as a
    fallback, from ``MAYA_MODEL_API_KEY`` at call time. It is never logged,
    stored on the instance, or included in an error message. Any failure raises
    :class:`ApiEmbeddingError` so the caller can fall back to BM25 explicitly.
    """

    mode = "REMOTE_API"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: Callable[[], str] | None = None,
        model: str = "text-embedding-3-small",
        dimension: int | None = None,
        timeout: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint must be a non-empty /embeddings URL")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if dimension is not None and dimension < 1:
            raise ValueError("embedding dimension must be positive")
        self.endpoint = endpoint
        self.model = model
        self.name = f"api:{model}"
        self.timeout = float(timeout)
        self._dimension = dimension
        self._inferred_dimension: int | None = None
        self._response_model: str | None = None
        self._key_provider = api_key or (lambda: os.environ.get("MAYA_MODEL_API_KEY", ""))
        self._opener = opener or urllib.request.urlopen

    @property
    def dimension(self) -> int:
        return self._dimension or self._inferred_dimension or 0

    def _key(self) -> str:
        key = self._key_provider()
        if not isinstance(key, str) or not key.strip():
            raise ApiEmbeddingError("embedding API key is not configured")
        return key.strip()

    def _post(self, inputs: list[str]) -> list[list[float]]:
        key = self._key()
        payload: dict[str, Any] = {"input": list(inputs), "model": self.model}
        if self._dimension is not None:
            payload["dimensions"] = self._dimension
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            data = json.loads(raw)
            rows = data.get("data")
            if not isinstance(rows, list) or not rows:
                raise ValueError("response contained no embedding data")
            ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
            vectors: list[list[float]] = []
            for row in ordered:
                vector = row.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise ValueError("response contained an empty embedding")
                values = [float(value) for value in vector]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("response contained a non-finite embedding value")
                vectors.append(values)
            dimensions = {len(vector) for vector in vectors}
            if len(dimensions) != 1:
                raise ValueError("response contained inconsistent embedding dimensions")
            dimension = dimensions.pop()
            if self._dimension is not None and dimension != self._dimension:
                raise ValueError(
                    f"response dimension {dimension} does not match requested {self._dimension}"
                )
            self._inferred_dimension = dimension
            response_model = data.get("model")
            if isinstance(response_model, str):
                self._response_model = response_model
            return vectors
        except Exception as exc:
            # Fail closed. The redacted message is the only thing surfaced; the
            # original exception (which may echo the Authorization header) is
            # deliberately not chained.
            raise ApiEmbeddingError(
                f"embedding API call failed for {self.model}: {_redact(str(exc), key)}"
            ) from None

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        inputs = list(texts)
        if not inputs:
            return []
        return self._post(inputs)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "model": self.model,
            "response_model": self._response_model,
            "semantic": True,
            "dimension": self.dimension,
            "dimension_known": self.dimension > 0,
            "api_version": "openai-compatible:/embeddings",
            "note": "Remote embeddings; API key is injected and never logged.",
        }


def reciprocal_rank_fusion(
    rankings: dict[str, Sequence[str]],
    *,
    weights: dict[str, float] | None = None,
    k: int = 60,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Deterministic weighted RRF over named ranked channels.

    Each channel is an ordered list of document ids. The result records the
    exact channels that contributed to every document, so a caller can tell a
    BM25-only ranking from one boosted by graph or dense channels. Empty or
    zero-weight channels are ignored.
    """
    if k <= 0:
        raise ValueError("RRF k must be positive")
    weight_map = weights or {}
    scores: dict[str, float] = {}
    details: dict[str, dict[str, Any]] = {}
    for channel, ranked in rankings.items():
        weight = float(weight_map.get(channel, 1.0))
        if weight == 0.0:
            continue
        seen: set[str] = set()
        for rank, item in enumerate(ranked, start=1):
            doc_id = str(item)
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
            detail = details.setdefault(doc_id, {"channels": set(), "ranks": {}})
            detail["channels"].add(channel)
            detail["ranks"][channel] = rank
    fused = []
    for doc_id, score in scores.items():
        detail = details[doc_id]
        fused.append(
            {
                "id": doc_id,
                "score": round(score, 9),
                "channels": sorted(detail["channels"]),
                "ranks": dict(sorted(detail["ranks"].items())),
            }
        )
    fused.sort(key=lambda row: (-row["score"], row["id"]))
    if limit is not None:
        fused = fused[: max(0, limit)]
    return fused


class HybridRetriever:
    """Opt-in fusion of BM25, graph PPR and optional dense ranked channels.

    Dense ranks may be supplied directly (offline/testable) or computed from a
    pluggable :class:`EmbeddingProvider` via ``dense_ranks``. A missing dense
    channel is simply absent from the fusion, so BM25+PPR ordering is preserved.
    """

    def __init__(
        self,
        *,
        dense_provider: EmbeddingProvider | None = None,
        weights: dict[str, float] | None = None,
        k: int = 60,
    ) -> None:
        if k <= 0:
            raise ValueError("RRF k must be positive")
        self.dense_provider = dense_provider
        self.k = k
        self.weights: dict[str, float] = {"bm25": 1.0, "ppr": 1.0, "dense": 1.0}
        self.weights.update(weights or {})

    def embed_query(self, query: str) -> list[float] | None:
        if self.dense_provider is None:
            return None
        return self.dense_provider.embed(query)

    def dense_ranks(self, query: str, documents: dict[str, str]) -> list[str] | None:
        """Rank ``documents`` by cosine similarity to ``query`` using the provider."""
        if self.dense_provider is None or not documents:
            return None
        query_vector = self.dense_provider.embed(query)
        scored = []
        for doc_id, text in documents.items():
            vector = self.dense_provider.embed(text)
            score = _cosine(query_vector, vector)
            scored.append((doc_id, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [doc_id for doc_id, _ in scored]

    def fuse(
        self,
        *,
        bm25: Sequence[str] = (),
        ppr: Sequence[str] = (),
        dense: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        rankings: dict[str, Sequence[str]] = {"bm25": list(bm25), "ppr": list(ppr)}
        if dense is not None:
            rankings["dense"] = list(dense)
        return reciprocal_rank_fusion(rankings, weights=self.weights, k=self.k)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def evaluate_hybrid(
    query_cases: list[dict[str, Any]],
    *,
    reranker: LexicalReranker | None = None,
    k: int = 5,
) -> dict[str, Any]:
    """Compare baseline vs reranked Hit@k on a small, explicit gold set.

    Every case supplies ``query``, ``candidates`` and ``relevant`` (a candidate
    name). No benchmark claim is made beyond this fixed corpus.
    """
    ranker = reranker or LexicalReranker()
    baseline_hits = 0
    hybrid_hits = 0
    for case in query_cases:
        query = str(case.get("query", ""))
        relevant = str(case.get("relevant", ""))
        candidates = list(case.get("candidates", []))
        baseline_names = [str(item.get("name", "")) for item in candidates[:k]]
        ranked = ranker.rerank(query, candidates, limit=k)
        hybrid_names = [str(item.get("name", "")) for item in ranked]
        baseline_hits += int(relevant in baseline_names)
        hybrid_hits += int(relevant in hybrid_names)
    total = max(1, len(query_cases))
    return {
        "cases": len(query_cases),
        "k": k,
        "baseline_hit_at_k": round(baseline_hits / total, 3),
        "hybrid_hit_at_k": round(hybrid_hits / total, 3),
        "improvement": round((hybrid_hits - baseline_hits) / total, 3),
        "note": "Fixed local corpus only; not an external benchmark claim.",
    }


__all__ = [
    "ApiEmbeddingError",
    "ApiEmbeddingProvider",
    "EmbeddingProvider",
    "HashingEmbeddingModel",
    "HybridRetriever",
    "LexicalReranker",
    "ppr_scores",
    "reciprocal_rank_fusion",
    "evaluate_hybrid",
    "tokens",
    "DEFAULT_DAMPING",
]

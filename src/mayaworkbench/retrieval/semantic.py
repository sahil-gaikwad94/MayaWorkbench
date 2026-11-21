"""Optional deterministic semantic adapters (WP5/6.2).

These adapters are intentionally dependency-free:

* ``HashingEmbeddingModel`` produces a deterministic character-n-gram vector;
* ``LexicalReranker`` scores candidate spans by query-term coverage and exact
  identifier matches;
* ``ppr_scores`` implements damped Personalized PageRank over a symbol graph.

They are **off by default**. The audit and v2.1 require a measured gain over the
lexical/symbol baseline before any semantic mode becomes the default; see
``evaluate_hybrid`` and ``tests/test_semantic_retrieval.py``.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from typing import Any, Iterable

_TOKEN_RE = re.compile(r"[A-Za-z_][\w-]{2,}")
DEFAULT_DIM = 256
DEFAULT_DAMPING = 0.85
STOPWORDS = frozenset({"the", "and", "for", "with", "that", "this", "from", "into", "when", "then"})


def tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in STOPWORDS}


class HashingEmbeddingModel:
    """Deterministic, reproducible hashing embeddings (not a trained model)."""

    def __init__(self, dim: int = DEFAULT_DIM):
        if dim < 8:
            raise ValueError("embedding dimension must be at least 8")
        self.dim = dim

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
    "HashingEmbeddingModel",
    "LexicalReranker",
    "ppr_scores",
    "evaluate_hybrid",
    "tokens",
    "DEFAULT_DAMPING",
]

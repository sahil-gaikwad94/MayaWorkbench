"""Retrieval quality evaluation with an explicit labelled relevance set.

Search scores are retrieval hints, not truth. This harness compares the default
BM25/exact baseline with the optional semantic and PPR modes on the same
labelled queries, reporting precision@k, recall@k and MRR. A mode is only
credited when it actually improves the measure; no uplift is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = "retrieval-eval/v1"


@dataclass(frozen=True)
class RelevanceCase:
    query: str
    relevant_paths: tuple[str, ...]
    language: str = "python"

    def __post_init__(self) -> None:
        if not self.query or not self.relevant_paths:
            raise ValueError("a relevance case needs a query and at least one relevant path")


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top = list(retrieved)[:k]
    return sum(1 for item in top if item in relevant) / k


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = list(retrieved)[:k]
    return sum(1 for item in top if item in relevant) / len(relevant)


def reciprocal_rank(retrieved: Sequence[str], relevant: set[str]) -> float:
    for index, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def evaluate_retrieval(
    cases: Iterable[RelevanceCase],
    *,
    retrieve: Callable[[str, int], Sequence[str]],
    modes: Sequence[str] = ("baseline", "semantic", "ppr"),
    k: int = 5,
    retrievers: dict[str, Callable[[str, int], Sequence[str]]] | None = None,
) -> dict[str, Any]:
    """Evaluate each mode on identical queries; never invent an uplift."""
    rows = list(cases)
    results: dict[str, Any] = {}
    for mode in modes:
        per_query: list[dict[str, Any]] = []
        mode_retrieve = (retrievers or {}).get(mode, retrieve)
        for case in rows:
            retrieved = list(mode_retrieve(case.query, k) or [])
            relevant = set(case.relevant_paths)
            per_query.append({
                "query": case.query,
                "retrieved": retrieved,
                "relevant": sorted(relevant),
                "precision_at_k": round(precision_at_k(retrieved, relevant, k), 4),
                "recall_at_k": round(recall_at_k(retrieved, relevant, k), 4),
                "mrr": round(reciprocal_rank(retrieved, relevant), 4),
            })
        results[mode] = {
            "precision_at_k": round(
                sum(item["precision_at_k"] for item in per_query) / len(per_query), 4
            ) if per_query else 0.0,
            "recall_at_k": round(
                sum(item["recall_at_k"] for item in per_query) / len(per_query), 4
            ) if per_query else 0.0,
            "mrr": round(sum(item["mrr"] for item in per_query) / len(per_query), 4)
            if per_query else 0.0,
            "queries": per_query,
        }
    base = results.get("baseline", {})
    for mode, payload in results.items():
        if mode == "baseline" or not base:
            continue
        payload["uplift_precision_at_k"] = round(
            payload["precision_at_k"] - base.get("precision_at_k", 0.0), 4
        )
        payload["uplift_mrr"] = round(payload["mrr"] - base.get("mrr", 0.0), 4)
    return {
        "schema_version": SCHEMA_VERSION,
        "k": k,
        "cases": len(rows),
        "modes": results,
        "note": (
            "Uplift is measured, never assumed. Negative uplift is reported "
            "as-is; a mode that does not help should not be enabled by default."
        ),
    }


__all__ = [
    "SCHEMA_VERSION", "RelevanceCase", "evaluate_retrieval",
    "precision_at_k", "recall_at_k", "reciprocal_rank",
]

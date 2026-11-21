"""`RepoIntelligenceEngine` — a facade over the real repository services.

The v2.0 blueprint named this class but it never existed. This facade groups
the already-shipped pinned snapshot index, lexical/symbol search and retrieval
behind one object. It deliberately does not claim PPR or embeddings unless those
optional modes are explicitly requested and measured.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.indexing.indexer import Indexer, IndexReport
from mayaworkbench.retrieval.models import RetrievalBundle
from mayaworkbench.retrieval.service import RetrievalService
from mayaworkbench.retrieval.search import SearchService


class RepoIntelligenceEngine:
    def __init__(
        self,
        *,
        indexer: Indexer,
        search: SearchService,
        retrieval: RetrievalService,
    ):
        self.indexer = indexer
        self.search = search
        self.retrieval = retrieval

    def index(self, snapshot_id: str) -> IndexReport:
        return self.indexer.index(snapshot_id)

    def search_text(
        self, snapshot_id: str, query: str, *, mode: str = "auto", limit: int = 25
    ) -> list[dict[str, Any]]:
        hits, _expression = self.search.search_text(
            snapshot_id, query, mode=mode, limit=limit  # type: ignore[arg-type]
        )
        return [hit.model_dump(mode="json") for hit in hits]

    def retrieve(
        self,
        snapshot_id: str,
        question: str,
        *,
        budget: int | None = None,
        selection: str = "greedy",
        use_semantic: bool = False,
        use_ppr: bool = False,
    ) -> RetrievalBundle:
        return self.retrieval.retrieve(
            snapshot_id,
            question,
            budget=budget,
            selection=selection,  # type: ignore[arg-type]
            use_semantic=use_semantic,
            use_ppr=use_ppr,
        )


__all__ = ["RepoIntelligenceEngine"]

"""Research intelligence: document ingestion, passages, claims, memos.

Modules:

* :mod:`mayaworkbench.research.pdf` — PDF text extraction and its limits;
* :mod:`mayaworkbench.research.fetch` — guarded URL/DOI retrieval;
* :mod:`mayaworkbench.research.metadata` — bibliographic metadata provenance;
* :mod:`mayaworkbench.research.passages` — structure-aware segmentation;
* :mod:`mayaworkbench.research.ingestion` — document versions and artifacts;
* :mod:`mayaworkbench.research.claims` — extraction, authoring and review;
* :mod:`mayaworkbench.research.dag` — claim DAG validation;
* :mod:`mayaworkbench.research.readiness` — reproduction risk assessment;
* :mod:`mayaworkbench.research.memo` — versioned research memo generation.

Submodules are imported lazily (``import mayaworkbench.research.memo``) so the
mapping package can depend on ``research.models`` without a cycle through
``research.memo``.
"""

__all__ = [
    "claims",
    "dag",
    "fetch",
    "ingestion",
    "memo",
    "metadata",
    "models",
    "passages",
    "pdf",
    "prompts",
    "readiness",
]

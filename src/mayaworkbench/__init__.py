"""MayaWorkbench — an evidence-first research-engineering workbench.

Phase 1 surface: project/repository registration, pinned snapshots, repository
indexing, hybrid retrieval, and citation-validated repository Q&A.

The package is a modular monolith (see ``docs/architecture.md``). Domain models
and contracts live in :mod:`mayaworkbench.domain` and must not import the CLI,
HTTP, or provider SDK layers.
"""

from mayaworkbench.version import (
    CONTRACT_VERSION,
    INDEXER_VERSION,
    MAYA_VERSION,
)

__version__ = MAYA_VERSION

__all__ = ["MAYA_VERSION", "INDEXER_VERSION", "CONTRACT_VERSION", "__version__"]

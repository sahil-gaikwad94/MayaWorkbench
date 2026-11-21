"""`PaperToCodeMatrixEngine` — a facade over the real claim-to-code services.

The v2.0 blueprint named this class but it never existed. This facade does not
re-implement mapping: it delegates to `ClaimMappingService` and `ClaimService`,
preserving the mechanical `PRESENT` rule and verified evidence spans.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.domain.models import Project, RepositorySnapshot
from mayaworkbench.mapping.service import ClaimMappingService
from mayaworkbench.research.claims import ClaimService


class PaperToCodeMatrixEngine:
    def __init__(self, *, mapping: ClaimMappingService, claims: ClaimService):
        self.mapping = mapping
        self.claims = claims

    def build_matrix(
        self, project: Project, snapshot: RepositorySnapshot
    ) -> dict[str, Any]:
        matrix = self.mapping.build_matrix(project, snapshot)
        return matrix.model_dump(mode="json")

    def claim_types(self) -> list[str]:
        from mayaworkbench.domain.enums import ClaimKind

        return [item.value for item in ClaimKind]


__all__ = ["PaperToCodeMatrixEngine"]

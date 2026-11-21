"""Data contracts for the persistent skills system."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.models import Record


class Skill(Record):
    """A versioned, reusable procedure or checker."""

    id: str
    project_id: str
    key: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="procedure", max_length=60)
    title: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=4000)
    body: str = Field(default="", max_length=100_000)
    scope: str = Field(default="project", pattern=r"^(project|user)$")
    enabled: bool = True
    details: dict[str, object] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())

    @property
    def effective_key(self) -> str:
        return f"{self.key}@v{self.version}"


class SkillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    status: str  # PROCESSED | SKIPPED | FAILED
    output: str
    validation: list[str] = Field(default_factory=list)
    duration_ms: int = 0


__all__ = ["Skill", "SkillResult"]

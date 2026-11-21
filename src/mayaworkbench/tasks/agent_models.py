"""Structured model contracts for planning and one agent step.

These schemas are what the model gateway validates. They are deliberately
narrow: a model can propose one typed action at a time and cannot express a
shell string, a path policy override, an approval grant or a workflow state
change. Those are application authority, not model output.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from mayaworkbench.domain.models import PlanStep, Record

AgentTool = Literal[
    "read_file",
    "list_files",
    "search_text",
    "find_symbol",
    "apply_patch",
    "write_file",
    "delete_file",
    "git_status",
    "git_diff",
    "run_command",
    "run_check",
    "finish",
]


class PlanPayload(Record):
    """Output of the Technical Planner role."""

    summary: str = Field(min_length=1, max_length=8000)
    steps: list[PlanStep] = Field(min_length=1, max_length=50)
    files: list[str] = Field(default_factory=list, max_length=100)
    checks: list[str] = Field(default_factory=list, max_length=50)
    risks: list[str] = Field(default_factory=list, max_length=30)
    unknowns: list[str] = Field(default_factory=list, max_length=30)
    estimated_steps: int = Field(default=1, ge=1, le=200)


class AgentAction(Record):
    """One bounded step proposed by the Implementer role."""

    thought: str = Field(min_length=1, max_length=4000)
    tool: AgentTool
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def _arguments_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20:
            raise ValueError("tool arguments have too many keys")
        return value


class FinishArgs(Record):
    summary: str = Field(min_length=1, max_length=8000)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    checks_run: list[str] = Field(default_factory=list, max_length=30)
    remaining_work: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("limitations", "checks_run", "remaining_work")
    @classmethod
    def _item_length(cls, values: list[str]) -> list[str]:
        if any(len(item) > 2000 for item in values):
            raise ValueError("finish list items must be at most 2000 characters")
        return values


__all__ = ["PlanPayload", "AgentAction", "FinishArgs", "AgentTool"]

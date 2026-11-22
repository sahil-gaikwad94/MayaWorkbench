"""Versioned task corpus for the opt-in real-model benchmark harness.

A corpus is plain JSON validated into strict :class:`EvalTask` records. Every
field is explicit: an unlabelled or ambiguous task is refused rather than scored.
The bundled corpus is an **internal seed** built from this repository's own
deterministic fixtures; it is a harness self-test, never an external benchmark.

The corpus hash is computed over canonical JSON (sorted object keys, compact
separators, tasks sorted by id) so the same corpus always produces the same
hash regardless of file formatting or task order.
"""

from __future__ import annotations

import hashlib
import importlib.resources as resources
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mayaworkbench.domain.enums import TaskDecision

INTERNAL_SEED_PROVENANCE = (
    "Internal seed corpus authored for the MayaWorkbench benchmark harness "
    "self-test from this repository's deterministic fixtures; not an external benchmark."
)

KNOWN_FAMILIES = frozenset(
    {"bug_fix", "build_repair", "paper_to_code", "adversarial", "eval_gaming"}
)

_DECISION_CLASSES = frozenset(item.value for item in TaskDecision)

_SEED_PACKAGE = "mayaworkbench.evaluation"
_SEED_RESOURCE = "gold/eval_tasks_seed.json"


class EvalTask(BaseModel):
    """One labelled benchmark task. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    fixture_ref: str = Field(min_length=1)
    required_capabilities: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    expected_decision_class: str = Field(min_length=1)
    anti_gaming_probe: bool = False
    max_steps: int = Field(default=8, ge=1, le=200)
    max_model_tokens: int = Field(default=20_000, ge=1, le=2_000_000)
    max_spend_usd: float = Field(default=0.10, gt=0)
    provenance: str = Field(default=INTERNAL_SEED_PROVENANCE, min_length=1)
    is_seed_corpus: bool = True

    @field_validator("id", "objective", "fixture_ref")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("family")
    @classmethod
    def _known_family(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in KNOWN_FAMILIES:
            raise ValueError(
                f"unknown task family {value!r}; expected one of {sorted(KNOWN_FAMILIES)}"
            )
        return normalized

    @field_validator("expected_decision_class")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _DECISION_CLASSES:
            raise ValueError(
                f"unknown expected decision class {value!r}; "
                f"expected one of {sorted(_DECISION_CLASSES)}"
            )
        return normalized

    @field_validator("acceptance_criteria", "required_capabilities", "required_checks")
    @classmethod
    def _non_empty_items(cls, value: list[str]) -> list[str]:
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError("list entries must be non-blank strings")
        return [item.strip() for item in value]


def _validate_tasks(tasks: Sequence[EvalTask], *, source: str) -> list[EvalTask]:
    result = list(tasks)
    if not result:
        raise ValueError(f"corpus {source} is empty")
    seen: dict[str, int] = {}
    for index, task in enumerate(result):
        if task.id in seen:
            raise ValueError(
                f"corpus {source} has duplicate task id {task.id!r} "
                f"(positions {seen[task.id]} and {index})"
            )
        seen[task.id] = index
    return result


def _parse_payload(payload: Any, *, source: str) -> list[EvalTask]:
    if isinstance(payload, dict) and "tasks" in payload:
        payload = payload["tasks"]
    if not isinstance(payload, list):
        raise ValueError(f"corpus {source} must be a list of tasks or an object with 'tasks'")
    tasks: list[EvalTask] = []
    for index, item in enumerate(payload):
        try:
            tasks.append(EvalTask.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"task #{index} in corpus {source} has an invalid schema: {exc}") from exc
    return _validate_tasks(tasks, source=source)


def load_corpus(path: str | Path) -> list[EvalTask]:
    """Load and validate a task corpus from a JSON file or a directory of JSON files."""
    target = Path(path).expanduser()
    if target.is_dir():
        files = sorted(target.glob("*.json"))
        if not files:
            raise ValueError(f"corpus directory {target} contains no .json files")
        tasks: list[EvalTask] = []
        for file in files:
            tasks.extend(_parse_payload(_read_payload(file), source=str(file)))
        return _validate_tasks(tasks, source=str(target))
    return _parse_payload(_read_payload(target), source=str(target))


def _read_payload(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read task corpus {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"task corpus {path} is not valid JSON: {exc}") from exc


def bundled_seed_corpus() -> list[EvalTask]:
    """Load the checked-in internal seed corpus through importlib.resources."""
    try:
        text = resources.files(_SEED_PACKAGE).joinpath("gold").joinpath("eval_tasks_seed.json").read_text(
            encoding="utf-8"
        )
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        raise ValueError(
            f"bundled seed corpus {_SEED_RESOURCE} is unavailable; reinstall mayaworkbench"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bundled seed corpus {_SEED_RESOURCE} is not valid JSON: {exc}") from exc
    return _parse_payload(payload, source=_SEED_RESOURCE)


def corpus_hash(corpus: Sequence[EvalTask]) -> str:
    """Stable SHA-256 over the canonical JSON representation of a corpus."""
    canonical = [task.model_dump(mode="json") for task in sorted(corpus, key=lambda item: item.id)]
    encoded = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "INTERNAL_SEED_PROVENANCE",
    "KNOWN_FAMILIES",
    "EvalTask",
    "load_corpus",
    "bundled_seed_corpus",
    "corpus_hash",
]

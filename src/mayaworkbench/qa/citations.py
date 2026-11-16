"""Citation validation.

A citation is accepted only if all of the following hold against the retrieved
context for the pinned snapshot:

1. the path exists in the context;
2. the cited line range lies entirely inside one retrieved span for that path;
3. the quoted text, normalised for whitespace, appears inside the cited range.

This is deterministic code, not a model judgement. It converts "the model said
it read this" into "this exact text exists at this exact location in this
snapshot".
"""

from __future__ import annotations

import re

from mayaworkbench.domain.models import Citation
from mayaworkbench.retrieval.models import ContextPack

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _span_text(item_text: str, start_line: int, end_line: int) -> str | None:
    """Extract the sub-span of a rendered context item."""
    selected: list[str] = []
    for line in item_text.splitlines():
        prefix, sep, remainder = line.partition(": ")
        if not sep:
            continue
        try:
            line_no = int(prefix)
        except ValueError:
            continue
        if start_line <= line_no <= end_line:
            selected.append(remainder)
    return "\n".join(selected) if selected else None


def validate_citations(citations: list[Citation], context: ContextPack) -> list[Citation]:
    items_by_path: dict[str, list] = {}
    for item in context.items:
        items_by_path.setdefault(item.path, []).append(item)

    validated: list[Citation] = []
    for citation in citations:
        path = citation.path.strip().lstrip("./")
        items = items_by_path.get(path)
        if not items:
            validated.append(
                citation.model_copy(
                    update={
                        "valid": False,
                        "validation": "path was not part of the retrieved context",
                    }
                )
            )
            continue
        containing = next(
            (
                item
                for item in items
                if item.start_line <= citation.start_line and citation.end_line <= item.end_line
            ),
            None,
        )
        if containing is None:
            validated.append(
                citation.model_copy(
                    update={
                        "valid": False,
                        "validation": (
                            "line range is outside the retrieved spans for this path "
                            f"(retrieved: {', '.join(f'{i.start_line}-{i.end_line}' for i in items)})"
                        ),
                    }
                )
            )
            continue
        span_text = _span_text(containing.text, citation.start_line, citation.end_line)
        if span_text is None:
            validated.append(
                citation.model_copy(
                    update={"valid": False, "validation": "cited lines are not present in context"}
                )
            )
            continue
        quote = _normalise(citation.quote)
        if not quote:
            validated.append(
                citation.model_copy(
                    update={"valid": False, "validation": "no quote supplied for the cited span"}
                )
            )
            continue
        if quote not in _normalise(span_text):
            validated.append(
                citation.model_copy(
                    update={
                        "valid": False,
                        "validation": "quoted text does not appear in the cited lines",
                    }
                )
            )
            continue
        validated.append(citation.model_copy(update={"valid": True, "validation": "verified"}))
    return validated


def citation_summary(citations: list[Citation]) -> tuple[int, int]:
    valid = sum(1 for citation in citations if citation.valid)
    return valid, len(citations) - valid


__all__ = ["validate_citations", "citation_summary"]

"""Bounded equation / algorithm extraction with exact source spans.

This is a deterministic *detection* aid, not a parser of mathematics. Every
result carries the exact character range it came from so a claim can cite an
equation the same way it cites a sentence. Ambiguous or malformed candidates
are labelled `LOW` confidence and never promoted to a verified requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_EQUATIONS = 64
MAX_EQUATION_CHARS = 600

_LATEX_BLOCK = re.compile(r"\\begin\{(equation|align|eqnarray)\*?\}.*?\\end\{\1\*?\}", re.S)
_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE_MATH = re.compile(r"\$([^$\n]{1,200})\$")
_EQUATION_LINE = re.compile(
    r"^(?P<body>.{1,200}?(?:=|\\approx|\\propto|\\leq|\\geq|<=|>=).{1,400})$"
)
_KNOWN_OPERATORS = (
    "softmax", "argmax", "sum", "exp", "log", "norm", "loss", "gradient",
    "attention", "mask", "clip", "normalize", "adam", "uniform",
)


@dataclass(frozen=True)
class EquationSpan:
    text: str
    char_start: int
    char_end: int
    kind: str  # EQUATION | ALGORITHM | CONSTRAINT
    confidence: str  # HIGH | MEDIUM | LOW
    operators: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "kind": self.kind,
            "confidence": self.confidence,
            "operators": list(self.operators),
        }


def _operators(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(sorted(name for name in _KNOWN_OPERATORS if name in lowered))


def _classify(text: str) -> tuple[str, str]:
    lowered = text.lower()
    if re.search(r"\\begin\{|\\frac|\\sum|\$\$", text):
        return "EQUATION", "HIGH"
    if any(word in lowered for word in ("algorithm", "procedure", "step 1", "repeat until")):
        return "ALGORITHM", "MEDIUM"
    if any(word in lowered for word in ("subject to", "such that", "constraint", "must satisfy")):
        return "CONSTRAINT", "MEDIUM"
    if _operators(text):
        return "EQUATION", "LOW"
    return "EQUATION", "LOW"


def extract_equations(text: str, *, max_equations: int = MAX_EQUATIONS) -> list[EquationSpan]:
    """Return non-overlapping equation spans with exact character offsets."""
    if not isinstance(text, str) or not text:
        return []
    spans: list[EquationSpan] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < existing_end and existing_start < end for existing_start, existing_end in claimed)

    for pattern in (_LATEX_BLOCK, _DISPLAY_MATH):
        for match in pattern.finditer(text):
            body = match.group(0).strip()
            if not body or len(body) > MAX_EQUATION_CHARS or overlaps(*match.span()):
                continue
            kind, confidence = _classify(body)
            spans.append(EquationSpan(body, match.start(), match.end(), kind, confidence, _operators(body)))
            claimed.append(match.span())

    for number, line in enumerate(text.splitlines(keepends=True)):
        if len(spans) >= max_equations:
            break
        stripped = line.strip()
        if not stripped or len(stripped) > MAX_EQUATION_CHARS:
            continue
        lowered = stripped.lower()
        is_constraint = any(
            term in lowered for term in ("subject to", "such that", "must satisfy", "must equal")
        )
        match = _EQUATION_LINE.match(stripped)
        if not match and not is_constraint:
            continue
        # Compute absolute offsets robustly even for repeated lines.
        start = 0
        for previous in range(number):
            start += len(text.splitlines(keepends=True)[previous])
        absolute = start + (len(line) - len(line.lstrip()))
        end = absolute + len(stripped)
        if overlaps(absolute, end):
            continue
        kind, confidence = _classify(stripped)
        spans.append(EquationSpan(stripped, absolute, end, kind, confidence, _operators(stripped)))
        claimed.append((absolute, end))

    spans.sort(key=lambda item: item.char_start)
    return spans[:max_equations]


def equations_to_claim_hints(spans: list[EquationSpan]) -> list[dict]:
    """Convert spans into advisory claim hints; never a verdict."""
    return [
        {
            "text": item.text,
            "source_span": {"char_start": item.char_start, "char_end": item.char_end},
            "kind": item.kind,
            "confidence": item.confidence,
            "note": "detected equation; confirm the exact meaning against the paper text",
        }
        for item in spans
    ]


__all__ = ["EquationSpan", "extract_equations", "equations_to_claim_hints"]

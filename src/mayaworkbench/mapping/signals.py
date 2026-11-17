"""Behavioural signal matching.

A *signal* is a short, machine-checkable string that should appear in code if a
claim's requirement is implemented (``triu``, ``masked_fill``, ``causal_mask``).
Signals are either declared by a claim's author or derived from the claim text.

Matching is deliberately strict: a signal matches as a whole token (or a
whole multi-word phrase) with case-insensitive separators, so ``mask`` does not
silently match ``masked_fill``. Each match records whether it occurred in
executable code, a comment, a docstring or a string literal, because a signal
that only appears in prose is not implementation evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "do",
    "does",
    "each",
    "either",
    "for",
    "from",
    "has",
    "have",
    "having",
    "here",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "more",
    "most",
    "must",
    "no",
    "not",
    "of",
    "on",
    "only",
    "or",
    "other",
    "our",
    "out",
    "over",
    "same",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "under",
    "until",
    "up",
    "use",
    "used",
    "using",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "will",
    "with",
    "would",
    "you",
    # Generic research words that carry no implementation meaning on their own.
    "paper",
    "method",
    "methods",
    "approach",
    "section",
    "figure",
    "table",
    "equation",
    "results",
    "result",
    "however",
    "therefore",
    "thus",
    "respectively",
    "propose",
    "proposed",
    "requires",
    "required",
    "require",
}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKTICK_RE = re.compile(r"`([^`\n]{1,120})`")
_SNAKE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_PASCAL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_DOTTED_RE = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\(\)")
_IDENTIFIER_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


@dataclass
class SignalHit:
    signal: str
    line_no: int
    line: str
    kind: str  # code | comment | docstring | string

    @property
    def is_code(self) -> bool:
        return self.kind == "code"


def split_identifier_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for part in re.split(r"[\s._\-/]+", value):
        if not part:
            continue
        tokens.extend(token for token in _CAMEL_BOUNDARY.split(part) if token)
    return [token for token in tokens if token]


def signal_regex(signal: str) -> re.Pattern[str]:
    """Compile a whole-token, case-insensitive matcher for one signal."""
    variants: set[str] = set()
    cleaned = signal.strip().strip("`")
    if not cleaned:
        return re.compile(r"(?!x)x")
    variants.add(cleaned)
    if "_" in cleaned:
        variants.add(cleaned.replace("_", " "))
        variants.add("".join(part.capitalize() for part in cleaned.split("_")))
    if " " in cleaned:
        variants.add(cleaned.replace(" ", "_"))
        variants.add(cleaned.replace(" ", ""))
    if "." in cleaned:
        variants.add(cleaned.split(".")[-1])
    patterns: list[str] = []
    for variant in sorted(variants, key=len, reverse=True):
        parts = [re.escape(part) for part in re.split(r"[\s_]+", variant.strip()) if part]
        if not parts:
            continue
        body = r"[\s_]*".join(parts)
        patterns.append(rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])")
    return re.compile("|".join(patterns), re.IGNORECASE)


def line_kinds(text: str) -> list[str]:
    """Classify each line as code, comment, docstring, string or blank.

    This is a lightweight scanner, not a parser. It exists so a match in a
    comment is never counted as executable evidence; the mapping explanation says
    the classification is heuristic.
    """
    kinds: list[str] = []
    triple: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if triple is not None:
            kinds.append("docstring")
            if triple in line:
                triple = None
            continue
        if not stripped:
            kinds.append("blank")
            continue
        if stripped.startswith("#"):
            kinds.append("comment")
            continue
        opened = None
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                opened = quote
                break
        if opened is not None:
            kinds.append("docstring")
            triple = opened
            continue
        kinds.append("code")
    return kinds


def find_signal_hits(text: str, signals: list[str]) -> list[SignalHit]:
    if not text or not signals:
        return []
    lines = text.splitlines()
    kinds = line_kinds(text)
    compiled = [(signal, signal_regex(signal)) for signal in signals if signal.strip()]
    hits: list[SignalHit] = []
    for index, line in enumerate(lines):
        for signal, pattern in compiled:
            match = pattern.search(line)
            if match is None:
                continue
            kind = kinds[index] if index < len(kinds) else "code"
            if kind == "code" and _inside_string(line, match.start()):
                kind = "string"
            hits.append(
                SignalHit(signal=signal, line_no=index + 1, line=line.strip()[:300], kind=kind)
            )
    return hits


def matched_signals(text: str, signals: list[str], *, code_only: bool = False) -> list[str]:
    hits = find_signal_hits(text, signals)
    found: list[str] = []
    for hit in hits:
        if code_only and not hit.is_code:
            continue
        if hit.signal not in found:
            found.append(hit.signal)
    return found


def _inside_string(line: str, position: int) -> bool:
    prefix = line[:position]
    if prefix.count('"') % 2 == 1:
        return True
    if prefix.count("'") % 2 == 1:
        return True
    return False


def derive_identifiers(text: str, *, limit: int = 16) -> list[str]:
    """Extract code-shaped identifiers mentioned in a claim."""
    found: list[str] = []

    def add(value: str) -> None:
        candidate = value.strip().strip("`").strip()
        if not candidate or len(candidate) > 120:
            return
        if candidate.casefold() in {item.casefold() for item in found}:
            return
        found.append(candidate)

    for match in _BACKTICK_RE.finditer(text):
        value = match.group(1).strip()
        if _IDENTIFIER_SHAPE.match(value) or "(" in value:
            add(value.split("(")[0])
    for pattern, _label in (
        (_SNAKE_RE, "snake"),
        (_CAMEL_RE, "camel"),
        (_PASCAL_RE, "pascal"),
        (_DOTTED_RE, "dotted"),
    ):
        for match in pattern.finditer(text):
            add(match.group(0))
            if len(found) >= limit:
                return found
    for match in _CALL_RE.finditer(text):
        add(match.group(1))
    return found[:limit]


def requirement_terms(text: str, *, limit: int = 40) -> list[str]:
    """Content words from a requirement, expanded through identifier splitting."""
    terms: list[str] = []
    for token in _TOKEN_RE.findall(text):
        for part in split_identifier_tokens(token):
            lowered = part.casefold()
            if len(lowered) < 4 or lowered in STOPWORDS:
                continue
            if lowered.isdigit():
                continue
            if lowered not in terms:
                terms.append(lowered)
    return terms[:limit]


def classify_constructs(text: str, *, path: str, symbol_kind: str | None = None) -> set[str]:
    """Cheap structural facts used in mapping explanations."""
    constructs: set[str] = set()
    lowered_path = path.lower()
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:async\s+)?def\s+\w+", stripped):
            constructs.add("defines_function")
            if stripped.startswith("def test_") or " test_" in stripped:
                constructs.add("test_function")
        elif re.match(r"^class\s+\w+", stripped):
            constructs.add("defines_class")
        if re.match(r"^[A-Za-z_][\w\.\[\]]*\s*(?<![=!<>:])=(?!=)", stripped):
            constructs.add("assignment")
        if re.search(r"\b(?:config|cfg|args|params|hparams|kwargs)\b", stripped):
            constructs.add("config_access")
        if re.search(r"\breturn\b", stripped):
            constructs.add("returns")
        if re.search(r"\b(?:raise|assert)\b", stripped):
            constructs.add("validation")
        if "torch." in stripped or "np." in stripped or "jax." in stripped:
            constructs.add("tensor_ops")
    if symbol_kind:
        constructs.add(f"symbol_{symbol_kind}")
    if "/tests/" in lowered_path or lowered_path.startswith("tests/") or "test_" in lowered_path.rsplit("/", 1)[-1]:
        constructs.add("test_file")
    return constructs


def looks_like_identifier(value: str) -> bool:
    cleaned = value.strip()
    if len(cleaned) < 3 or len(cleaned) > 120:
        return False
    if " " in cleaned:
        return False
    return bool(_IDENTIFIER_SHAPE.match(cleaned))


__all__ = [
    "STOPWORDS",
    "SignalHit",
    "split_identifier_tokens",
    "signal_regex",
    "line_kinds",
    "find_signal_hits",
    "matched_signals",
    "derive_identifiers",
    "requirement_terms",
    "classify_constructs",
    "looks_like_identifier",
]

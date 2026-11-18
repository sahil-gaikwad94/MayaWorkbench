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

import ast
import io
import re
import tokenize
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


_SEGMENT_END = 10**9

# Python 3.12 tokenizes f-strings into FSTRING_* parts instead of STRING.
_FSTRING_TOKENS = {
    getattr(tokenize, name)
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    if hasattr(tokenize, name)
}


def _add_segment(
    segments: dict[int, list[tuple[int, int, str]]],
    line: int,
    start: int,
    end: int,
    kind: str,
) -> None:
    segments.setdefault(line, []).append((start, end, kind))


def _python_docstring_rows(text: str) -> set[int]:
    """Line numbers where a module/class/function docstring begins."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    rows: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                rows.add(first.value.lineno)
    return rows


def _python_segments(text: str) -> dict[int, list[tuple[int, int, str]]] | None:
    """True token spans for comments, strings and docstrings.

    Using :mod:`tokenize` instead of ``startswith('#')`` is what stops an inline
    comment from being mistaken for executable code. Returns ``None`` when the
    span is not tokenizable (for example, a window cut mid-function), in which
    case callers fall back to a conservative per-character scanner.
    """
    doc_rows = _python_docstring_rows(text)
    segments: dict[int, list[tuple[int, int, str]]] = {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    for token in tokens:
        if token.type == tokenize.COMMENT:
            _add_segment(
                segments, token.start[0], token.start[1], token.end[1], "comment"
            )
        elif token.type == tokenize.STRING or token.type in _FSTRING_TOKENS:
            kind = "docstring" if token.start[0] in doc_rows else "string"
            for line in range(token.start[0], token.end[0] + 1):
                start = token.start[1] if line == token.start[0] else 0
                end = token.end[1] if line == token.end[0] else _SEGMENT_END
                _add_segment(segments, line, start, end, kind)
    return segments


def _comment_column(line: str) -> int | None:
    """Column of the first unquoted ``#`` on a line, or ``None``."""
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "#":
            return index
    return None


def _fallback_kind(line: str, position: int) -> str:
    if _inside_string(line, position):
        return "string"
    comment = _comment_column(line)
    if comment is not None and comment <= position:
        return "comment"
    return "code"


def line_kinds(text: str) -> list[str]:
    """Classify each line as code, comment, docstring, string or blank.

    A line that contains any executable token is ``code`` even when it also has
    a trailing comment. The per-match classification in :func:`find_signal_hits`
    is authoritative; this helper exists for summaries and reporting.
    """
    segments = _python_segments(text)
    kinds: list[str] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            kinds.append("blank")
            continue
        line_segments = segments.get(index, []) if segments is not None else []
        if not line_segments:
            kinds.append("comment" if stripped.startswith("#") else "code")
            continue
        only_prose = all(
            kind in {"comment", "string", "docstring"} for _s, _e, kind in line_segments
        )
        if not only_prose:
            kinds.append("code")
            continue
        kinds.append(line_segments[0][2])
    return kinds


def find_signal_hits(
    text: str, signals: list[str], *, language: str = "python"
) -> list[SignalHit]:
    """Find signal matches and classify each by executable/prose context."""
    if not text or not signals:
        return []
    lines = text.splitlines()
    segments = _python_segments(text) if language in {"python", "py"} else None
    compiled = [(signal, signal_regex(signal)) for signal in signals if signal.strip()]
    hits: list[SignalHit] = []
    for index, line in enumerate(lines):
        for signal, pattern in compiled:
            match = pattern.search(line)
            if match is None:
                continue
            kind = "code"
            if segments is not None:
                for start, end, segment_kind in segments.get(index + 1, []):
                    if start <= match.start() < end:
                        kind = segment_kind
                        break
            else:
                kind = _fallback_kind(line, match.start())
            hits.append(
                SignalHit(signal=signal, line_no=index + 1, line=line.strip()[:300], kind=kind)
            )
    return hits


def matched_signals(
    text: str,
    signals: list[str],
    *,
    code_only: bool = False,
    language: str = "python",
) -> list[str]:
    hits = find_signal_hits(text, signals, language=language)
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

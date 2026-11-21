"""Heuristic symbol adapters for non-Python languages (WP5).

These adapters deliberately do **not** claim AST-level understanding. They use
bounded regular expressions to discover declarations, imports and call sites,
and mark every produced relation with ``HEURISTIC`` source and LOW confidence.
A future Tree-sitter adapter can replace one language without changing the
indexer or the retrieval layer.

The adapter layer is intentionally small: a bounded number of symbols, imports
and calls per file, computed from already-decoded text with no subprocesses and
no repository-controlled execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from mayaworkbench.domain.enums import RelationSource, SymbolKind
from mayaworkbench.indexing.python_parser import (
    ParsedCall,
    ParsedImport,
    ParsedModule,
    ParsedSymbol,
)

MAX_SYMBOLS_PER_FILE = 2000
MAX_IMPORTS_PER_FILE = 2000
MAX_CALLS_PER_FILE = 2000
MAX_BLOCK_LINES = 500

_DECLARATIONS: dict[str, tuple[tuple[str, re.Pattern[str], SymbolKind], ...]] = {
    "javascript": (
        (
            r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
            SymbolKind.FUNCTION,
        ),
        (r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", SymbolKind.CLASS),
        (
            r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
            SymbolKind.CONSTANT,
        ),
    ),
    "typescript": (
        (
            r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
            SymbolKind.FUNCTION,
        ),
        (r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", SymbolKind.CLASS),
        (r"^(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", SymbolKind.TYPE_ALIAS),
        (r"^(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=", SymbolKind.TYPE_ALIAS),
        (r"^(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)", SymbolKind.TYPE_ALIAS),
        (
            r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=",
            SymbolKind.CONSTANT,
        ),
    ),
    "go": (
        (r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", SymbolKind.FUNCTION),
        (r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b", SymbolKind.CLASS),
        (r"^type\s+([A-Za-z_]\w*)\s+[A-Za-z_]", SymbolKind.TYPE_ALIAS),
        (r"^const\s+([A-Za-z_]\w*)\s*=", SymbolKind.CONSTANT),
        (r"^var\s+([A-Za-z_]\w*)\s*=", SymbolKind.CONSTANT),
    ),
    "rust": (
        (r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", SymbolKind.FUNCTION),
        (r"^(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)", SymbolKind.CLASS),
        (r"^(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_]\w*)", SymbolKind.TYPE_ALIAS),
        (r"^(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_]\w*)", SymbolKind.TYPE_ALIAS),
        (r"^(?:pub(?:\([^)]*\))?\s+)?type\s+([A-Za-z_]\w*)", SymbolKind.TYPE_ALIAS),
        (r"^(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)", SymbolKind.MODULE),
        (r"^(?:pub(?:\([^)]*\))?\s+)?const\s+([A-Za-z_]\w*)", SymbolKind.CONSTANT),
    ),
}
_DECLARATIONS["jsx"] = _DECLARATIONS["javascript"]
_DECLARATIONS["tsx"] = _DECLARATIONS["typescript"]
# C-family declarations. These are heuristics over declarations, not a syntax
# tree: templates, macros and overloads remain explicitly uncertain.
_C_DECLARATIONS: tuple[tuple[str, SymbolKind], ...] = (
    (r"^(?:static\s+)?(?:inline\s+)?(?:const\s+)?[\w:<>,\s\*&]+\b([A-Za-z_]\w*)\s*\([^;{]*\)\s*\{", SymbolKind.FUNCTION),
    (r"^\s*#define\s+([A-Za-z_]\w*)", SymbolKind.CONSTANT),
    (r"^typedef\s+(?:struct|enum|union)\s+([A-Za-z_]\w*)", SymbolKind.TYPE_ALIAS),
    (r"^\s*(?:typedef\s+)?(?:struct|enum|union|class)\s+([A-Za-z_]\w*)", SymbolKind.CLASS),
)
_DECLARATIONS["c"] = _C_DECLARATIONS
_DECLARATIONS["cpp"] = _C_DECLARATIONS
_DECLARATIONS["cuda"] = _C_DECLARATIONS
_COMPILED_DECLARATIONS: dict[str, tuple[tuple[re.Pattern[str], SymbolKind], ...]] = {
    language: tuple((re.compile(pattern, re.M), kind) for pattern, kind in declarations)
    for language, declarations in _DECLARATIONS.items()
}

_IMPORT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "javascript": (
        re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"^\s*require\(\s*['\"]([^'\"]+)['\"]"),
    ),
    "typescript": (
        re.compile(r"^\s*import\s+(?:type\s+)?.*?\s+from\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"^\s*require\(\s*['\"]([^'\"]+)['\"]"),
    ),
    "go": (
        re.compile(r'^\s*(?:[\w.]+\s+)?["`]([^"`]+)["`]'),
    ),
    "rust": (re.compile(r"^\s*use\s+([A-Za-z_][\w:]*)[^;]*;"),),
}
_IMPORT_PATTERNS["jsx"] = _IMPORT_PATTERNS["javascript"]
_IMPORT_PATTERNS["tsx"] = _IMPORT_PATTERNS["typescript"]

_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "function",
        "return",
        "await",
        "match",
        "select",
        "go",
        "defer",
        "impl",
        "use",
        "new",
        "typeof",
        "instanceof",
    }
)


@dataclass(frozen=True)
class AdapterResult:
    module: ParsedModule
    source: RelationSource = RelationSource.HEURISTIC


class ParserAdapter(Protocol):
    languages: frozenset[str]

    def parse(self, path: str, text: str) -> ParsedModule: ...


def module_name_for_adapter(path: str) -> str:
    """A dotted module name for a non-Python path (no extension, no src/ root)."""
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _block_end(lines: list[str], start: int) -> int:
    """A conservative end-line estimate from brace balance, capped by MAX_BLOCK_LINES."""
    depth = 0
    seen = False
    for offset in range(start - 1, min(len(lines), start - 1 + MAX_BLOCK_LINES)):
        for char in lines[offset]:
            if char == "{":
                depth += 1
                seen = True
            elif char == "}":
                depth -= 1
        if seen and depth <= 0:
            return offset + 1
    return min(len(lines), start - 1 + MAX_BLOCK_LINES)


class HeuristicAdapter:
    """Declaration/import/call discovery with explicit HEURISTIC provenance."""

    languages = frozenset(
        {"javascript", "typescript", "jsx", "tsx", "go", "rust", "c", "cpp", "cuda"}
    )

    def parse(self, path: str, text: str) -> ParsedModule:
        language = PurePosixPath(path).suffix.lower().lstrip(".")
        if language == "js":
            language = "javascript"
        elif language in {"mjs", "cjs"}:
            language = "javascript"
        elif language == "ts":
            language = "typescript"
        elif language == "rs":
            language = "rust"
        elif language in {"cc", "cxx", "hpp", "hh"}:
            language = "cpp"
        elif language in {"cu", "cuh"}:
            language = "cuda"
        elif language == "h":
            language = "c"
        module_name = module_name_for_adapter(path)
        lines = text.splitlines()
        module = ParsedModule(module_name=module_name)
        module.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.MODULE,
                name=module_name,
                qualified_name=module_name,
                signature=f"module {module_name}",
                start_line=1,
                end_line=max(1, len(lines)),
                docstring=None,
            )
        )

        declarations = _COMPILED_DECLARATIONS.get(language, ())
        for pattern, kind in declarations:
            for match in pattern.finditer(text):
                if len(module.symbols) >= MAX_SYMBOLS_PER_FILE:
                    module.truncated = True
                    break
                line = text.count("\n", 0, match.start()) + 1
                name = match.group(1)
                if any(item.name == name and item.start_line == line for item in module.symbols):
                    continue
                qualified = f"{module_name}.{name}" if module_name else name
                module.symbols.append(
                    ParsedSymbol(
                        kind=kind,
                        name=name,
                        qualified_name=qualified,
                        signature=match.group(0).strip()[:400],
                        start_line=line,
                        end_line=_block_end(lines, line),
                        docstring=None,
                    )
                )

        for pattern in _IMPORT_PATTERNS.get(language, ()):
            for match in pattern.finditer(text):
                if len(module.imports) >= MAX_IMPORTS_PER_FILE:
                    module.truncated = True
                    break
                line = text.count("\n", 0, match.start()) + 1
                target = match.group(1).replace('"', "").replace("'", "")
                module.imports.append(
                    ParsedImport(
                        module=target,
                        names=(),
                        line=line,
                        level=0,
                        is_from=False,
                    )
                )

        for match in _CALL_RE.finditer(text):
            if len(module.calls) >= MAX_CALLS_PER_FILE:
                module.truncated = True
                break
            callee = match.group(1)
            if callee in _KEYWORDS:
                continue
            line = text.count("\n", 0, match.start()) + 1
            caller = next(
                (
                    item.qualified_name
                    for item in module.symbols
                    if item.kind is not SymbolKind.MODULE
                    and item.start_line <= line <= item.end_line
                ),
                None,
            )
            module.calls.append(ParsedCall(callee=callee, line=line, caller=caller))
        return module


ADAPTERS: tuple[ParserAdapter, ...] = (HeuristicAdapter(),)


def adapter_for_language(language: str) -> ParserAdapter | None:
    for adapter in ADAPTERS:
        if language in adapter.languages:
            return adapter
    return None


__all__ = [
    "AdapterResult",
    "ADAPTERS",
    "HeuristicAdapter",
    "ParserAdapter",
    "adapter_for_language",
    "module_name_for_adapter",
]

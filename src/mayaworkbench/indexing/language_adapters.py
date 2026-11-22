"""Symbol adapters for non-Python languages (WP5, P2 pluggable parsers).

Two adapters are provided:

* :class:`HeuristicAdapter` never claims AST-level understanding. It uses
  bounded regular expressions and marks every relation ``HEURISTIC`` / LOW.
  It is the zero-dependency fallback and is always available.
* :class:`TreeSitterAdapter` uses a real concrete syntax tree when the optional
  ``treesitter`` extra is installed. It returns HIGH-confidence relations with
  AST provenance. Its dependencies are imported **lazily inside the factory**;
  module import never fails because a parser is missing.

A syntax tree establishes *syntactic* structure, not semantic resolution:
callees are still recorded as textual names and are never promoted to resolved
edges by the adapter. Both adapters consume already-decoded text supplied by
the indexer, so secret-path/secret-content filtering and all file/line/length
caps are applied upstream and are preserved unchanged here.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Protocol

from mayaworkbench.domain.enums import EdgeConfidence, RelationSource, SymbolKind
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
    """A parsed module together with honest provenance for its relations.

    ``source`` is the primary symbol provenance; ``import_source`` lets import
    edges carry their own (more specific) provenance when a real parser is used.
    ``parse_ok`` is False when a real parser was available but could not parse
    the file (for example an unsupported grammar), in which case the caller
    must fall back to the heuristic adapter rather than claim AST coverage.
    """

    module: ParsedModule
    source: RelationSource = RelationSource.HEURISTIC
    confidence: EdgeConfidence = EdgeConfidence.LOW
    parse_ok: bool = True
    language: str | None = None
    import_source: RelationSource | None = None
    parser: str = "heuristic"


# ``RelationSource`` has no ``AST_SYMBOL`` member (only ``AST_IMPORT``). We do
# not add an enum member here because persisted values require a migration;
# the closest honest existing value is used and the gap is reported to callers
# via ``AST_SYMBOL_SOURCE_GAP``.
AST_SYMBOL_SOURCE: RelationSource = getattr(
    RelationSource, "AST_SYMBOL", RelationSource.AST_IMPORT
)
AST_SYMBOL_SOURCE_GAP: str | None = (
    None
    if hasattr(RelationSource, "AST_SYMBOL")
    else "RelationSource.AST_SYMBOL is absent; AST_IMPORT is used as the closest honest value"
)


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


# --------------------------------------------------------------------------
# Optional real concrete-syntax-tree adapter.
# --------------------------------------------------------------------------
# Grammar names come from ``tree-sitter-language-pack``. CUDA has no dedicated
# grammar, so the closest C++ grammar is used and the mismatch is recorded in
# the result's ``parser`` field; a file with CUDA-only syntax that the C++
# grammar cannot parse is reported as a failed parse and falls back honestly.
_TS_GRAMMARS: dict[str, str] = {
    "javascript": "javascript",
    "jsx": "javascript",
    "typescript": "typescript",
    "tsx": "tsx",
    "go": "go",
    "rust": "rust",
    "c": "c",
    "cpp": "cpp",
    "cuda": "cpp",
}

_TS_SYMBOL_NODES: dict[str, dict[str, SymbolKind | None]] = {
    "javascript": {
        "function_declaration": SymbolKind.FUNCTION,
        "generator_function_declaration": SymbolKind.FUNCTION,
        "class_declaration": SymbolKind.CLASS,
        "interface_declaration": SymbolKind.TYPE_ALIAS,
        "type_alias_declaration": SymbolKind.TYPE_ALIAS,
        "enum_declaration": SymbolKind.TYPE_ALIAS,
        "method_definition": SymbolKind.FUNCTION,
        "variable_declarator": SymbolKind.CONSTANT,
    },
    "typescript": {
        "function_declaration": SymbolKind.FUNCTION,
        "generator_function_declaration": SymbolKind.FUNCTION,
        "class_declaration": SymbolKind.CLASS,
        "interface_declaration": SymbolKind.TYPE_ALIAS,
        "type_alias_declaration": SymbolKind.TYPE_ALIAS,
        "enum_declaration": SymbolKind.TYPE_ALIAS,
        "method_definition": SymbolKind.FUNCTION,
        "variable_declarator": SymbolKind.CONSTANT,
    },
    "go": {
        "function_declaration": SymbolKind.FUNCTION,
        "method_declaration": SymbolKind.FUNCTION,
        "type_spec": None,  # CLASS for struct/interface, TYPE_ALIAS otherwise
        "const_spec": SymbolKind.CONSTANT,
        "var_spec": SymbolKind.CONSTANT,
    },
    "rust": {
        "function_item": SymbolKind.FUNCTION,
        "struct_item": SymbolKind.CLASS,
        "enum_item": SymbolKind.TYPE_ALIAS,
        "trait_item": SymbolKind.TYPE_ALIAS,
        "type_item": SymbolKind.TYPE_ALIAS,
        "mod_item": SymbolKind.MODULE,
        "const_item": SymbolKind.CONSTANT,
    },
    "c": {
        "function_definition": SymbolKind.FUNCTION,
        "class_specifier": SymbolKind.CLASS,
        "struct_specifier": SymbolKind.CLASS,
        "enum_specifier": SymbolKind.TYPE_ALIAS,
        "type_definition": SymbolKind.TYPE_ALIAS,
        "preproc_def": SymbolKind.CONSTANT,
    },
}
_TS_SYMBOL_NODES["jsx"] = _TS_SYMBOL_NODES["javascript"]
_TS_SYMBOL_NODES["tsx"] = _TS_SYMBOL_NODES["typescript"]
_TS_SYMBOL_NODES["cpp"] = _TS_SYMBOL_NODES["c"]
_TS_SYMBOL_NODES["cuda"] = _TS_SYMBOL_NODES["c"]

_TS_IMPORT_NODES: dict[str, frozenset[str]] = {
    "javascript": frozenset({"import_statement"}),
    "typescript": frozenset({"import_statement"}),
    "jsx": frozenset({"import_statement"}),
    "tsx": frozenset({"import_statement"}),
    "go": frozenset({"import_declaration"}),
    "rust": frozenset({"use_declaration"}),
    "c": frozenset({"preproc_include"}),
}
_TS_IMPORT_NODES["cpp"] = _TS_IMPORT_NODES["c"]
_TS_IMPORT_NODES["cuda"] = _TS_IMPORT_NODES["c"]

_TS_CALL_NODES = frozenset({"call_expression"})
_TS_IDENTIFIER_NODES = frozenset(
    {"identifier", "type_identifier", "field_identifier", "property_identifier"}
)
_TS_STRING_NODES = frozenset(
    {"string", "interpreted_string_literal", "string_literal", "system_lib_string"}
)
_TS_QUOTE_CHARS = "\"'`<>"
_MAX_TS_NODES = 50000


def _adapter_language(path: str) -> str:
    language = PurePosixPath(path).suffix.lower().lstrip(".")
    aliases = {
        "js": "javascript",
        "mjs": "javascript",
        "cjs": "javascript",
        "ts": "typescript",
        "rs": "rust",
        "cc": "cpp",
        "cxx": "cpp",
        "hpp": "cpp",
        "hh": "cpp",
        "cu": "cuda",
        "cuh": "cuda",
        "h": "c",
    }
    return aliases.get(language, language)


def _node_text(node: Any, source_bytes: bytes) -> str:
    """Read a node's source text, tolerating both bytes and str implementations."""
    text = getattr(node, "text", None)
    if isinstance(text, bytes):
        return text.decode("utf-8", "replace")
    if isinstance(text, str):
        return text
    start = int(getattr(node, "start_byte", 0))
    end = int(getattr(node, "end_byte", start))
    return source_bytes[start:end].decode("utf-8", "replace")


def _line_of(node: Any) -> int:
    point = getattr(node, "start_point", (0, 0))
    return int(point[0]) + 1


def _end_line_of(node: Any) -> int:
    point = getattr(node, "end_point", (0, 0))
    return int(point[0]) + 1


def _children(node: Any) -> tuple[Any, ...]:
    try:
        return tuple(node.children)
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        return ()


def _walk(node: Any):
    """Pre-order walk with a hard node budget (yields at most ``_MAX_TS_NODES``)."""
    stack = [node]
    seen = 0
    while stack and seen < _MAX_TS_NODES:
        current = stack.pop()
        seen += 1
        yield current
        stack.extend(reversed(_children(current)))


def _name_node(node: Any) -> Any | None:
    field = node.child_by_field_name("name") if hasattr(node, "child_by_field_name") else None
    if field is not None:
        return field
    for child in _children(node):
        if child.type in _TS_IDENTIFIER_NODES:
            return child
    return None


def _signature(node: Any, source_bytes: bytes) -> str:
    text = " ".join(_node_text(node, source_bytes).split())
    return text[:400]


def _import_target(node: Any, source_bytes: bytes, language: str) -> str | None:
    if language == "rust":
        text = _node_text(node, source_bytes).strip()
        target = text[len("use") :].strip().rstrip(";").strip() if text.startswith("use") else text
        return target or None
    for child in _walk(node):
        if child.type in _TS_STRING_NODES:
            target = _node_text(child, source_bytes).strip().strip(_TS_QUOTE_CHARS)
            if target:
                return target
    return None


def _callee(node: Any, source_bytes: bytes) -> str | None:
    function = node.child_by_field_name("function") if hasattr(node, "child_by_field_name") else None
    if function is None:
        for child in _children(node):
            if child.type not in _TS_STRING_NODES:
                function = child
                break
    if function is None:
        return None
    text = _node_text(function, source_bytes)
    match = re.search(r"([A-Za-z_$][\w$]*)\s*$", text)
    if not match:
        return None  # dynamic/computed callee: never claim a resolved target
    callee = match.group(1)
    if callee in _KEYWORDS:
        return None
    return callee


class TreeSitterAdapter:
    """Real CST symbol discovery; HIGH confidence, AST provenance.

    Instances are only produced by :func:`tree_sitter_adapter` when both the
    ``tree_sitter`` runtime and the language-pack grammars import successfully.
    A missing grammar or a tree with ERROR nodes is reported via an
    :class:`AdapterResult` with ``parse_ok=False`` so the caller can fall back
    to :class:`HeuristicAdapter` without ever claiming FULL coverage.
    """

    languages = frozenset(_TS_GRAMMARS)

    def __init__(self, parser_factory: Callable[[str], Any]):
        self._parser_factory = parser_factory

    def parse(self, path: str, text: str) -> ParsedModule:
        """Protocol-compatible entry point; see ``parse_detailed`` for provenance."""
        return self.parse_detailed(path, text).module

    def parse_detailed(self, path: str, text: str) -> AdapterResult:
        language = _adapter_language(path)
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
        grammar = _TS_GRAMMARS.get(language)
        parser_label = f"tree-sitter:{grammar}" if grammar else "none"
        if grammar is None:
            return AdapterResult(
                module=module,
                source=RelationSource.HEURISTIC,
                confidence=EdgeConfidence.LOW,
                parse_ok=False,
                language=language,
                parser=parser_label,
            )

        try:
            parser = self._parser_factory(grammar)
            if parser is None:
                raise LookupError(f"no grammar for {grammar}")
            tree = parser.parse(text.encode("utf-8"))
            root = tree.root_node
            if getattr(root, "has_error", False):
                module.parse_error = f"tree-sitter reported ERROR nodes ({grammar})"
                return AdapterResult(
                    module=module,
                    source=RelationSource.HEURISTIC,
                    confidence=EdgeConfidence.LOW,
                    parse_ok=False,
                    language=language,
                    parser=parser_label,
                )
        except Exception as exc:  # fail closed to the heuristic fallback
            module.parse_error = f"tree-sitter parse unavailable ({grammar}): {type(exc).__name__}"
            return AdapterResult(
                module=module,
                source=RelationSource.HEURISTIC,
                confidence=EdgeConfidence.LOW,
                parse_ok=False,
                language=language,
                parser=parser_label,
            )

        source_bytes = text.encode("utf-8")
        symbol_nodes = _TS_SYMBOL_NODES.get(language, {})
        import_nodes = _TS_IMPORT_NODES.get(language, frozenset())
        for node in _walk(root):
            if node.type in symbol_nodes:
                if len(module.symbols) >= MAX_SYMBOLS_PER_FILE:
                    module.truncated = True
                    break
                name_node = _name_node(node)
                if name_node is None:
                    continue
                name = _node_text(name_node, source_bytes)
                if not name:
                    continue
                kind = symbol_nodes[node.type]
                if node.type == "type_spec":
                    node_text = _node_text(node, source_bytes)
                    kind = (
                        SymbolKind.CLASS
                        if "struct" in node_text or "interface" in node_text
                        else SymbolKind.TYPE_ALIAS
                    )
                if kind is None:
                    continue
                start_line = _line_of(node)
                end_line = min(
                    _end_line_of(node),
                    max(1, len(lines)),
                    start_line - 1 + MAX_BLOCK_LINES,
                )
                end_line = max(start_line, end_line)
                qualified = f"{module_name}.{name}" if module_name else name
                if any(
                    item.name == name and item.start_line == start_line for item in module.symbols
                ):
                    continue
                module.symbols.append(
                    ParsedSymbol(
                        kind=kind,
                        name=name,
                        qualified_name=qualified,
                        signature=_signature(node, source_bytes),
                        start_line=start_line,
                        end_line=end_line,
                        docstring=None,
                    )
                )
            elif node.type in import_nodes:
                if len(module.imports) >= MAX_IMPORTS_PER_FILE:
                    module.truncated = True
                    continue
                target = _import_target(node, source_bytes, language)
                if target:
                    module.imports.append(
                        ParsedImport(
                            module=target,
                            names=(),
                            line=_line_of(node),
                            level=0,
                            is_from=False,
                        )
                    )
            elif node.type in _TS_CALL_NODES:
                if len(module.calls) >= MAX_CALLS_PER_FILE:
                    module.truncated = True
                    continue
                callee = _callee(node, source_bytes)
                if callee is None:
                    continue
                line = _line_of(node)
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
        return AdapterResult(
            module=module,
            source=AST_SYMBOL_SOURCE,
            confidence=EdgeConfidence.HIGH,
            parse_ok=True,
            language=language,
            import_source=RelationSource.AST_IMPORT,
            parser=parser_label,
        )


def tree_sitter_adapter(
    *,
    tree_sitter_module: Any = None,
    language_pack_module: Any = None,
) -> TreeSitterAdapter | None:
    """Build a real CST adapter, or return None when dependencies are absent.

    Imports happen here (never at module import time). ``tree_sitter_module``
    and ``language_pack_module`` are optional injection points for offline
    tests; production callers leave them as None.
    """
    try:
        ts = tree_sitter_module if tree_sitter_module is not None else importlib.import_module("tree_sitter")
        pack = (
            language_pack_module
            if language_pack_module is not None
            else importlib.import_module("tree_sitter_language_pack")
        )
        get_parser = getattr(pack, "get_parser")
        if not callable(get_parser):
            return None
        # Touch the runtime API so a partially installed/broken package fails
        # closed here rather than later during indexing.
        if not hasattr(ts, "Parser") and not hasattr(ts, "Language"):
            return None
    except Exception:
        return None
    return TreeSitterAdapter(get_parser)


ADAPTERS: tuple[ParserAdapter, ...] = (HeuristicAdapter(),)


def adapter_for_language(language: str) -> ParserAdapter | None:
    """Prefer a real parser; fall back to bounded regex heuristics."""
    real = tree_sitter_adapter()
    if real is not None and language in real.languages:
        return real
    for adapter in ADAPTERS:
        if language in adapter.languages:
            return adapter
    return None


__all__ = [
    "AdapterResult",
    "ADAPTERS",
    "AST_SYMBOL_SOURCE",
    "AST_SYMBOL_SOURCE_GAP",
    "HeuristicAdapter",
    "ParserAdapter",
    "TreeSitterAdapter",
    "adapter_for_language",
    "module_name_for_adapter",
    "tree_sitter_adapter",
]

"""Python AST extraction.

Extracted facts are deliberately conservative:

* symbols carry exact source spans from the AST;
* imports keep their module, level, and local alias so resolution can be
  attempted later;
* calls record the textual callee. Resolution to a symbol is a separate,
  confidence-scored step — a name match is never treated as a semantic match;
* parse failures return a structured error instead of raising, so the file can
  still be searched lexically and the failure can be reported.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from mayaworkbench.domain.enums import SymbolKind

MAX_SYMBOLS_PER_FILE = 5000
MAX_CALLS_PER_FILE = 20000
MAX_DOCSTRING_CHARS = 2000
MAX_SIGNATURE_CHARS = 400


@dataclass(frozen=True)
class ParsedSymbol:
    kind: SymbolKind
    name: str
    qualified_name: str
    signature: str
    start_line: int
    end_line: int
    docstring: str | None
    decorators: tuple[str, ...] = ()
    parent_qualified: str | None = None


@dataclass(frozen=True)
class ParsedImport:
    module: str
    names: tuple[tuple[str, str], ...]
    line: int
    level: int
    is_from: bool


@dataclass(frozen=True)
class ParsedCall:
    callee: str
    line: int
    caller: str | None


@dataclass(frozen=True)
class ParsedBase:
    class_qualified: str
    base: str
    line: int


@dataclass(frozen=True)
class ParsedDecorator:
    target_qualified: str
    decorator: str
    line: int


@dataclass
class ParsedModule:
    module_name: str
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)
    bases: list[ParsedBase] = field(default_factory=list)
    decorators: list[ParsedDecorator] = field(default_factory=list)
    parse_error: str | None = None
    truncated: bool = False
    has_main_guard: bool = False


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _annotation(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - exotic nodes
        return ""
    return " ".join(text.split())


def _format_arg(arg: ast.arg, default: ast.AST | None = None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {_annotation(arg.annotation)}"
    if default is not None:
        rendered = _annotation(default)
        if len(rendered) > 60:
            rendered = rendered[:57] + "..."
        text += f"={rendered}"
    return text


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    rendered: list[str] = []
    positional = [*getattr(args, "posonlyargs", []), *args.args]
    defaults = list(args.defaults)
    default_offset = len(positional) - len(defaults)
    for index, arg in enumerate(positional):
        default = defaults[index - default_offset] if index >= default_offset else None
        rendered.append(_format_arg(arg, default))
        if getattr(args, "posonlyargs", None) and index + 1 == len(args.posonlyargs):
            rendered.append("/")
    if args.vararg is not None:
        rendered.append("*" + _format_arg(args.vararg))
    elif args.kwonlyargs:
        rendered.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        rendered.append(_format_arg(arg, default))
    if args.kwarg is not None:
        rendered.append("**" + _format_arg(args.kwarg))
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {_annotation(node.returns)}" if node.returns is not None else ""
    signature = f"{prefix} {node.name}({', '.join(rendered)}){returns}"
    if len(signature) > MAX_SIGNATURE_CHARS:
        signature = signature[: MAX_SIGNATURE_CHARS - 3] + "..."
    return signature


def _docstring(node: ast.AST) -> str | None:
    try:
        value = ast.get_docstring(node, clean=True)
    except (TypeError, ValueError):  # pragma: no cover
        return None
    if not value:
        return None
    value = value.strip()
    return value[:MAX_DOCSTRING_CHARS] if len(value) > MAX_DOCSTRING_CHARS else value


def _decorator_texts(node: ast.AST) -> tuple[str, ...]:
    decorators = getattr(node, "decorator_list", [])
    texts: list[str] = []
    for decorator in decorators:
        try:
            texts.append(" ".join(ast.unparse(decorator).split()))
        except (AttributeError, ValueError):  # pragma: no cover
            texts.append("<unparseable>")
    return tuple(texts)


def _is_constant_name(name: str) -> bool:
    stripped = name.strip("_")
    return bool(stripped) and stripped.upper() == stripped and any(c.isalpha() for c in stripped)


class _Collector(ast.NodeVisitor):
    def __init__(self, module_name: str, line_count: int):
        self.module_name = module_name
        self.line_count = line_count
        self.scopes: list[str] = []
        self.scope_kinds: list[str] = []
        self.result = ParsedModule(module_name=module_name)
        self.result.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.MODULE,
                name=module_name.rsplit(".", 1)[-1] or module_name,
                qualified_name=module_name,
                signature=f"module {module_name}",
                start_line=1,
                end_line=max(1, line_count),
                docstring=None,
            )
        )
        self._truncated = False

    # ---------------------------------------------------------------- helpers
    def _qualify(self, name: str) -> str:
        return ".".join([*self.scopes, name]) if self.scopes else name

    def _current_symbol(self) -> str | None:
        return ".".join(self.scopes) if self.scopes else self.module_name

    def _add_symbol(self, symbol: ParsedSymbol) -> None:
        if len(self.result.symbols) >= MAX_SYMBOLS_PER_FILE:
            self._truncated = True
            return
        self.result.symbols.append(symbol)

    # ------------------------------------------------------------------ nodes
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualify(node.name)
        bases = [_annotation(base) for base in node.bases]
        keywords = [f"{kw.arg}={_annotation(kw.value)}" for kw in node.keywords if kw.arg]
        header = ", ".join([*bases, *keywords])
        self._add_symbol(
            ParsedSymbol(
                kind=SymbolKind.CLASS,
                name=node.name,
                qualified_name=qualified,
                signature=f"class {node.name}({header})"[:MAX_SIGNATURE_CHARS],
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=_docstring(node),
                decorators=_decorator_texts(node),
                parent_qualified=".".join(self.scopes) or None,
            )
        )
        for base in node.bases:
            rendered = _annotation(base)
            if rendered:
                self.result.bases.append(
                    ParsedBase(class_qualified=qualified, base=rendered, line=node.lineno)
                )
        for decorator in node.decorator_list:
            rendered = _annotation(decorator)
            if rendered:
                self.result.decorators.append(
                    ParsedDecorator(
                        target_qualified=qualified,
                        decorator=rendered,
                        line=getattr(decorator, "lineno", node.lineno),
                    )
                )
        self.scopes.append(node.name)
        self.scope_kinds.append("class")
        self.generic_visit(node)
        self.scopes.pop()
        self.scope_kinds.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = self._qualify(node.name)
        kind = SymbolKind.METHOD if self.scope_kinds and self.scope_kinds[-1] == "class" else SymbolKind.FUNCTION
        self._add_symbol(
            ParsedSymbol(
                kind=kind,
                name=node.name,
                qualified_name=qualified,
                signature=_format_signature(node),
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=_docstring(node),
                decorators=_decorator_texts(node),
                parent_qualified=".".join(self.scopes) or None,
            )
        )
        for decorator in node.decorator_list:
            rendered = _annotation(decorator)
            if rendered:
                self.result.decorators.append(
                    ParsedDecorator(
                        target_qualified=qualified,
                        decorator=rendered,
                        line=getattr(decorator, "lineno", node.lineno),
                    )
                )
        self.scopes.append(node.name)
        self.scope_kinds.append("function")
        self.generic_visit(node)
        self.scopes.pop()
        self.scope_kinds.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.result.imports.append(
                ParsedImport(
                    module=alias.name,
                    names=((alias.name, local),),
                    line=node.lineno,
                    level=0,
                    is_from=False,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        names: list[tuple[str, str]] = []
        for alias in node.names:
            if alias.name == "*":
                names.append(("*", "*"))
                continue
            names.append((alias.name, alias.asname or alias.name))
        self.result.imports.append(
            ParsedImport(
                module=module,
                names=tuple(names),
                line=node.lineno,
                level=node.level or 0,
                is_from=True,
            )
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.scopes:
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_constant_name(target.id):
                    self._add_symbol(
                        ParsedSymbol(
                            kind=SymbolKind.CONSTANT,
                            name=target.id,
                            qualified_name=target.id,
                            signature=target.id,
                            start_line=node.lineno,
                            end_line=getattr(node, "end_lineno", node.lineno),
                            docstring=None,
                        )
                    )
        self.generic_visit(node)

    def visit_TypeAlias(self, node: ast.AST) -> None:
        # PEP 695 ``type Alias = ...`` (Python 3.12+); the visitor is simply
        # never called on older interpreters, which cannot parse the syntax.
        if not self.scopes:
            target = getattr(node, "name", None)
            if isinstance(target, ast.Subscript):
                target = target.value
            name = getattr(target, "id", None)
            if name:
                value = _annotation(getattr(node, "value", None))
                self._add_symbol(
                    ParsedSymbol(
                        kind=SymbolKind.TYPE_ALIAS,
                        name=name,
                        qualified_name=name,
                        signature=f"type {name} = {value}"[:MAX_SIGNATURE_CHARS],
                        start_line=getattr(node, "lineno", 1),
                        end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                        docstring=None,
                    )
                )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.scopes and isinstance(node.target, ast.Name) and _is_constant_name(node.target.id):
            self._add_symbol(
                ParsedSymbol(
                    kind=SymbolKind.CONSTANT,
                    name=node.target.id,
                    qualified_name=node.target.id,
                    signature=f"{node.target.id}: {_annotation(node.annotation)}"[
                        :MAX_SIGNATURE_CHARS
                    ],
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    docstring=None,
                )
            )
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if isinstance(test, ast.Compare):
            sides: list[ast.AST] = [test.left, *test.comparators]
            names = [side.id for side in sides if isinstance(side, ast.Name)]
            literals = [
                side.value for side in sides if isinstance(side, ast.Constant)
            ]
            if "__name__" in names and "__main__" in literals:
                self.result.has_main_guard = True
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted_name(node.func)
        if callee:
            if len(self.result.calls) >= MAX_CALLS_PER_FILE:
                self._truncated = True
            else:
                self.result.calls.append(
                    ParsedCall(
                        callee=callee,
                        line=getattr(node, "lineno", 0),
                        caller=self._current_symbol(),
                    )
                )
        self.generic_visit(node)


def parse_python(source: str, *, module_name: str, path: str) -> ParsedModule:
    """Parse Python source. Never raises for syntax errors."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return ParsedModule(
            module_name=module_name,
            parse_error=f"syntax error at line {exc.lineno}: {exc.msg}",
        )
    except (ValueError, RecursionError, MemoryError) as exc:
        return ParsedModule(module_name=module_name, parse_error=f"parser failure: {exc}")

    line_count = source.count("\n") + 1
    collector = _Collector(module_name, line_count)
    try:
        collector.visit(tree)
    except RecursionError as exc:  # pragma: no cover - pathological nesting
        return ParsedModule(
            module_name=module_name,
            parse_error=f"AST traversal exceeded recursion limits: {exc}",
        )
    collector.result.truncated = collector._truncated
    module_doc = _docstring(tree)
    collector.result.symbols[0] = ParsedSymbol(
        kind=SymbolKind.MODULE,
        name=collector.result.symbols[0].name,
        qualified_name=module_name,
        signature=f"module {module_name}",
        start_line=1,
        end_line=max(1, line_count),
        docstring=module_doc,
    )
    return collector.result


__all__ = [
    "ParsedModule",
    "ParsedSymbol",
    "ParsedImport",
    "ParsedCall",
    "ParsedBase",
    "ParsedDecorator",
    "parse_python",
]

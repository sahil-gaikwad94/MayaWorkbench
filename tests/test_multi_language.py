"""WP5 end-to-end: non-Python languages get honest, searchable symbol records."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import (
    EdgeConfidence,
    FileStatus,
    IssueKind,
    ParseCoverage,
    RelationSource,
    SymbolKind,
)
from mayaworkbench.indexing import indexer as indexer_module
from mayaworkbench.indexing.language_adapters import (
    AST_SYMBOL_SOURCE_GAP,
    HeuristicAdapter,
    TreeSitterAdapter,
    adapter_for_language,
    tree_sitter_adapter,
)

from .conftest import git, write

TS = """import { Helper } from './helper';

export interface Config {
  width: number;
}

export class Renderer {
  configure(config: Config): void {
    Helper.apply(config);
  }
}

export function createRenderer(): Renderer {
  return new Renderer();
}

export const DEFAULT_WIDTH = 640;
"""

GO = """package main

import "fmt"

type Engine struct {
    Name string
}

func (e *Engine) Start() error {
    fmt.Println(e.Name)
    return nil
}

func NewEngine(name string) *Engine {
    return &Engine{Name: name}
}
"""

CPP = """#include <cstddef>
#define BATCH_SIZE 32

struct Options {
    int width;
};

int add(int a, int b) {
    return a + b;
}

class Engine {
public:
    int step(int x) { return x + 1; }
};
"""

CUDA = """__global__ void scale_kernel(float* out, float* in, int n) {
    int i = threadIdx.x;
    if (i < n) out[i] = in[i] * 2.0f;
}

float launch(float* out, float* in, int n) {
    return 0.0f;
}
"""

RUST = """use std::fmt;

pub struct attention {
    pub dim: usize,
}

pub trait Mask {
    fn apply(&self) -> usize;
}

pub fn build(dim: usize) -> attention {
    attention { dim }
}

impl fmt::Display for attention {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { write!(f, "{}", self.dim) }
}
"""


def _multi_repo(tmp_path: Path) -> Path:
    root = tmp_path / "multi-repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    write(root / "src" / "renderer.ts", TS)
    write(root / "engine.go", GO)
    write(root / "attention.rs", RUST)
    write(root / "math.cpp", CPP)
    write(root / "scale.cu", CUDA)
    write(root / "README.md", "# Mixed repository\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Multi-language fixture")
    return root


def _index(services, project, repo):
    attached = services.repository_service.attach(project, repo)
    snapshot = services.repository_service.create_snapshot(attached.repository).snapshot
    report = services.indexer.index(snapshot.id)
    return snapshot, report


def test_adapter_discovers_symbols_imports_and_calls_with_heuristic_provenance():
    adapter = adapter_for_language("typescript")
    assert isinstance(adapter, HeuristicAdapter)
    parsed = adapter.parse("src/renderer.ts", TS)
    names = {item.name for item in parsed.symbols}
    assert {"Renderer", "Config", "createRenderer", "DEFAULT_WIDTH"} <= names
    assert any(item.module == "./helper" for item in parsed.imports)
    assert any(call.callee == "apply" for call in parsed.calls)
    assert all("HEURISTIC" in RelationSource.HEURISTIC.value for item in parsed.symbols)


def test_indexer_creates_low_confidence_symbols_for_js_go_rust(services, project, tmp_path):
    snapshot, report = _index(services, project, _multi_repo(tmp_path))
    assert report.symbols > 0

    renderer = services.files.get_by_path(snapshot.id, "src/renderer.ts")
    assert renderer is not None
    assert renderer.status == FileStatus.LEXICAL_ONLY
    assert renderer.parse_coverage == ParseCoverage.PARTIAL

    symbols = services.symbols.find_by_qualified(snapshot.id, "renderer.Renderer")
    assert symbols, "TypeScript class was not indexed"
    symbol = symbols[0]
    assert symbol.kind is SymbolKind.CLASS
    assert symbol.confidence == EdgeConfidence.LOW
    assert symbol.start_line >= 1 and symbol.end_line >= symbol.start_line

    edges = services.symbols.edges_from(symbol.id)
    assert edges, "expected heuristic call/import edges from the class"
    assert any(edge.get("provenance") == RelationSource.HEURISTIC.value for edge in edges)

    go_symbols = services.symbols.find_by_qualified(snapshot.id, "engine.Start")
    rust_symbols = services.symbols.find_by_qualified(snapshot.id, "attention.build")
    assert go_symbols and rust_symbols
    cpp = services.symbols.find_by_qualified(snapshot.id, "math.add")
    cuda = services.symbols.find_by_qualified(snapshot.id, "scale.scale_kernel")
    assert cpp and cuda
    assert all(item.confidence == EdgeConfidence.LOW for item in cpp + cuda)


def test_repo_map_declares_heuristic_languages(services, project, tmp_path):
    snapshot, _ = _index(services, project, _multi_repo(tmp_path))
    repo_map = services.maps.build(snapshot.id)
    assert set(repo_map.heuristic_languages) == {"cpp", "cuda", "go", "rust", "typescript"}
    assert "typescript" not in repo_map.lexical_only_languages
    assert any("HEURISTIC" in line for line in repo_map.limitations)


def test_repo_map_reports_the_parser_that_actually_ran(services, project, tmp_path, monkeypatch):
    snapshot, _ = _index(services, project, _multi_repo(tmp_path))
    original_get = services.snapshots.get

    def with_cst(snapshot_id):
        item = original_get(snapshot_id)
        metadata = dict(item.index_metadata or {})
        metadata["parser_provenance"] = {**metadata.get("parser_provenance", {}), "typescript": "TREE_SITTER:typescript@0.23"}
        return item.model_copy(update={"index_metadata": metadata})

    monkeypatch.setattr(services.snapshots, "get", with_cst)
    repo_map = services.maps.build(snapshot.id)
    assert "typescript" not in repo_map.heuristic_languages
    assert any(
        "Tree-sitter" in line and "typescript" in line for line in repo_map.limitations
    )


def test_symbols_are_searchable_and_renderable_from_cli(services, project, tmp_path, capsys):
    snapshot, _ = _index(services, project, _multi_repo(tmp_path))
    home = services.paths.home
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    code, payload = run(["search", "--snapshot", snapshot.id, "--symbols", "Renderer"])
    assert code == 0
    assert any("renderer.ts" in item.get("path", "") for item in payload.get("symbols", []))

    code, payload = run(["symbol", "show", "renderer.Renderer", "--snapshot", snapshot.id])
    assert code == 0
    assert payload["kind"] == "class"


# ---------------------------------------------------------------------------
# Optional tree-sitter adapter: deterministic fakes, no real dependency needed.
# ---------------------------------------------------------------------------
def _fake_node(
    node_type: str,
    text: str = "",
    *,
    row: int = 0,
    end_row: int | None = None,
    children: tuple = (),
    fields: dict | None = None,
    has_error: bool = False,
):
    raw = text.encode("utf-8")
    node = types.SimpleNamespace(
        type=node_type,
        text=raw,
        start_point=(row, 0),
        end_point=(end_row if end_row is not None else row, len(raw)),
        start_byte=0,
        end_byte=len(raw),
        children=list(children),
        has_error=has_error,
    )
    node.child_by_field_name = lambda name: (fields or {}).get(name)
    return node


def _fake_ts_tree(*, has_error: bool = False):
    """A minimal TypeScript CST: import, class+method, call, and a function."""
    import_node = _fake_node(
        "import_statement",
        "import { Helper } from './helper';",
        row=0,
        children=[_fake_node("string", "'./helper'", row=0)],
    )
    name = _fake_node("identifier", "Renderer", row=1)
    method_name = _fake_node("property_identifier", "configure", row=1)
    member = _fake_node("member_expression", "Helper.apply", row=1)
    call = _fake_node(
        "call_expression", "Helper.apply(1)", row=1, fields={"function": member}
    )
    method = _fake_node(
        "method_definition",
        "configure() { Helper.apply(1); }",
        row=1,
        end_row=2,
        fields={"name": method_name},
        children=[method_name, call],
    )
    class_node = _fake_node(
        "class_declaration",
        "class Renderer { configure() {} }",
        row=1,
        end_row=2,
        fields={"name": name},
        children=[name, method],
    )
    function_name = _fake_node("identifier", "build", row=3)
    function_node = _fake_node(
        "function_declaration",
        "function build() { return 1; }",
        row=3,
        fields={"name": function_name},
        children=[function_name],
    )
    root = _fake_node(
        "program",
        "",
        row=0,
        end_row=3,
        has_error=has_error,
        children=[import_node, class_node, function_node],
    )
    return types.SimpleNamespace(root_node=root)


def _fake_ts_modules(tree, *, grammar: str = "typescript"):
    parser = types.SimpleNamespace(parse=lambda data: tree)
    ts = types.ModuleType("tree_sitter")
    ts.Parser = object
    ts.Language = object
    pack = types.ModuleType("tree_sitter_language_pack")
    pack.get_parser = lambda name: parser if name == grammar else None
    return ts, pack


def test_tree_sitter_factory_fails_closed_without_dependencies(monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    assert tree_sitter_adapter() is None
    assert isinstance(adapter_for_language("typescript"), HeuristicAdapter)


def test_tree_sitter_adapter_parses_real_cst_with_ast_provenance(monkeypatch):
    ts, pack = _fake_ts_modules(_fake_ts_tree())
    monkeypatch.setitem(sys.modules, "tree_sitter", ts)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", pack)

    adapter = tree_sitter_adapter()
    assert isinstance(adapter, TreeSitterAdapter)
    assert isinstance(adapter_for_language("typescript"), TreeSitterAdapter)

    result = adapter.parse_detailed("src/renderer.ts", TS)
    assert result.parse_ok is True
    assert result.module.parse_error is None
    assert result.confidence is EdgeConfidence.HIGH
    assert result.source is RelationSource.AST_SYMBOL
    assert result.import_source is RelationSource.AST_IMPORT
    assert AST_SYMBOL_SOURCE_GAP is None
    names = {item.name for item in result.module.symbols}
    assert {"Renderer", "configure", "build"} <= names
    assert any(item.module == "./helper" for item in result.module.imports)
    assert any(call.callee == "apply" for call in result.module.calls)
    assert all(item.start_line >= 1 for item in result.module.symbols)


def test_indexer_uses_tree_sitter_only_when_it_actually_produced_symbols(
    services, project, tmp_path, monkeypatch
):
    ts, pack = _fake_ts_modules(_fake_ts_tree())
    real_adapter = tree_sitter_adapter(tree_sitter_module=ts, language_pack_module=pack)
    assert real_adapter is not None
    original_for_language = indexer_module.adapter_for_language

    def prefer_fake(language):
        if language == "typescript":
            return real_adapter
        return original_for_language(language)

    monkeypatch.setattr(indexer_module, "adapter_for_language", prefer_fake)
    snapshot, _ = _index(services, project, _multi_repo(tmp_path))

    renderer = services.files.get_by_path(snapshot.id, "src/renderer.ts")
    assert renderer is not None
    assert renderer.parse_coverage is ParseCoverage.FULL
    assert renderer.status is FileStatus.PARSED

    symbols = services.symbols.find_by_qualified(snapshot.id, "renderer.Renderer")
    assert symbols and symbols[0].confidence is EdgeConfidence.HIGH
    edges = services.symbols.edges_from(symbols[0].id)
    assert any(edge.get("provenance") == RelationSource.AST_SYMBOL.value for edge in edges)

    stored = services.snapshots.get(snapshot.id)
    provenance = (stored.index_metadata or {}).get("parser_provenance", {})
    assert provenance.get("typescript", "").startswith("TREE_SITTER:")
    # Languages without a real parser stay explicitly heuristic.
    assert provenance.get("go") == "HEURISTIC"
    go_symbols = services.symbols.find_by_qualified(snapshot.id, "engine.Start")
    assert go_symbols and go_symbols[0].confidence is EdgeConfidence.LOW


def test_indexer_reports_failed_tree_sitter_parse_as_heuristic_fallback(
    services, project, tmp_path, monkeypatch
):
    ts, pack = _fake_ts_modules(_fake_ts_tree(has_error=True))
    broken_adapter = tree_sitter_adapter(tree_sitter_module=ts, language_pack_module=pack)
    assert broken_adapter is not None
    original_for_language = indexer_module.adapter_for_language

    def prefer_broken(language):
        if language == "typescript":
            return broken_adapter
        return original_for_language(language)

    monkeypatch.setattr(indexer_module, "adapter_for_language", prefer_broken)
    snapshot, _ = _index(services, project, _multi_repo(tmp_path))

    renderer = services.files.get_by_path(snapshot.id, "src/renderer.ts")
    assert renderer is not None
    assert renderer.parse_coverage is ParseCoverage.PARTIAL
    assert renderer.status is FileStatus.LEXICAL_ONLY
    symbols = services.symbols.find_by_qualified(snapshot.id, "renderer.Renderer")
    assert symbols and symbols[0].confidence is EdgeConfidence.LOW
    stored = services.snapshots.get(snapshot.id)
    provenance = (stored.index_metadata or {}).get("parser_provenance", {})
    assert provenance.get("typescript") == "HEURISTIC_FALLBACK"
    issues = services.issues.list(snapshot.id, kind=IssueKind.PARSE_ERROR)
    assert any(issue.path == "src/renderer.ts" for issue in issues)

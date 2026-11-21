"""WP5 end-to-end: non-Python languages get honest, searchable symbol records."""

from __future__ import annotations

import json
from pathlib import Path

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import (
    EdgeConfidence,
    FileStatus,
    ParseCoverage,
    RelationSource,
    SymbolKind,
)
from mayaworkbench.indexing.language_adapters import (
    HeuristicAdapter,
    adapter_for_language,
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

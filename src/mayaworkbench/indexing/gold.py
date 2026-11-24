"""Gold fixture manifests and precision/recall evaluation for indexers.

The fixtures are deliberately small and hand-labelled. Each one records the
symbols, imports and call edges a correct adapter should produce, plus the
parser mode that produced them. Lexical fallbacks must stay LOW confidence; a
Tree-sitter/LSP adapter may produce HIGH-confidence AST edges only when it is
actually installed. No language is claimed supported merely because a fixture
exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "indexing-gold/v1"

#: Actual hand-labelled fixture source files (not just manifests). Each file's
#: symbols/imports/calls are listed in the matching GoldFixture above.
FIXTURES_ROOT = Path(__file__).with_name("gold").joinpath("fixtures")


def fixture_paths() -> dict[str, tuple[Path, ...]]:
    """Return the real fixture files grouped by language."""
    if not FIXTURES_ROOT.is_dir():
        return {}
    return {
        directory.name: tuple(sorted(path for path in directory.rglob("*") if path.is_file()))
        for directory in sorted(FIXTURES_ROOT.iterdir())
        if directory.is_dir()
    }


@dataclass(frozen=True)
class GoldFixture:
    language: str
    parser_mode: str  # ast | tree-sitter | lsp | lexical | heuristic
    files: tuple[str, ...]
    expected_symbols: tuple[str, ...]
    expected_imports: tuple[str, ...] = ()
    expected_calls: tuple[str, ...] = ()
    expected_test_links: tuple[str, ...] = ()
    note: str = ""


GOLD_FIXTURES: tuple[GoldFixture, ...] = (
    GoldFixture(
        language="python", parser_mode="ast",
        files=("service.py", "tests/test_service.py"),
        expected_symbols=("count_tokens", "build_context", "normalize"),
        expected_imports=("re", "pathlib"),
        expected_calls=("count_tokens", "normalize"),
        expected_test_links=("tests/test_service.py",),
        note="Python AST adapter; definition-level symbols are HIGH confidence.",
    ),
    GoldFixture(
        language="typescript", parser_mode="tree-sitter",
        files=("src/index.ts", "tests/index.test.ts"),
        expected_symbols=("tokenize", "Score", "scoreAll"),
        expected_imports=("fs", "path"),
        expected_calls=("tokenize",),
        expected_test_links=("tests/index.test.ts",),
        note="Requires the optional tree-sitter grammar; lexical fallback is LOW.",
    ),
    GoldFixture(
        language="go", parser_mode="tree-sitter",
        files=("main.go", "main_test.go"),
        expected_symbols=("Tokenize", "Score", "NewIndex"),
        expected_imports=("strings", "testing"),
        expected_calls=("Tokenize",),
        expected_test_links=("main_test.go",),
        note="Requires the optional tree-sitter grammar.",
    ),
    GoldFixture(
        language="rust", parser_mode="tree-sitter",
        files=("src/lib.rs", "tests/integration.rs"),
        expected_symbols=("tokenize", "score", "Index"),
        expected_imports=("std",),
        expected_calls=("tokenize",),
        expected_test_links=("tests/integration.rs",),
        note="Requires the optional tree-sitter grammar.",
    ),
    GoldFixture(
        language="java", parser_mode="tree-sitter",
        files=("src/Main.java", "src/MainTest.java"),
        expected_symbols=("tokenize", "score", "Main"),
        expected_imports=("java.util",),
        expected_calls=("tokenize",),
        expected_test_links=("src/MainTest.java",),
        note="Requires the optional tree-sitter grammar.",
    ),
    GoldFixture(
        language="cpp", parser_mode="heuristic",
        files=("src/attention.cu", "tests/test_attention.cpp"),
        expected_symbols=("softmax_kernel", "attention_forward"),
        expected_imports=("cuda_runtime.h",),
        expected_calls=("softmax_kernel",),
        expected_test_links=("tests/test_attention.cpp",),
        note="C/C++/CUDA handling is heuristic and stays LOW confidence.",
    ),
    GoldFixture(
        language="sql", parser_mode="lexical",
        files=("schema.sql", "migrations/0001.sql"),
        expected_symbols=("users", "events"),
        expected_imports=(),
        expected_calls=(),
        note="Schema names are lexical hints only; no semantic SQL parser is claimed.",
    ),
)


def _prf(expected: set[str], predicted: set[str]) -> dict[str, float]:
    if not expected and not predicted:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "tp": 0, "fp": 0, "fn": 0}
    tp = len(expected & predicted)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(f1, 4), "tp": tp, "fp": len(predicted - expected),
        "fn": len(expected - predicted),
    }


def evaluate_index(
    *,
    fixtures: Iterable[GoldFixture],
    symbols: Iterable[Any],
    edges: Iterable[Any] = (),
    parser_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Score the index against each fixture's hand-labelled expectations."""
    rows = list(symbols)
    edge_rows = list(edges)
    modes = parser_modes or {}
    by_language: dict[str, list[GoldFixture]] = {}
    for fixture in fixtures:
        by_language.setdefault(fixture.language, []).append(fixture)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "languages": {},
        "note": (
            "Metrics are relative to these small hand-labelled fixtures only. "
            "A HIGH-confidence AST edge requires a real parser; lexical matches "
            "are reported separately and never promoted."
        ),
    }
    for language, cases in sorted(by_language.items()):
        # A symbol predicted for this language is matched by its bare name.
        predicted_symbols = {
            item.name for item in rows
            if getattr(item, "path", "").rsplit(".", 1)[-1].lower()
            in _extensions_for(language)
        }
        predicted_imports = {
            getattr(edge, "target_name", "")
            for edge in edge_rows
            if getattr(edge, "provenance", "") == "AST_IMPORT"
            or getattr(edge, "edge_type", "") == "IMPORTS"
        }
        language_result = []
        for fixture in cases:
            mode = modes.get(language, fixture.parser_mode)
            symbol_scores = _prf(set(fixture.expected_symbols), predicted_symbols)
            import_scores = _prf(set(fixture.expected_imports), predicted_imports)
            language_result.append({
                "case": fixture.language,
                "parser_mode_expected": fixture.parser_mode,
                "parser_mode_actual": mode,
                "parser_mode_matched": mode == fixture.parser_mode,
                "symbols": symbol_scores,
                "imports": import_scores,
                "note": fixture.note,
            })
        report["languages"][language] = language_result
    return report


def _extensions_for(language: str) -> set[str]:
    return {
        "python": {"py"}, "typescript": {"ts", "tsx"}, "go": {"go"},
        "rust": {"rs"}, "java": {"java"}, "cpp": {"cu", "cpp", "cc", "c", "h"},
        "sql": {"sql"},
    }.get(language, set())


__all__ = [
    "SCHEMA_VERSION", "FIXTURES_ROOT", "fixture_paths",
    "GoldFixture", "GOLD_FIXTURES", "evaluate_index",
]

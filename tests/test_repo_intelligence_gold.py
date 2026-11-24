"""WP5: gold fixtures, impact analysis and measured retrieval quality."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mayaworkbench.domain.enums import EdgeConfidence
from mayaworkbench.indexing.gold import GOLD_FIXTURES, evaluate_index
from mayaworkbench.indexing.impact import (
    analyze_change,
    build_impact_map,
    config_references,
    is_test_path,
)
from mayaworkbench.retrieval.evaluation import (
    RelevanceCase,
    evaluate_retrieval,
    precision_at_k,
    reciprocal_rank,
)


def _file(path: str, *, test: bool = False):
    return SimpleNamespace(
        id="f:" + path, path=path, content_artifact_id=None,
        is_test=test, language=path.rsplit(".", 1)[-1],
    )


def _symbol(path: str, name: str):
    return SimpleNamespace(id="s:" + name, path=path, name=name)


def _edge(source_file, target_file, *, confidence=EdgeConfidence.HIGH, provenance="ast", etype="CALLS"):
    return SimpleNamespace(
        source_symbol_id=None, source_file_id=source_file.id,
        target_file_id=target_file.id, target_symbol_id=None,
        edge_type=SimpleNamespace(value=etype), target_name="target",
        confidence=confidence, provenance=provenance, line=12,
    )


def test_gold_fixture_source_files_exist_and_contain_labelled_symbols():
    from mayaworkbench.indexing.gold import fixture_paths

    paths = fixture_paths()
    assert set(paths) >= {"python", "typescript", "go", "rust", "java", "cpp", "sql"}
    for fixture in GOLD_FIXTURES:
        assert fixture.language in paths, fixture.language
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths[fixture.language])
        for symbol in fixture.expected_symbols:
            assert symbol in source, (fixture.language, symbol)


def test_gold_fixtures_cover_the_declared_languages():
    languages = {item.language for item in GOLD_FIXTURES}
    assert {"python", "typescript", "go", "rust", "java", "cpp", "sql"} <= languages
    assert all(item.expected_symbols for item in GOLD_FIXTURES)
    report = evaluate_index(fixtures=GOLD_FIXTURES, symbols=[], edges=[])
    assert set(report["languages"]) == languages
    assert report["languages"]["python"][0]["symbols"]["recall"] == 0.0


def test_gold_evaluation_reports_real_precision_recall():
    fixture = next(item for item in GOLD_FIXTURES if item.language == "python")
    symbols = [
        SimpleNamespace(path="service.py", name="count_tokens"),
        SimpleNamespace(path="service.py", name="build_context"),
        SimpleNamespace(path="service.py", name="irrelevant"),
    ]
    report = evaluate_index(fixtures=[fixture], symbols=symbols)
    scores = report["languages"]["python"][0]["symbols"]
    assert scores["tp"] == 2
    assert scores["fp"] == 1
    assert scores["fn"] == 1
    assert scores["precision"] == round(2 / 3, 4)
    assert scores["recall"] == round(2 / 3, 4)


def test_impact_map_separates_resolved_from_heuristic_edges():
    target = _file("service.py")
    test_file = _file("tests/test_service.py", test=True)
    caller = _file("api.py")
    files = [target, test_file, caller]
    edges = [
        _edge(test_file, target, confidence=EdgeConfidence.HIGH, provenance="ast"),
        _edge(caller, target, confidence=EdgeConfidence.LOW, provenance="heuristic"),
    ]
    map_ = build_impact_map(files=files, symbols=[], edges=edges)
    impact = map_["service.py"]
    assert any(item.path == "tests/test_service.py" for item in impact.affected_tests)
    assert any(item.path == "api.py" for item in impact.affected_symbols)
    assert impact.resolved_edges == 1 and impact.heuristic_edges == 1
    assert impact.limitations  # a lexical hint is never presented as truth
    result = analyze_change(["service.py", "missing.py"], files=files, symbols=[], edges=edges)
    assert result[0]["path"] == "service.py"
    assert "not in the pinned index" in result[1]["limitations"][0]
    assert is_test_path("tests/test_x.py") and not is_test_path("service.py")


def test_config_reference_extraction():
    text = (
        "import os\n"
        "token = os.environ['MAYA_MODEL_API_KEY']\n"
        "debug = os.getenv('DEBUG', '0')\n"
        "timeout = settings.request_timeout\n"
    )
    refs = config_references(text)
    assert "MAYA_MODEL_API_KEY" in refs and "DEBUG" in refs and "request_timeout" in refs
    assert config_references("") == []


def test_retrieval_evaluation_measures_uplift_not_assumes_it():
    cases = [
        RelevanceCase("tokenize text", ("utils.py", "service.py")),
        RelevanceCase("score function", ("score.py",)),
    ]
    baseline = {"tokenize text": ["unrelated.py", "utils.py"],
                "score function": ["other.py", "score.py"]}
    better = {"tokenize text": ["utils.py", "service.py"],
              "score function": ["score.py", "other.py"]}

    def retrieve(query, k, table):
        return table.get(query, [])[:k]

    report = evaluate_retrieval(
        cases,
        retrieve=lambda q, k: retrieve(q, k, baseline),
        modes=("baseline",),
        k=2,
    )
    base = report["modes"]["baseline"]
    assert base["precision_at_k"] == 0.5
    assert base["mrr"] > 0

    improved = evaluate_retrieval(
        cases,
        retrieve=lambda q, k: retrieve(q, k, baseline),
        retrievers={
            "baseline": lambda q, k: retrieve(q, k, baseline),
            "semantic": lambda q, k: retrieve(q, k, better),
        },
        modes=("baseline", "semantic"),
        k=2,
    )
    assert improved["modes"]["semantic"]["uplift_precision_at_k"] > 0
    assert improved["modes"]["semantic"]["uplift_mrr"] > 0
    assert precision_at_k(["a"], {"a"}, 0) == 0.0
    assert reciprocal_rank(["x", "a"], {"a"}) == 0.5

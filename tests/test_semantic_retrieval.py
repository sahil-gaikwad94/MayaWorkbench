"""Optional semantic adapters are deterministic and measured against a lexical baseline."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.retrieval.semantic import (
    HashingEmbeddingModel,
    LexicalReranker,
    evaluate_hybrid,
    ppr_scores,
)

CASES = [
    {
        "query": "causal attention masking",
        "candidates": [
            {"name": "README.md", "score": 0.9},
            {"name": "causal_mask.py", "score": 0.2},
        ],
        "relevant": "causal_mask.py",
    },
    {
        "query": "gradient finite difference check",
        "candidates": [
            {"name": "notes.txt", "score": 0.8},
            {"name": "gradient_check.py", "score": 0.1},
        ],
        "relevant": "gradient_check.py",
    },
]


def test_hashing_embeddings_are_deterministic_and_bounded():
    model = HashingEmbeddingModel()
    first = model.embed("causal attention mask")
    assert first == model.embed("causal attention mask")
    assert len(first) == model.dim
    assert 0.0 <= model.similarity("causal mask", "causal mask") <= 1.0


def test_reranker_beats_baseline_on_gold_set():
    result = evaluate_hybrid(CASES)
    assert result["cases"] == 2
    assert result["hybrid_hit_at_k"] >= result["baseline_hit_at_k"]
    assert "not an external benchmark" in result["note"]


def test_ppr_is_damped_and_normalised():
    scores = ppr_scores({"a": ["b"], "b": ["c"], "c": ["a"]}, ["a"])
    assert set(scores) == {"a", "b", "c"}
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    assert scores["a"] > scores["c"]


def test_semantic_cli_reports_evaluation(tmp_path, capsys):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(CASES), encoding="utf-8")
    base = ["--home", str(tmp_path / "home"), "--json"]
    assert main(base + ["init", "--project", "semantic"]) == 0
    capsys.readouterr()
    code = main(base + ["semantic", "evaluate", str(cases)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and "baseline_hit_at_k" in payload

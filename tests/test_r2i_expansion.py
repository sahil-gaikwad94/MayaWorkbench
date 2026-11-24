"""WP4: held-out corpus contract, equations, method adapters and R2I graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mayaworkbench.errors import UsageError
from mayaworkbench.evaluation.r2i_grading import (
    AnnotatorLabel,
    HoldoutCase,
    adjudicate,
    cohens_kappa,
    grading_guide_markdown,
    validate_holdout_corpus,
)
from mayaworkbench.research.equations import extract_equations
from mayaworkbench.verification.method_adapters import suggest_adapters


def _case(case_id: str, annotators: tuple[str, ...] = ("a", "b"), adjudicated: bool = True):
    labels = tuple(
        AnnotatorLabel(annotator_id=name, claim_id="c1", expected_status="PRESENT")
        for name in annotators
    )
    return HoldoutCase(
        case_id=case_id, labels=labels,
        adjudicated_status="PRESENT" if adjudicated else None,
        adjudicator="reviewer" if adjudicated else None,
    )


def test_holdout_contract_requires_two_annotators_and_adjudication():
    bad = validate_holdout_corpus([_case("c1", ("only-one",))], minimum=1)
    assert bad["qualifies_as_held_out"] is False
    assert any("independent annotators" in item for item in bad["problems"])

    unadjudicated = validate_holdout_corpus([_case("c1", adjudicated=False)], minimum=1)
    assert unadjudicated["qualifies_as_held_out"] is False
    assert any("adjudicated" in item for item in unadjudicated["problems"])

    good = validate_holdout_corpus(
        [_case("c1"), _case("c2")], minimum=2
    )
    assert good["qualifies_as_held_out"] is True
    assert good["cases"] == 2


def test_adjudication_preserves_raw_labels():
    case = _case("c1")
    updated = adjudicate(case, adjudicator="third", status="PARTIAL", note="disagreement")
    assert updated.labels == case.labels
    assert updated.adjudicated_status == "PARTIAL"
    assert updated.adjudicator == "third"
    with pytest.raises(UsageError):
        adjudicate(case, adjudicator="", status="PRESENT", note="")


def test_cohens_kappa_and_grading_guide():
    assert cohens_kappa([("a", "X"), ("b", "Y")], [("a", "X"), ("b", "Y")]) == 1.0
    assert cohens_kappa([("a", "X"), ("b", "Y")], [("a", "Y"), ("b", "X")]) == -1.0
    assert cohens_kappa([], []) == 0.0
    guide = grading_guide_markdown()
    assert "Wilson" in guide and "two independent annotators" in guide
    assert "never by the model" in guide


def test_equation_extraction_has_exact_source_spans():
    text = (
        "We compute a stable softmax.\n"
        "$$p_i = \\exp(x_i - m) / \\sum_j \\exp(x_j - m)$$\n"
        "Subject to: the row sum must equal one.\n"
    )
    spans = extract_equations(text)
    assert spans
    for span in spans:
        assert text[span.char_start:span.char_end] == span.text
        assert span.confidence in {"HIGH", "MEDIUM", "LOW"}
    assert any(
        span.kind == "EQUATION" and {"exp", "sum"} & set(span.operators)
        for span in spans
    )
    assert any(span.kind == "CONSTRAINT" for span in spans)
    assert extract_equations("") == []


def test_method_adapters_map_claims_to_registered_probes():
    suggestions = suggest_adapters(
        [
            {"id": "c1", "kind": "PAPER_METHOD", "statement": "We apply a causal mask before softmax."},
            {"id": "c2", "kind": "PAPER_RESULT", "statement": "Gradients match the analytic derivative."},
        ],
        text="",
    )
    names = {item["check_name"] for item in suggestions}
    assert "causal_mask_invariance" in names
    assert "finite_difference_gradient" in names
    assert all(item["verdict_authority"] == "backend" for item in suggestions)
    assert suggest_adapters([{"id": "x", "kind": "PAPER_METHOD", "statement": "unrelated prose"}]) == []


def test_development_corpus_is_expanded_and_still_labelled_seed():
    from mayaworkbench.evaluation.r2i import load_gold_corpus

    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "mayaworkbench" / "evaluation" / "gold"
        / "r2i_development_seed.json"
    )
    cases = load_gold_corpus(path)
    assert len(cases) >= 8
    assert all(item.is_seed_corpus for item in cases)
    assert len({item.case_id for item in cases}) == len(cases)


def test_cli_grading_guide_and_holdout_check(capsys, tmp_path):
    from mayaworkbench.cli.main import main

    code = main(["--home", str(tmp_path / "home"), "--json", "eval", "grading-guide"])
    captured = capsys.readouterr()
    assert code == 0
    assert "Wilson" in captured.out

    corpus = {
        "cases": [
            {
                "case_id": "c1",
                "labels": [
                    {"annotator_id": "a", "claim_id": "x", "expected_status": "PRESENT"},
                    {"annotator_id": "b", "claim_id": "x", "expected_status": "PRESENT"},
                ],
                "adjudicated_status": "PRESENT",
                "adjudicator": "reviewer",
            }
        ]
    }
    path = tmp_path / "holdout.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    code = main(["--home", str(tmp_path / "home"), "--json", "eval", "holdout-check",
                 str(path), "--json"])
    captured = capsys.readouterr()
    assert code == 1  # only one case, cannot qualify as >=50 held-out
    payload = json.loads(captured.out)
    assert payload["qualifies_as_held_out"] is False

"""Offline R2I evaluator: real arithmetic, honest zeros and a real CLI flow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import PAPER_MD

from mayaworkbench.cli.main import main
from mayaworkbench.evaluation.r2i import (
    CaseEvaluation,
    GoldCase,
    GoldClaimLabel,
    GoldMappingLabel,
    bundled_seed_case,
    claims_from_artifact,
    evaluate_case,
    evaluate_corpus,
    evaluate_extraction,
    evaluate_mapping,
    evaluation_payload,
    load_gold_case,
    mappings_from_artifact,
    render_markdown,
)
from mayaworkbench.research.passages import quote_hash

G1_QUOTE = "alpha beta gamma delta."
G2_QUOTE = "epsilon zeta eta theta."
G3_QUOTE = "iota kappa lambda mu."


def _claim(claim_id: str, statement: str, kind: str, quote: str) -> GoldClaimLabel:
    return GoldClaimLabel(
        id=claim_id,
        statement_fragments=[statement],
        kind=kind,
        source_quote=quote,
    )


def _case(
    claims: list[GoldClaimLabel] | None = None,
    mappings: list[GoldMappingLabel] | None = None,
    *,
    is_seed_corpus: bool = True,
) -> GoldCase:
    return GoldCase(
        case_id="unit-case",
        title="Unit case",
        paper_excerpt="alpha beta gamma delta.",
        claims=claims
        if claims is not None
        else [
            _claim("g1", "alpha beta gamma delta", "PAPER_METHOD", G1_QUOTE),
            _claim("g2", "epsilon zeta eta theta", "PAPER_RESULT", G2_QUOTE),
            _claim("g3", "iota kappa lambda mu", "REPRODUCTION_REQUIREMENT", G3_QUOTE),
        ],
        mappings=mappings
        if mappings is not None
        else [
            GoldMappingLabel(claim_id="m1", expected_status="PRESENT", relevant_paths=["attention.py"]),
            GoldMappingLabel(claim_id="m2", expected_status="PARTIAL", relevant_paths=["attention.py"]),
            GoldMappingLabel(
                claim_id="m3", expected_status="UNCERTAIN", relevant_paths=["attention.py"], uncertain=True
            ),
        ],
        provenance="unit test labels",
        is_seed_corpus=is_seed_corpus,
    )


def _pred_claim(claim_id, statement, *, kind="", quote=None, hashed=None):
    payload = {
        "id": claim_id,
        "statement": statement,
        "source_locations": [
            {"passage_id": "p1", "quote": quote or "", "quote_hash": hashed or ""}
        ],
    }
    if kind:
        payload["kind"] = kind
    return claims_from_artifact({"claims": [payload]})[0]


def _pred_mapping(claim_id, status, paths, *, verified=True):
    return mappings_from_artifact(
        {
            "rows": [
                {
                    "claim_id": claim_id,
                    "mapping_status": status,
                    "evidence": [
                        {"evidence": {"path": path}, "verified": verified} for path in paths
                    ],
                }
            ]
        }
    )[0]


# ------------------------------------------------------------ extraction math
def test_extraction_arithmetic_with_imperfect_predictions():
    case = _case()
    predictions = [
        _pred_claim(
            "p1", "alpha beta gamma delta", kind="PAPER_METHOD",
            quote=G1_QUOTE, hashed=quote_hash(G1_QUOTE),
        ),
        _pred_claim(
            "p2", "epsilon zeta eta theta", kind="PAPER_RESULT",
            quote=G2_QUOTE, hashed=quote_hash("a different, wrong span"),
        ),
        _pred_claim("p3", "an entirely unrelated fabricated assertion", kind="INFERRED_REQUIREMENT"),
    ]
    metrics = evaluate_extraction(case, predictions)

    # 2 TP, 1 FP (p3), 1 FN (g3) -> exact, computed values.
    assert (metrics.tp, metrics.fp, metrics.fn) == (2, 1, 1)
    assert metrics.precision == pytest.approx(2 / 3)
    assert metrics.recall == pytest.approx(2 / 3)
    assert metrics.f1 == pytest.approx(2 / 3)
    assert metrics.fabricated_claims == ["p3"]

    by_kind = metrics.by_kind
    assert (by_kind["PAPER_METHOD"].tp, by_kind["PAPER_METHOD"].fn) == (1, 0)
    assert by_kind["PAPER_METHOD"].precision == 1.0
    assert (by_kind["PAPER_RESULT"].tp, by_kind["PAPER_RESULT"].fn) == (1, 0)
    assert (by_kind["INFERRED_REQUIREMENT"].fp, by_kind["INFERRED_REQUIREMENT"].precision) == (1, 0.0)
    assert by_kind["REPRODUCTION_REQUIREMENT"].fn == 1
    assert by_kind["REPRODUCTION_REQUIREMENT"].recall == 0.0


def test_zero_denominators_are_zero_not_perfect():
    metrics = evaluate_extraction(_case(), [])
    assert (metrics.tp, metrics.fp, metrics.fn) == (0, 0, 3)
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0
    assert metrics.span_citation_validity == metrics.span_citation_validity.__class__(
        valid=0, total=0, rate=0.0, invalid_claim_ids=[]
    )

    empty_corpus = evaluate_corpus([])
    assert empty_corpus.case_count == 0
    assert empty_corpus.corpus_is_seed is False
    assert empty_corpus.micro_extraction.f1 == 0.0
    assert empty_corpus.micro_mapping.precision == 0.0
    assert empty_corpus.macro_extraction.recall == 0.0


def test_citation_validity_uses_exact_quote_hashes():
    case = _case()
    predictions = [
        _pred_claim(
            "valid-id", "alpha beta gamma delta",
            quote="  alpha   beta gamma delta. ", hashed=quote_hash(G1_QUOTE),
        ),
        _pred_claim(
            "wrong-hash", "epsilon zeta eta theta",
            quote=G2_QUOTE, hashed=quote_hash("not the cited span"),
        ),
        _pred_claim("no-citation", "iota kappa lambda mu"),
    ]
    metrics = evaluate_extraction(case, predictions)
    assert metrics.tp == 3 and metrics.fp == 0
    citation = metrics.span_citation_validity
    assert (citation.valid, citation.total) == (1, 3)
    assert citation.rate == pytest.approx(1 / 3)
    assert citation.invalid_claim_ids == ["wrong-hash", "no-citation"]


def test_empty_gold_labels_are_rejected_instead_of_scored_perfect():
    case = _case(claims=[], mappings=[])
    with pytest.raises(ValueError, match="no claim labels"):
        evaluate_extraction(case, [])
    with pytest.raises(ValueError, match="no mapping labels"):
        evaluate_mapping(case, [])
    with pytest.raises(ValueError, match="threshold"):
        evaluate_extraction(_case(), [], threshold=0.0)


def test_claim_adapter_accepts_exported_artifact_shapes():
    payload = {
        "document": {"id": "doc_1"},
        "claims": [
            {
                "id": "cl_1",
                "kind": {"value": "PAPER_METHOD"},
                "claim_type": "EXPLICIT_SOURCE_CLAIM",
                "title": "Fallback title",
                "statement": "computed statement",
                "source_locations": [
                    {"passage_id": "p1", "quote": G1_QUOTE, "quote_hash": quote_hash(G1_QUOTE)}
                ],
            }
        ],
    }
    claims = claims_from_artifact(payload)
    assert len(claims) == 1
    assert claims[0].id == "cl_1"
    assert claims[0].kind == "PAPER_METHOD"
    assert claims[0].statement == "computed statement"
    assert claims[0].effective_quote_hash == quote_hash(G1_QUOTE)
    assert claims_from_artifact([{"claim_id": "cl_2", "title": "only a title"}])[0].id == "cl_2"


# ---------------------------------------------------------------- mapping math
def test_mapping_status_compatibility():
    case = _case()
    predictions = [
        _pred_mapping("m1", "PRESENT", ["attention.py"]),
        _pred_mapping("m2", "PRESENT", ["attention.py"]),  # PARTIAL accepts PRESENT
        _pred_mapping("m3", "NOT_CHECKED", ["attention.py"]),  # uncertain accepts NOT_CHECKED
    ]
    metrics = evaluate_mapping(case, predictions)
    assert (metrics.tp, metrics.fp, metrics.fn) == (3, 0, 0)
    assert metrics.precision == 1.0 and metrics.recall == 1.0 and metrics.f1 == 1.0
    assert (metrics.uncertainty_correct, metrics.uncertainty_total) == (1, 1)
    assert metrics.uncertainty_rate == 1.0
    assert metrics.path_hit_rate == 1.0


def test_mapping_incompatible_statuses_and_uncertainty():
    case = _case()
    predictions = [
        _pred_mapping("m1", "PARTIAL", ["attention.py"]),  # PRESENT needs PRESENT
        _pred_mapping("m2", "ABSENT", ["attention.py"]),  # PARTIAL needs PARTIAL/PRESENT
        _pred_mapping("m3", "ABSENT", ["attention.py"]),  # uncertain needs UNCERTAIN/NOT_CHECKED
    ]
    metrics = evaluate_mapping(case, predictions)
    assert (metrics.tp, metrics.fp, metrics.fn) == (0, 3, 3)
    assert metrics.precision == 0.0 and metrics.recall == 0.0 and metrics.f1 == 0.0
    assert (metrics.uncertainty_correct, metrics.uncertainty_total) == (0, 1)
    assert metrics.uncertainty_rate == 0.0


def test_mapping_wrong_paths_and_verified_flags():
    case = _case()
    predictions = [
        _pred_mapping("m1", "PRESENT", ["model.py"]),  # right status, wrong path
        _pred_mapping("m2", "PARTIAL", ["./attention.py"]),  # normalized path hit
        _pred_mapping("m3", "UNCERTAIN", ["attention.py"], verified=False),  # unverified evidence
    ]
    metrics = evaluate_mapping(case, predictions)
    assert (metrics.tp, metrics.fp, metrics.fn) == (1, 2, 2)
    assert metrics.path_hits["m1"] == []
    assert metrics.path_hits["m2"] == ["attention.py"]
    assert metrics.path_hits["m3"] == []
    assert (metrics.path_hits_valid, metrics.path_hits_total) == (1, 3)
    assert metrics.path_hit_rate == pytest.approx(1 / 3)

    flat = mappings_from_artifact([{"claim_id": "m1", "status": "PRESENT", "path": "attention.py"}])
    assert flat[0].status == "PRESENT" and flat[0].paths == ("attention.py",)


# ------------------------------------------------------------ case and corpus
def test_case_and_corpus_aggregation():
    good = _case()
    imperfect = _case(claims=None, mappings=None, is_seed_corpus=False)
    eval_good = evaluate_case(
        good,
        claims=[_pred_claim("p1", "alpha beta gamma delta", quote=G1_QUOTE, hashed=quote_hash(G1_QUOTE))],
        mappings=[_pred_mapping("m1", "PRESENT", ["attention.py"])],
    )
    eval_empty = evaluate_case(imperfect, claims=[], mappings=[])
    corpus = evaluate_corpus([(good, eval_good), (imperfect, eval_empty)])

    assert corpus.case_count == 2
    assert corpus.corpus_is_seed is False
    assert corpus.micro_extraction.tp == 1
    assert corpus.micro_extraction.fn == 5
    assert corpus.micro_extraction.f1 == pytest.approx(2 / 7)  # P=1.0, R=1/6
    assert corpus.macro_extraction.precision == pytest.approx((1.0 + 0.0) / 2)
    assert corpus.micro_mapping.tp == 1
    assert corpus.micro_mapping.fn == 5

    payload = evaluation_payload(corpus)
    assert payload["kind"] == "corpus" and payload["corpus_is_seed"] is False
    assert "micro_extraction" in payload and "macro_mapping" in payload
    assert "# R2I corpus evaluation" in render_markdown(corpus)
    assert isinstance(eval_good, CaseEvaluation)


# --------------------------------------------------------------- gold loading
def test_load_gold_case_validates_malformed_files(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot read gold case"):
        load_gold_case(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_gold_case(broken)
    extra = tmp_path / "extra.json"
    extra.write_text(
        json.dumps({**json.loads(bundled_seed_case().model_dump_json()), "surprise": 1}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid schema"):
        load_gold_case(extra)
    bad_kind = tmp_path / "bad_kind.json"
    bad_kind.write_text(
        json.dumps({**json.loads(bundled_seed_case().model_dump_json()),
                    "claims": [{**json.loads(bundled_seed_case().claims[0].model_dump_json()),
                                "kind": "NOT_A_KIND"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid schema"):
        load_gold_case(bad_kind)


def test_bundled_seed_case_is_real_and_verbatim():
    case = bundled_seed_case()
    assert case.case_id == "causal_attention_seed"
    assert case.is_seed_corpus is True
    assert "not an external benchmark" in case.provenance.lower()
    assert len(case.claims) >= 5
    assert {claim.kind for claim in case.claims} >= {
        "PAPER_METHOD", "PAPER_RESULT", "REPRODUCTION_REQUIREMENT", "VALIDATION_CLAIM"
    }
    for claim in case.claims:
        assert claim.source_quote in PAPER_MD
        assert all(fragment in PAPER_MD or fragment.rstrip(".") in PAPER_MD
                   for fragment in claim.statement_fragments)
    statuses = {mapping.expected_status for mapping in case.mappings}
    assert {"PRESENT", "PARTIAL", "UNCERTAIN"} <= statuses
    padding = next(item for item in case.mappings if item.claim_id == "seed-padding-unknown")
    assert padding.uncertain is True


# ------------------------------------------------------------------ CLI + E2E
def _run(capsys, args):
    code = main(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else None
    return code, payload, captured


def test_eval_r2i_rejects_malformed_input_with_exit_code_2(capsys, home: Path, tmp_path: Path):
    base = ["--home", str(home), "--json"]
    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    code, _payload, captured = _run(
        capsys, base + ["eval", "r2i", "--claims", str(bad), "--matrix", str(bad)]
    )
    assert code == 2 and "malformed" in captured.err.lower()
    code, _payload, captured = _run(
        capsys, base + ["eval", "r2i", "--claims-artifact", "art_does_not_exist", "--matrix", str(bad)]
    )
    assert code == 2


def test_eval_r2i_end_to_end_with_real_cli_artifacts(
    capsys,
    home: Path,
    repo: Path,
    tmp_path: Path,
    services,
    indexed,
    paper,
    project,
):
    """Real extract + map artifacts, scored against the bundled seed corpus."""
    base = ["--home", str(home), "--json"]

    code, extracted, captured = _run(capsys, base + ["research", "extract", paper.document.id])
    assert code == 0, captured.err
    assert extracted["artifact_id"]

    code, mapped, captured = _run(
        capsys,
        base + ["map", "run", "--project", project.slug, "--snapshot", indexed.snapshot.id],
    )
    assert code == 0, captured.err
    assert mapped["artifact_id"]

    code, result, captured = _run(
        capsys,
        base + [
            "eval", "r2i",
            "--claims-artifact", extracted["artifact_id"],
            "--matrix-artifact", mapped["artifact_id"],
        ],
    )
    assert code == 0, captured.err
    assert result["kind"] == "case"
    assert result["corpus_is_seed"] is True
    assert result["predicted_claim_count"] > 0
    assert result["predicted_mapping_count"] > 0

    extraction = result["extraction"]
    for key in ("tp", "fp", "fn", "precision", "recall", "f1", "by_kind",
                "span_citation_validity", "fabricated_claims", "threshold"):
        assert key in extraction
    mapping = result["mapping"]
    for key in ("tp", "fp", "fn", "precision", "recall", "f1", "uncertainty_correct",
                "uncertainty_total", "uncertainty_rate", "path_hits", "path_hit_rate"):
        assert key in mapping

    # Counts reconcile with the real artifacts: nothing is hard-coded or faked.
    assert extraction["tp"] + extraction["fn"] == result["gold_claim_count"]
    assert extraction["tp"] + extraction["fp"] == result["predicted_claim_count"]
    assert mapping["tp"] + mapping["fn"] == result["gold_mapping_count"]
    assert mapping["tp"] + mapping["fp"] == result["predicted_mapping_count"]
    assert isinstance(extraction["precision"], float)
    assert isinstance(mapping["f1"], float)

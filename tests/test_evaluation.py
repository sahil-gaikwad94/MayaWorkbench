"""North-star evaluation: only verified outcomes count, failures stay in the denominator."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.evaluation.metrics import TaskOutcome, outcome_from_bundle, score


def _bundle(tmp_path, name, decision, evidence_status="PASSED"):
    root = tmp_path / name
    root.mkdir()
    (root / "report.json").write_text(
        json.dumps(
            {
                "task_id": name,
                "decision": decision,
                "criteria": [{"id": "AC1", "status": evidence_status}],
                "evidence_links": [{"relation": "SUPPORTS"}],
                "run_details": {"cost_usd": 0.25, "tool_calls": 12, "tokens": 5000},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_score_counts_only_verified_tasks(tmp_path):
    accepted = outcome_from_bundle(_bundle(tmp_path, "a", "ACCEPTED"), human_minutes=30)
    rejected = outcome_from_bundle(_bundle(tmp_path, "b", "REJECTED"), human_minutes=10)
    missing_evidence = outcome_from_bundle(
        _bundle(tmp_path, "c", "ACCEPTED", evidence_status="UNVERIFIED"), human_minutes=5
    )
    assert accepted.verified is True
    assert rejected.verified is False
    assert missing_evidence.verified is False
    result = score([accepted, rejected, missing_evidence])
    assert result["tasks"] == 3 and result["verified"] == 1
    assert result["rate"] == 0.333
    assert result["verified_per_human_hour"] == 2.0
    assert result["median_cost_usd"] == 0.25


def test_empty_corpus_is_honest():
    assert score([]) == {"tasks": 0, "verified": 0, "rate": 0.0}


def test_eval_cli_uses_real_bundles(tmp_path, capsys):
    root = _bundle(tmp_path, "cli-bundle", "COMPLETED_WITH_LIMITATIONS")
    base = ["--home", str(tmp_path / "home"), "--json"]
    code = main(base + ["init", "--project", "eval"])
    assert code == 0
    capsys.readouterr()
    code = main(base + ["eval", "score", str(root), "--human-minutes", "15"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["verified"] == 1

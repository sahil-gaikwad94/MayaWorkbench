"""Opt-in benchmark harness: corpus validation, offline dry-run, caps and metrics."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.evaluation.live import (
    ACCEPTED_DECISIONS,
    SCRIPTED_CALL_COST_USD,
    EvalFlags,
    TaskResult,
    build_scorecard,
    live_gate_status,
    run_eval,
)
from mayaworkbench.evaluation.tasks import (
    KNOWN_FAMILIES,
    bundled_seed_corpus,
    corpus_hash,
    load_corpus,
)

EXPECTED_SEED_HASH = "05d5bac8775ee3ba503f9da74bbe969bbe16b61de723901b409d967c452cc8b0"
SEED_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "mayaworkbench"
    / "evaluation"
    / "gold"
    / "eval_tasks_seed.json"
)
_KEY_ENV_VARS = ("MAYA_MODEL_API_KEY", "MAYA_ANTHROPIC_API_KEY", "MAYA_GEMINI_API_KEY")


# --------------------------------------------------------------------- corpus
def test_seed_corpus_has_25_valid_tasks_and_a_stable_hash():
    tasks = bundled_seed_corpus()
    assert len(tasks) == 25
    assert corpus_hash(tasks) == EXPECTED_SEED_HASH
    assert corpus_hash(bundled_seed_corpus()) == EXPECTED_SEED_HASH
    # Canonical hashing is independent of corpus order.
    assert corpus_hash(list(reversed(tasks))) == EXPECTED_SEED_HASH
    assert corpus_hash(load_corpus(SEED_PATH)) == EXPECTED_SEED_HASH

    families = Counter(task.family for task in tasks)
    assert set(families) == KNOWN_FAMILIES
    assert all(count == 5 for count in families.values())
    assert len({task.id for task in tasks}) == 25
    assert all(len(task.acceptance_criteria) >= 1 for task in tasks)
    assert all(task.is_seed_corpus for task in tasks)
    assert all("not an external benchmark" in task.provenance.lower() for task in tasks)
    assert all("tests/conftest.py" in task.fixture_ref for task in tasks)
    assert all(
        task.anti_gaming_probe == (task.family in {"adversarial", "eval_gaming"})
        for task in tasks
    )
    assert all(task.required_checks for task in tasks)
    assert all(task.max_spend_usd > 0 for task in tasks)


def test_load_corpus_rejects_invalid_corpora(tmp_path: Path):
    valid = json.loads(bundled_seed_corpus()[0].model_dump_json())

    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="cannot read task corpus"):
        load_corpus(missing)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_corpus(broken)

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_corpus(empty)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps([valid, valid]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate task id"):
        load_corpus(duplicate)

    bad_family = tmp_path / "family.json"
    bad_family.write_text(
        json.dumps([{**valid, "family": "not_a_family"}]), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid schema"):
        load_corpus(bad_family)

    no_criteria = tmp_path / "criteria.json"
    no_criteria.write_text(
        json.dumps([{**valid, "acceptance_criteria": []}]), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid schema"):
        load_corpus(no_criteria)

    extra_field = tmp_path / "extra.json"
    extra_field.write_text(json.dumps([{**valid, "surprise": 1}]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid schema"):
        load_corpus(extra_field)

    empty_dir = tmp_path / "empty-dir"
    empty_dir.mkdir()
    with pytest.raises(ValueError, match="no .json files"):
        load_corpus(empty_dir)


# -------------------------------------------------------------------- dry-run
def test_dry_run_runs_real_pipeline_offline(monkeypatch: pytest.MonkeyPatch):
    for name in (_KEY_ENV_VARS + ("MAYA_LIVE_EVAL", "MAYA_LIVE_SPEND_CAP_USD")):
        monkeypatch.delenv(name, raising=False)

    scorecard = run_eval(bundled_seed_corpus(), task_limit=2)

    assert scorecard.corpus_hash == EXPECTED_SEED_HASH
    assert scorecard.flags.dry_run is True
    assert scorecard.flags.live is False
    assert scorecard.counts.tasks == 2
    assert scorecard.counts.executed == 2
    assert scorecard.counts.skipped == 0
    assert scorecard.counts.failed == 0
    assert scorecard.results
    for result in scorecard.results:
        assert result.status == "EXECUTED"
        assert result.verification_report_present is True
        assert result.decision in ACCEPTED_DECISIONS
        assert result.evidence_complete is True
        assert result.required_evidence_missing == []
        assert result.tool_calls > 0
        assert result.model_tokens > 0
        assert result.cost_usd == pytest.approx(SCRIPTED_CALL_COST_USD * 4)
        assert result.run_id and result.verification_id
    assert scorecard.metrics.verified_completion_rate == 1.0
    assert scorecard.metrics.false_verification_rate == 0.0
    assert scorecard.metrics.total_spend_usd > 0
    _assert_metrics_from_results(scorecard)


def _assert_metrics_from_results(scorecard):  # type: ignore[no-untyped-def]
    """A tiny consistency check: totals and counts must come from the rows."""
    costs = [item.cost_usd for item in scorecard.results]
    assert scorecard.metrics.total_spend_usd == pytest.approx(sum(costs))
    assert scorecard.counts.executed == sum(
        1 for item in scorecard.results if item.status == "EXECUTED"
    )


def test_spend_cap_aborts_before_an_overspend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MAYA_LIVE_EVAL", raising=False)
    scorecard = run_eval(bundled_seed_corpus()[:1], max_spend_usd=0.001)

    assert scorecard.counts.executed == 0
    assert scorecard.counts.skipped == 1
    assert scorecard.counts.budget_exceeded == 1
    assert scorecard.budget_exceeded is True
    assert "BUDGET_EXCEEDED" in (scorecard.results[0].reason or "")
    assert scorecard.results[0].decision is None
    assert scorecard.metrics.verified_completion_rate == 0.0
    assert scorecard.metrics.total_spend_usd == 0.0
    assert any("insufficient_data" in note for note in scorecard.notes)
    assert scorecard.skip_breakdown


# -------------------------------------------------------------------- metrics
def _result(
    task_id: str,
    *,
    decision: str | None,
    evidence: bool = True,
    unsupported: list[str] | None = None,
    anti: bool = False,
    calls: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    status: str = "EXECUTED",
    reason: str | None = None,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        family="eval_gaming" if anti else "bug_fix",
        status=status,
        decision=decision,
        decision_class=decision,
        expected_decision_class="ACCEPTED",
        anti_gaming_probe=anti,
        evidence_complete=evidence,
        verification_report_present=evidence,
        required_evidence_missing=[] if evidence else ["AC1"],
        unsupported_claims=unsupported or [],
        tool_calls=calls,
        model_tokens=tokens,
        cost_usd=cost,
        reason=reason,
    )


def test_metrics_arithmetic_from_deliberately_mixed_outcomes():
    rows = [
        _result("r1", decision="ACCEPTED", calls=2, tokens=100, cost=0.01),
        _result("r2", decision="COMPLETED_WITH_LIMITATIONS", calls=4, tokens=200, cost=0.03),
        # ACCEPTED but missing evidence: a false verification, never dropped.
        _result("r3", decision="ACCEPTED", evidence=False, unsupported=["AC1"],
                calls=6, tokens=300, cost=0.05),
        _result("r4", decision="REJECTED", calls=8, tokens=400, cost=0.07),
        # Two anti-gaming probes: one caught (refusal), one not (accepted).
        _result("r5", decision="INCONCLUSIVE", unsupported=["AC2"], anti=True,
                calls=10, tokens=500, cost=0.09),
        _result("r6", decision="ACCEPTED", anti=True, calls=12, tokens=600, cost=0.11),
        _result("r7", decision=None, status="SKIPPED", reason="LIVE_GATES_NOT_MET"),
        _result("r8", decision=None, status="SKIPPED", reason="BUDGET_EXCEEDED: cap"),
    ]
    scorecard = build_scorecard(
        corpus_fingerprint="mixed",
        flags=EvalFlags(live=False, dry_run=True),
        results=rows,
    )

    assert scorecard.counts.tasks == 8
    assert scorecard.counts.executed == 6
    assert scorecard.counts.skipped == 2
    assert scorecard.counts.accepted == 4
    assert scorecard.counts.verified == 3
    assert scorecard.counts.false_verification == 1
    assert scorecard.counts.unsupported_claims == 2
    assert scorecard.counts.anti_gaming_total == 2
    assert scorecard.counts.anti_gaming_caught == 1
    assert scorecard.counts.budget_exceeded == 1

    assert scorecard.metrics.verified_completion_rate == 0.5  # 3 / 6 executed
    assert scorecard.metrics.unsupported_claim_rate == 0.3333  # 2 / 6
    assert scorecard.metrics.false_verification_rate == 0.25  # 1 / 4 accepted
    assert scorecard.metrics.anti_gaming_catch_rate == 0.5  # 1 / 2 probes
    assert scorecard.metrics.median_tool_calls == 7.0
    assert scorecard.metrics.median_model_tokens == 350.0
    assert scorecard.metrics.median_cost_usd == 0.06
    assert scorecard.metrics.total_spend_usd == pytest.approx(0.36)
    assert scorecard.skip_breakdown == {"BUDGET_EXCEEDED: cap": 1, "LIVE_GATES_NOT_MET": 1}
    assert scorecard.budget_exceeded is True
    assert scorecard.notes == []


def test_zero_denominators_are_zero_with_explicit_notes():
    scorecard = build_scorecard(
        corpus_fingerprint="empty",
        flags=EvalFlags(live=True, dry_run=False),
        results=[_result("s1", decision=None, status="SKIPPED", reason="no gates")],
    )
    metrics = scorecard.metrics
    assert metrics.verified_completion_rate == 0.0
    assert metrics.unsupported_claim_rate == 0.0
    assert metrics.false_verification_rate == 0.0
    assert metrics.anti_gaming_catch_rate == 0.0
    assert metrics.median_tool_calls == 0.0
    assert len(scorecard.notes) >= 4
    assert all("insufficient_data" in note for note in scorecard.notes)


# ------------------------------------------------------------------ live gates
class _SentinelProvider:
    name = "sentinel"
    model = "sentinel"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self):  # type: ignore[no-untyped-def]
        raise AssertionError("live gates missing: no provider may be constructed")

    def generate(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("live gates missing: no model call may be made")


def test_live_mode_skips_without_all_gates(monkeypatch: pytest.MonkeyPatch):
    for name in (_KEY_ENV_VARS + ("MAYA_LIVE_EVAL", "MAYA_LIVE_SPEND_CAP_USD")):
        monkeypatch.delenv(name, raising=False)
    sentinel = _SentinelProvider()

    scorecard = run_eval(bundled_seed_corpus()[:2], live=True, provider=sentinel)

    assert sentinel.calls == 0
    assert scorecard.flags.live is True
    assert scorecard.flags.gate_reasons
    assert scorecard.counts.executed == 0
    assert scorecard.counts.skipped == 2
    assert all("LIVE_GATES_NOT_MET" in (item.reason or "") for item in scorecard.results)
    assert scorecard.metrics.verified_completion_rate == 0.0
    assert any("insufficient_data" in note for note in scorecard.notes)


def test_live_gates_require_all_three(monkeypatch: pytest.MonkeyPatch):
    for name in (_KEY_ENV_VARS + ("MAYA_LIVE_EVAL", "MAYA_LIVE_SPEND_CAP_USD")):
        monkeypatch.delenv(name, raising=False)
    ok, reasons = live_gate_status()
    assert not ok and len(reasons) >= 3

    monkeypatch.setenv("MAYA_LIVE_EVAL", "1")
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "sentinel-secret-value")
    monkeypatch.setenv("MAYA_LIVE_SPEND_CAP_USD", "0.10")
    ok, reasons = live_gate_status()
    assert ok and reasons == []
    # A CLI cap also satisfies the spend gate.
    monkeypatch.delenv("MAYA_LIVE_SPEND_CAP_USD")
    ok, _reasons = live_gate_status(0.05)
    assert ok


# ------------------------------------------------------------------------- CLI
def _cli(capsys, args):  # type: ignore[no-untyped-def]
    code = main(args)
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        if stream.strip().startswith("{"):
            return code, json.loads(stream), captured
    return code, None, captured


def test_cli_tasks_round_trip(capsys, tmp_path: Path):
    home = str(tmp_path / "home")
    code, payload, captured = _cli(capsys, ["--home", home, "--json", "eval", "tasks"])
    assert code == 0, captured.err
    assert payload["count"] == 25
    assert payload["corpus_hash"] == EXPECTED_SEED_HASH
    assert payload["tasks"][0]["id"] == "eval-bugfix-01"

    # The subcommand-level --json flag also works.
    code, payload, captured = _cli(capsys, ["--home", home, "eval", "tasks", "--json"])
    assert code == 0, captured.err
    assert payload["corpus_hash"] == EXPECTED_SEED_HASH

    custom = tmp_path / "corpus.json"
    custom.write_text(json.dumps([json.loads(bundled_seed_corpus()[0].model_dump_json())]),
                      encoding="utf-8")
    code, payload, captured = _cli(
        capsys, ["--home", home, "--json", "eval", "tasks", "--corpus", str(custom)]
    )
    assert code == 0 and payload["count"] == 1


def test_cli_run_writes_scorecard_and_never_prints_keys(capsys, tmp_path: Path, monkeypatch):
    for name in (_KEY_ENV_VARS + ("MAYA_LIVE_EVAL", "MAYA_LIVE_SPEND_CAP_USD")):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "sentinel-secret-value")
    home = str(tmp_path / "home")
    out = tmp_path / "scorecard-out"

    code, payload, captured = _cli(
        capsys,
        [
            "--home", home, "--json", "eval", "run",
            "--task-limit", "1", "--out", str(out),
        ],
    )
    assert code == 0, captured.err
    assert payload["corpus_hash"] == EXPECTED_SEED_HASH
    assert payload["flags"]["dry_run"] is True
    assert payload["counts"]["executed"] == 1
    assert "sentinel-secret-value" not in captured.out
    assert "sentinel-secret-value" not in captured.err

    written = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    assert written["corpus_hash"] == EXPECTED_SEED_HASH
    markdown = (out / "scorecard.md").read_text(encoding="utf-8")
    assert "# MayaWorkbench evaluation scorecard" in markdown
    assert "sentinel-secret-value" not in markdown


def test_cli_live_without_gates_is_usage_error(capsys, tmp_path: Path, monkeypatch):
    for name in (_KEY_ENV_VARS + ("MAYA_LIVE_EVAL", "MAYA_LIVE_SPEND_CAP_USD")):
        monkeypatch.delenv(name, raising=False)
    code, payload, captured = _cli(
        capsys,
        ["--home", str(tmp_path / "home"), "--json", "eval", "run", "--live"],
    )
    assert code == 2
    assert payload["code"] == "USAGE_ERROR"
    assert "MAYA_LIVE_EVAL" in payload["error"]
    assert "MAYA_LIVE_SPEND_CAP_USD" in payload["error"]


# ------------------------------------------------------------------- WP3
def test_provider_profiles_are_frozen_and_secret_free():
    from mayaworkbench.evaluation.providers import (
        default_profiles,
        resolve_profile,
    )

    profiles = default_profiles()
    assert len(profiles) >= 2
    assert len({item.provider for item in profiles}) >= 2
    assert len({item.id for item in profiles}) == len(profiles)
    assert all(item.model_id and item.harness_version for item in profiles)
    for item in profiles:
        public = item.public_dict()
        assert not any("key" in name.lower() for name in public)  # no key *field*
        assert not any(token in json.dumps(public).lower()
                       for token in ("sk-", "ghp_", "bearer "))  # no key *value*
    assert resolve_profile(profiles[0].id) == profiles[0]
    with pytest.raises(Exception):
        resolve_profile("does-not-exist")


def test_wilson_interval_and_family_breakdown():
    from mayaworkbench.evaluation.metrics import wilson_interval

    lo, hi = wilson_interval(8, 10)
    assert 0 < lo < 0.8 < hi < 1
    assert wilson_interval(0, 0) == (0.0, 0.0)
    assert wilson_interval(0, 10)[1] > 0  # zero successes still has an upper bound

    results = [
        TaskResult(task_id="a", family="bug_fix", expected_decision_class="ACCEPTED",
                   decision="ACCEPTED", evidence_complete=True, verification_report_present=True),
        TaskResult(task_id="b", family="bug_fix", expected_decision_class="ACCEPTED",
                   decision="REJECTED"),
        TaskResult(task_id="c", family="adversarial", expected_decision_class="REJECTED",
                   decision="REJECTED", anti_gaming_probe=True),
    ]
    flags = EvalFlags(live=False, dry_run=True)
    card = build_scorecard(corpus_fingerprint="x", flags=flags, results=results)
    assert set(card.family_breakdown) == {"bug_fix", "adversarial"}
    assert card.family_breakdown["bug_fix"]["verified_rate"] == 0.5
    assert card.metrics.verified_completion_rate_ci[0] <= card.metrics.verified_completion_rate
    assert card.metrics.anti_gaming_catch_rate_ci[1] >= card.metrics.anti_gaming_catch_rate


def test_control_run_records_context_disabled(tmp_path):
    corpus = bundled_seed_corpus()[:2]
    direct = run_eval(corpus, task_limit=2)
    control = run_eval(corpus, task_limit=2, control_mode=True, trace=True)
    assert all(item.control_mode for item in control.results)
    assert not any(item.control_mode for item in direct.results)
    assert control.flags.control_mode is True
    assert all(item.trace.get("maya_context_disabled") is True for item in control.results)


def test_comparison_runs_two_profiles_and_control(tmp_path):
    from mayaworkbench.evaluation.live import run_comparison

    corpus = bundled_seed_corpus()[:1]
    report = run_comparison(corpus, task_limit=1, trace=True)
    assert len(report.arms) >= 2
    assert any(name.endswith("-control") for name in report.arms)
    assert all(arm.corpus_hash == report.corpus_hash for arm in report.arms.values())
    assert report.deltas  # at least one assisted/control delta


def test_model_safety_corpus_is_valid_and_scorable():
    from mayaworkbench.evaluation.live import load_model_safety_corpus

    corpus = load_model_safety_corpus()
    assert len(corpus) >= 5
    assert all(item.anti_gaming_probe for item in corpus)
    assert {item.family for item in corpus} <= KNOWN_FAMILIES
    card = run_eval(corpus, task_limit=2, trace=True)
    assert card.counts.anti_gaming_total >= 2
    assert card.metrics.anti_gaming_catch_rate_ci


def test_cli_eval_providers_and_compare(capsys, tmp_path):
    from mayaworkbench.cli.main import main

    code, payload, captured = _cli(capsys, ["--home", str(tmp_path / "home"), "--json", "eval", "providers"])
    assert code == 0, captured.err
    assert len(payload["profiles"]) >= 2

    out = tmp_path / "cards"
    for name, extra in (("assisted", []), ("control", ["--control"])):
        code, _, captured = _cli(
            capsys,
            ["--home", str(tmp_path / "home"), "--json", "eval", "run",
             "--task-limit", "1", "--out", str(out / name), *extra],
        )
        assert code == 0, captured.err
    code, payload, captured = _cli(
        capsys,
        ["--home", str(tmp_path / "home"), "--json", "eval", "compare",
         str(out / "assisted" / "scorecard.json"), str(out / "control" / "scorecard.json")],
    )
    assert code == 0, captured.err
    assert "assisted" in payload["arms"] and "control" in payload["arms"]
    assert "control" in payload["deltas_from_first"]

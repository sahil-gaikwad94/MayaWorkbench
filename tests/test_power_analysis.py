"""Tests for experiment power / sample-size planning."""

from __future__ import annotations

import math

import pytest

from mayaworkbench.experiments.power import (
    PowerAnalysis,
    minimum_detectable_effect,
    power_independent,
    power_paired,
    required_seeds_independent,
    required_seeds_paired,
)


def test_power_paired_increases_with_n() -> None:
    values = [power_paired(n, 0.5) for n in (2, 5, 10, 30, 100)]
    assert all(b >= a for a, b in zip(values, values[1:]))
    assert values[0] < values[-1]


def test_required_seeds_paired_is_deterministic_and_achieves_target() -> None:
    analysis = required_seeds_paired(0.5, alpha=0.05, target_power=0.80, max_seeds=200)
    assert isinstance(analysis, PowerAnalysis)
    assert analysis.design == "paired"
    assert analysis.achievable is True
    assert analysis.required_seeds >= 2
    assert power_paired(analysis.required_seeds, 0.5) >= 0.80
    # one fewer seed must not yet reach the target (minimality)
    if analysis.required_seeds > 2:
        assert power_paired(analysis.required_seeds - 1, 0.5) < 0.80


def test_required_seeds_paired_small_effect_needs_more_seeds() -> None:
    small = required_seeds_paired(0.2, target_power=0.80, max_seeds=2000)
    large = required_seeds_paired(0.8, target_power=0.80, max_seeds=2000)
    assert small.required_seeds > large.required_seeds


def test_required_seeds_paired_unachievable_is_honest() -> None:
    analysis = required_seeds_paired(0.05, target_power=0.99, max_seeds=5)
    assert analysis.achievable is False
    assert analysis.required_seeds == 5
    assert "not reached" in " ".join(analysis.notes)


def test_required_seeds_paired_validates_inputs() -> None:
    with pytest.raises(ValueError):
        required_seeds_paired(0.0)
    with pytest.raises(ValueError):
        required_seeds_paired(0.5, alpha=0.0)
    with pytest.raises(ValueError):
        required_seeds_paired(0.5, target_power=1.0)
    with pytest.raises(ValueError):
        required_seeds_paired(0.5, max_seeds=1)


def test_power_independent_needs_more_than_paired_for_same_d() -> None:
    # independent design is less efficient than paired for the same effect size
    ind = required_seeds_independent(0.5, target_power=0.80, max_seeds=2000)
    paired = required_seeds_paired(0.5, target_power=0.80, max_seeds=2000)
    assert ind.required_seeds >= paired.required_seeds


def test_power_independent_ratio_changes_arm_sizes() -> None:
    a = power_independent(10, 0.5, ratio=1.0)
    b = power_independent(10, 0.5, ratio=2.0)
    # more candidate runs should not reduce power
    assert b >= a - 1e-9


def test_minimum_detectable_effect_inverts_required_seeds() -> None:
    n = 34
    mde = minimum_detectable_effect(n, alpha=0.05, target_power=0.80, design="paired")
    assert mde > 0.0
    # the MDE for n should be no larger than the effect we planned for
    analysis = required_seeds_paired(0.5, target_power=0.80, max_seeds=200)
    assert minimum_detectable_effect(
        analysis.required_seeds, alpha=0.05, target_power=0.80, design="paired"
    ) <= 0.5 + 1e-9


def test_minimum_detectable_effect_rejects_bad_design() -> None:
    with pytest.raises(ValueError):
        minimum_detectable_effect(10, design="nope")
    assert minimum_detectable_effect(1, design="paired") == 0.0


def test_as_dict_is_json_friendly() -> None:
    analysis = required_seeds_paired(0.5)
    payload = analysis.as_dict()
    assert payload["design"] == "paired"
    assert isinstance(payload["notes"], list)


def test_zero_effect_returns_alpha_rate() -> None:
    # no effect => power collapses to the type-I rate
    assert power_paired(30, 0.0) == pytest.approx(0.05, abs=1e-9)
    assert power_independent(30, 0.0) == pytest.approx(0.05, abs=1e-9)


def test_cli_power_command(tmp_path, capsys) -> None:
    from mayaworkbench.cli.main import main as cli_main

    rc = cli_main(["--json", "experiment", "power", "--effect-size", "0.5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"required_seeds"' in out
    assert '"achievable": true' in out

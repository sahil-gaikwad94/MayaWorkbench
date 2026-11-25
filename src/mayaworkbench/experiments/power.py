"""Sample-size and power planning for experiment comparisons (researcher tooling).

The Experiment Lab records real runs and compares them with paired/Welch t tests,
Hedges' g and Holm-Bonferroni adjustment. What it did *not* do before this
module was answer the planning question the other way round: "given the effect I
want to detect, how many seeds (runs) do I need?" ``budget_runs`` only caps a
definition; it never told the user whether the cap was enough.

This module provides that planning arithmetic. It is deliberately approximate
and says so:

* power is computed with a normal approximation to the non-central t
  distribution, using the existing Student's t quantile for the critical value.
  It is accurate enough for planning and is never presented as an exact
  probability of detecting a real effect.
* inputs are Cohen's d (standardised mean difference), not a raw metric delta.
  The user converts their practically important difference into d using the
  observed or expected standard deviation of the paired differences.
* paired and independent (two-arm) designs are both supported; the design must
  be declared, because the required n differs by roughly a factor of two.
* ``max_seeds`` caps the search so a request for an undetectable effect does
  not loop forever; an unmet target is reported as ``not_achievable`` rather than
  as a fabricated large number.

No database, no I/O, no model calls. The functions are pure and deterministic so
they can be unit-tested against hand computations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from mayaworkbench.experiments.stats import (
    DEFAULT_ALPHA,
    TWO_SIDED_95,
    student_t_quantile,
)

#: Hard ceiling on the search so planning stays bounded and fast.
MAX_SEEDS_CEILING = 100_000

#: Default target power when the caller does not state one.
DEFAULT_POWER = 0.80


def _normal_cdf(value: float) -> float:
    """CDF of the standard normal distribution (stdlib only)."""
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _z_quantile(p: float) -> float:
    """Standard normal quantile via the existing bisection idiom (no scipy)."""
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")
    if p == 0.5:
        return 0.0
    low, high = -8.0, 8.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if _normal_cdf(mid) < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


@dataclass(frozen=True)
class PowerAnalysis:
    """Result of a sample-size / power planning computation."""

    design: str
    effect_size: float
    alpha: float
    target_power: float
    required_seeds: int
    achieved_power: float
    critical_value: float
    achievable: bool
    method: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "design": self.design,
            "effect_size": self.effect_size,
            "alpha": self.alpha,
            "target_power": self.target_power,
            "required_seeds": self.required_seeds,
            "achieved_power": self.achieved_power,
            "critical_value": self.critical_value,
            "achievable": self.achievable,
            "method": self.method,
            "notes": list(self.notes),
        }


def _validate_inputs(effect_size: float, alpha: float, target_power: float, max_seeds: int) -> None:
    if not (effect_size > 0.0 and math.isfinite(effect_size)):
        raise ValueError("effect_size must be a positive finite number (Cohen's d)")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha must be in (0, 1)")
    if not (0.0 < target_power < 1.0):
        raise ValueError("target_power must be in (0, 1)")
    if max_seeds < 2:
        raise ValueError("max_seeds must be at least 2")
    if max_seeds > MAX_SEEDS_CEILING:
        raise ValueError(f"max_seeds must not exceed {MAX_SEEDS_CEILING}")


def power_paired(n: int, effect_size: float, *, alpha: float = DEFAULT_ALPHA) -> float:
    """Approximate power of a paired t test for ``n`` seeds and Cohen's d.

    Uses a normal approximation with the Student's t critical value for
    ``n - 1`` degrees of freedom. Returns a value in ``[0, 1]``.
    """
    if n < 2:
        return 0.0
    if effect_size <= 0.0:
        return alpha  # no effect => only the type-I rate
    critical = student_t_quantile(1.0 - alpha / 2.0, n - 1)
    # non-centrality on the t scale: d * sqrt(n)
    noncentrality = effect_size * math.sqrt(n)
    # two-sided power via the normal approximation
    power = _normal_cdf(noncentrality - critical) + _normal_cdf(-noncentrality - critical)
    return max(0.0, min(1.0, power))


def power_independent(
    n_per_arm: int, effect_size: float, *, alpha: float = DEFAULT_ALPHA, ratio: float = 1.0
) -> float:
    """Approximate power of a two-arm (independent) t test.

    ``n_per_arm`` is the size of the baseline arm; ``ratio`` scales the
    candidate arm (``n_candidate = round(n_per_arm * ratio)``). Equal arms
    (ratio = 1.0) is the common case and the default.
    """
    if n_per_arm < 2:
        return 0.0
    n_b = max(2, int(round(n_per_arm * ratio)))
    n_a = n_per_arm
    if effect_size <= 0.0:
        return alpha
    # Welch-ish degrees of freedom for the critical value
    df = n_a + n_b - 2
    if df < 1:
        df = 1.0
    critical = student_t_quantile(1.0 - alpha / 2.0, df)
    # non-centrality for independent design: d * sqrt(n_a * n_b / (n_a + n_b))
    noncentrality = effect_size * math.sqrt((n_a * n_b) / (n_a + n_b))
    power = _normal_cdf(noncentrality - critical) + _normal_cdf(-noncentrality - critical)
    return max(0.0, min(1.0, power))


def required_seeds_paired(
    effect_size: float,
    *,
    alpha: float = DEFAULT_ALPHA,
    target_power: float = DEFAULT_POWER,
    max_seeds: int = 1000,
) -> PowerAnalysis:
    """Smallest n such that a paired t test reaches ``target_power``.

    Searches upward from 2. Returns ``achievable=False`` with
    ``required_seeds=max_seeds`` if the target is not met within the ceiling.
    """
    _validate_inputs(effect_size, alpha, target_power, max_seeds)
    notes: list[str] = [
        "Power is a normal approximation to the non-central t distribution; "
        "it is a planning estimate, not an exact probability.",
        "effect_size is Cohen's d on the paired differences; convert a raw "
        "metric delta to d using the expected standard deviation of the "
        "differences.",
    ]
    best_n = max_seeds
    best_power = power_paired(2, effect_size, alpha=alpha)
    achievable = False
    for n in range(2, max_seeds + 1):
        p = power_paired(n, effect_size, alpha=alpha)
        if p >= target_power:
            best_n = n
            best_power = p
            achievable = True
            break
        best_power = p
    critical = student_t_quantile(1.0 - alpha / 2.0, max(1, best_n - 1))
    if not achievable:
        notes.append(
            f"target power {target_power:.2f} was not reached within "
            f"{max_seeds} seeds (best achieved {best_power:.3f}); the required "
            "effect may be too small or the noise too large for this budget."
        )
    return PowerAnalysis(
        design="paired",
        effect_size=effect_size,
        alpha=alpha,
        target_power=target_power,
        required_seeds=best_n if achievable else max_seeds,
        achieved_power=best_power,
        critical_value=critical,
        achievable=achievable,
        method="normal_approximation_to_noncentral_t",
        notes=tuple(notes),
    )


def required_seeds_independent(
    effect_size: float,
    *,
    alpha: float = DEFAULT_ALPHA,
    target_power: float = DEFAULT_POWER,
    max_seeds: int = 1000,
    ratio: float = 1.0,
) -> PowerAnalysis:
    """Smallest per-arm n for a two-arm test to reach ``target_power``."""
    _validate_inputs(effect_size, alpha, target_power, max_seeds)
    if ratio <= 0.0 or not math.isfinite(ratio):
        raise ValueError("ratio must be a positive finite number")
    notes: list[str] = [
        "Power is a normal approximation to the non-central t distribution; "
        "it is a planning estimate, not an exact probability.",
        "effect_size is Cohen's d between the two arms; convert a raw metric "
        "delta to d using the pooled standard deviation.",
    ]
    if ratio != 1.0:
        notes.append(
            f"candidate arm is {ratio:.3g}x the size of the baseline arm; "
            "the required n refers to the baseline arm."
        )
    best_n = max_seeds
    best_power = power_independent(2, effect_size, alpha=alpha, ratio=ratio)
    achievable = False
    for n in range(2, max_seeds + 1):
        p = power_independent(n, effect_size, alpha=alpha, ratio=ratio)
        if p >= target_power:
            best_n = n
            best_power = p
            achievable = True
            break
        best_power = p
    df = best_n + max(2, int(round(best_n * ratio))) - 2
    critical = student_t_quantile(1.0 - alpha / 2.0, max(1, df))
    if not achievable:
        notes.append(
            f"target power {target_power:.2f} was not reached within "
            f"{max_seeds} seeds per arm (best achieved {best_power:.3f})."
        )
    return PowerAnalysis(
        design="independent",
        effect_size=effect_size,
        alpha=alpha,
        target_power=target_power,
        required_seeds=best_n if achievable else max_seeds,
        achieved_power=best_power,
        critical_value=critical,
        achievable=achievable,
        method="normal_approximation_to_noncentral_t",
        notes=tuple(notes),
    )


def minimum_detectable_effect(
    n: int,
    *,
    alpha: float = DEFAULT_ALPHA,
    target_power: float = DEFAULT_POWER,
    design: str = "paired",
    ratio: float = 1.0,
) -> float:
    """Cohen's d detectable at ``target_power`` with ``n`` seeds.

    Inverts the power formula: the smallest standardised difference that the
    design can detect at the requested power. Returns 0.0 for degenerate input.
    """
    if n < 2 or not (0.0 < alpha < 1.0) or not (0.0 < target_power < 1.0):
        return 0.0
    if design == "paired":
        critical = student_t_quantile(1.0 - alpha / 2.0, n - 1)
        z_power = _z_quantile(target_power)
        return (critical + z_power) / math.sqrt(n)
    if design == "independent":
        n_b = max(2, int(round(n * ratio)))
        df = n + n_b - 2
        critical = student_t_quantile(1.0 - alpha / 2.0, max(1, df))
        z_power = _z_quantile(target_power)
        return (critical + z_power) / math.sqrt((n * n_b) / (n + n_b))
    raise ValueError(f"unknown design {design!r}; use 'paired' or 'independent'")


__all__ = [
    "PowerAnalysis",
    "DEFAULT_POWER",
    "MAX_SEEDS_CEILING",
    "minimum_detectable_effect",
    "power_independent",
    "power_paired",
    "required_seeds_independent",
    "required_seeds_paired",
]

"""Small-sample statistics for experiment comparisons, without scipy.

Why hand-written: the workbench has exactly one runtime dependency (pydantic)
and must keep installation predictable. The functions here are the subset a
baseline/candidate comparison needs, and each one states its assumption.

What is implemented:

* descriptive statistics (mean, median, sample standard deviation);
* Student's t distribution CDF and quantile via the regularized incomplete
  beta function (Numerical Recipes continued-fraction method);
* paired t-test on within-seed differences, and Welch's t-test for unpaired
  samples (unequal variances, Welch-Satterthwaite degrees of freedom);
* 95% confidence intervals for the mean difference;
* Hedges' g with the small-sample correction J = 1 - 3/(4*df - 1) and a
  t-based interval, for paired and independent designs;
* Holm-Bonferroni step-down adjustment for secondary metrics.

Honest limitations are encoded, not hidden:

* with ``n < 2`` no test or interval is produced (``None`` is returned);
* zero variance produces no t statistic instead of dividing by zero;
* a p-value is evidence under assumptions, not proof of an effect, and the
  caller is expected to report the assumptions alongside it;
* multiple metrics are exploratory unless the primary metric was declared in
  advance and the secondary results are adjusted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

DEFAULT_ALPHA = 0.05
TWO_SIDED_95 = 0.975


# ------------------------------------------------------------------ descriptive
def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def sample_sd(values: list[float]) -> float | None:
    """Sample standard deviation (n-1 denominator). ``None`` when n < 2."""
    if len(values) < 2:
        return None
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def _is_constant(sd: float, scale: float) -> bool:
    """Whether spread is zero up to floating-point noise.

    A deterministic command often produces differences that are equal in exact
    arithmetic but differ by 1e-17 in binary floating point. Treating that as
    sampling variance yields a t statistic of 1e17 and p = 0, which is a
    fabricated certainty. The tolerance is relative to the observed magnitude.
    """
    return sd <= max(1.0e-12, 1.0e-12 * abs(scale))


# ------------------------------------------------------- incomplete beta / t
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    max_iterations = 300
    epsilon = 3.0e-14
    tiny = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b), the regularized incomplete beta function."""
    if not (0.0 <= x <= 1.0):
        raise ValueError("x must be in [0, 1]")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    """CDF of Student's t distribution with ``df`` degrees of freedom."""
    if df <= 0:
        raise ValueError("degrees of freedom must be positive")
    if math.isinf(t):
        return 0.0 if t < 0 else 1.0
    tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t >= 0 else tail


def student_t_quantile(p: float, df: float) -> float:
    """Quantile of Student's t distribution, by bisection on the CDF.

    Bisection is slower than Newton but deterministic and free of derivative
    pitfalls; 200 iterations on a bounded interval exceed double precision.
    """
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")
    if p == 0.5:
        return 0.0
    low, high = -1.0e6, 1.0e6
    for _ in range(200):
        mid = (low + high) / 2.0
        if student_t_cdf(mid, df) < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def two_sided_p_value(t: float, df: float) -> float:
    if df <= 0:
        return 1.0
    return max(0.0, min(1.0, 2.0 * (1.0 - student_t_cdf(abs(t), df))))


# ---------------------------------------------------------------------- tests
@dataclass
class TTestResult:
    """Result of a t test. Optional fields are ``None`` when unsupported."""

    method: str
    n_a: int
    n_b: int
    mean_a: float | None
    mean_b: float | None
    difference: float | None
    ci_low: float | None
    ci_high: float | None
    t_statistic: float | None
    degrees_of_freedom: float | None
    p_value: float | None
    notes: list[str] = field(default_factory=list)


def paired_t(differences: list[float], *, confidence: float = TWO_SIDED_95) -> TTestResult:
    """Paired t test on ``differences`` (candidate - baseline), two-sided."""
    n = len(differences)
    average = mean(differences)
    sd = sample_sd(differences)
    result = TTestResult(
        method="paired_t",
        n_a=n,
        n_b=n,
        mean_a=None,
        mean_b=None,
        difference=average,
        ci_low=None,
        ci_high=None,
        t_statistic=None,
        degrees_of_freedom=None,
        p_value=None,
    )
    if n < 2 or average is None or sd is None:
        result.notes.append("a paired test needs at least two pairs")
        return result
    result.degrees_of_freedom = n - 1
    if _is_constant(sd, average):
        result.notes.append(
            "all paired differences are identical up to floating-point precision; "
            "the t statistic is undefined, so no p-value or interval is reported"
        )
        result.ci_low = result.ci_high = average
        return result
    standard_error = sd / math.sqrt(n)
    t_statistic = average / standard_error
    critical = student_t_quantile(confidence, n - 1)
    result.t_statistic = t_statistic
    result.p_value = two_sided_p_value(t_statistic, n - 1)
    result.ci_low = average - critical * standard_error
    result.ci_high = average + critical * standard_error
    return result


def welch_t(a: list[float], b: list[float], *, confidence: float = TWO_SIDED_95) -> TTestResult:
    """Welch's unpaired t test (candidate ``a`` minus baseline ``b``)."""
    n_a, n_b = len(a), len(b)
    mean_a, mean_b = mean(a), mean(b)
    diff = (mean_a - mean_b) if mean_a is not None and mean_b is not None else None
    result = TTestResult(
        method="welch_t",
        n_a=n_a,
        n_b=n_b,
        mean_a=mean_a,
        mean_b=mean_b,
        difference=diff,
        ci_low=None,
        ci_high=None,
        t_statistic=None,
        degrees_of_freedom=None,
        p_value=None,
    )
    if n_a < 2 or n_b < 2 or diff is None:
        result.notes.append("an unpaired test needs at least two runs per arm")
        return result
    sd_a, sd_b = sample_sd(a), sample_sd(b)
    assert sd_a is not None and sd_b is not None
    var_a, var_b = sd_a * sd_a / n_a, sd_b * sd_b / n_b
    standard_error_sq = var_a + var_b
    if _is_constant(math.sqrt(standard_error_sq), diff):
        result.notes.append(
            "both arms have zero variance up to floating-point precision; the t "
            "statistic is undefined, so no p-value or interval is reported"
        )
        result.ci_low = result.ci_high = diff
        return result
    standard_error = math.sqrt(standard_error_sq)
    denominator = 0.0
    if n_a > 1:
        denominator += (var_a * var_a) / (n_a - 1)
    if n_b > 1:
        denominator += (var_b * var_b) / (n_b - 1)
    df = (standard_error_sq * standard_error_sq) / denominator if denominator else 0.0
    if df <= 0:
        result.notes.append("Welch degrees of freedom could not be computed")
        return result
    t_statistic = diff / standard_error
    critical = student_t_quantile(confidence, df)
    result.degrees_of_freedom = df
    result.t_statistic = t_statistic
    result.p_value = two_sided_p_value(t_statistic, df)
    result.ci_low = diff - critical * standard_error
    result.ci_high = diff + critical * standard_error
    return result


# ---------------------------------------------------------------- effect size
@dataclass
class EffectSize:
    value: float | None
    ci_low: float | None
    ci_high: float | None
    method: str
    notes: list[str] = field(default_factory=list)


def _hedges_correction(df: float) -> float:
    """Hedges' small-sample correction J = 1 - 3/(4*df - 1)."""
    if df <= 1:
        return 1.0
    return 1.0 - 3.0 / (4.0 * df - 1.0)


def hedges_g_independent(
    a: list[float], b: list[float], *, confidence: float = TWO_SIDED_95
) -> EffectSize:
    """Hedges' g for two independent samples (``a`` minus ``b``)."""
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return EffectSize(None, None, None, "hedges_g", ["needs two runs per arm"])
    mean_a, mean_b = mean(a), mean(b)
    sd_a, sd_b = sample_sd(a), sample_sd(b)
    assert mean_a is not None and mean_b is not None
    assert sd_a is not None and sd_b is not None
    df = n_a + n_b - 2
    pooled = math.sqrt(
        ((n_a - 1) * sd_a * sd_a + (n_b - 1) * sd_b * sd_b) / df
    )
    if _is_constant(pooled, mean_a - mean_b):
        return EffectSize(
            None, None, None, "hedges_g", ["pooled variance is zero to precision"]
        )
    difference = mean_a - mean_b
    d = difference / pooled
    correction = _hedges_correction(df)
    g = correction * d
    standard_error = math.sqrt((n_a + n_b) / (n_a * n_b) + (g * g) / (2.0 * df))
    critical = student_t_quantile(confidence, df)
    return EffectSize(
        value=g,
        ci_low=g - critical * standard_error,
        ci_high=g + critical * standard_error,
        method="hedges_g_independent",
    )


def hedges_g_paired(
    differences: list[float], *, confidence: float = TWO_SIDED_95
) -> EffectSize:
    """Hedges' g_z for paired differences (``candidate - baseline``)."""
    n = len(differences)
    average, sd = mean(differences), sample_sd(differences)
    if n < 2 or average is None or sd is None:
        return EffectSize(None, None, None, "hedges_gz", ["needs two pairs"])
    if _is_constant(sd, average):
        return EffectSize(
            None, None, None, "hedges_gz", ["difference variance is zero to precision"]
        )
    dz = average / sd
    correction = _hedges_correction(n - 1)
    g = correction * dz
    standard_error = correction * math.sqrt(1.0 / n + (dz * dz) / (2.0 * n))
    critical = student_t_quantile(confidence, n - 1)
    return EffectSize(
        value=g,
        ci_low=g - critical * standard_error,
        ci_high=g + critical * standard_error,
        method="hedges_gz",
    )


# ---------------------------------------------------------- multiple testing
def holm_adjust(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni step-down adjusted p-values, order preserved."""
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [1.0] * m
    running = 0.0
    for rank, (index, p_value) in enumerate(indexed):
        candidate = (m - rank) * p_value
        running = max(running, candidate)
        adjusted[index] = min(1.0, running)
    return adjusted


def summarize(values: list[float]) -> dict[str, float | int | None]:
    """Descriptive summary used in reports and comparison payloads."""
    return {
        "n": len(values),
        "mean": mean(values),
        "median": median(values),
        "sd": sample_sd(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


__all__ = [
    "DEFAULT_ALPHA",
    "mean",
    "median",
    "sample_sd",
    "regularized_incomplete_beta",
    "student_t_cdf",
    "student_t_quantile",
    "two_sided_p_value",
    "TTestResult",
    "paired_t",
    "welch_t",
    "EffectSize",
    "hedges_g_independent",
    "hedges_g_paired",
    "holm_adjust",
    "summarize",
]

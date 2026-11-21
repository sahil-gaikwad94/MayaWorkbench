"""Markdown evidence report for one experiment comparison.

The report is written for a technical reader who must decide whether the
comparison is trustworthy. Numbers are shown with their sample size, interval,
method and assumptions; the protocol diff is never hidden; and the
reproduction section states what is known and what is missing.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.domain.models import (
    ExperimentComparison,
    ExperimentDefinition,
    ExperimentManifest,
    ExperimentRun,
)
from mayaworkbench.experiments import stats


def _fmt(value: float | None, *, unit: str = "", digits: int = 4) -> str:
    if value is None:
        return "—"
    text = f"{value:.{digits}g}"
    return f"{text} {unit}".strip()


def _p(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.001:
        return "<0.001"
    return f"{value:.4f}"


def _seed_values(values: list[float]) -> str:
    return ", ".join(_fmt(value) for value in values) if values else "—"


def render_comparison_markdown(
    comparison: ExperimentComparison,
    *,
    baseline: ExperimentDefinition,
    candidate: ExperimentDefinition,
    runs: list[ExperimentRun],
    manifests: list[ExperimentManifest],
) -> str:
    primary = next(
        (item for item in comparison.metrics if item.primary),
        comparison.metrics[0] if comparison.metrics else None,
    )
    lines: list[str] = [
        f"# Experiment comparison: {candidate.name} vs {baseline.name}",
        "",
        f"- Verdict: **{comparison.verdict.value}**",
        f"- Comparable: **{'yes' if comparison.comparable else 'no'}**",
        f"- Design: **{comparison.design.value}**",
        f"- Baseline experiment: `{baseline.id}` ({baseline.role.value}, `{baseline.base_commit_sha[:12]}`)",
        f"- Candidate experiment: `{candidate.id}` ({candidate.role.value}, `{candidate.base_commit_sha[:12]}`)",
        f"- Comparison: `{comparison.id}`",
        f"- JSON sha256: `{comparison.json_sha256}`",
        "",
        "## What this result means, and what it does not",
        "",
    ]
    if primary is not None:
        lines.append(
            f"The primary metric is `{primary.key}` "
            f"({primary.direction.value.lower().replace('_', ' ')}"
            f"{', unit ' + primary.unit if primary.unit else ''})."
        )
    lines.extend(
        [
            "This is statistical evidence about these runs on this machine, under the "
            "protocol recorded below. It is not proof of a scientific claim, not a "
            "benchmark of the method in general, and not a substitute for a human "
            "reading the diff and the experiment code.",
            "",
            "## Protocol comparison",
            "",
            "| field | baseline | candidate | classification | note |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if comparison.findings:
        for finding in comparison.findings:
            baseline_value = finding.baseline if finding.baseline is not None else "—"
            candidate_value = finding.candidate if finding.candidate is not None else "—"
            lines.append(
                f"| `{finding.path}` | `{_escape(str(baseline_value))}` | "
                f"`{_escape(str(candidate_value))}` | `{finding.classification.value}` | "
                f"{_escape(finding.note)} |"
            )
    else:
        lines.append("| — | — | — | `MATCH` | the declared protocols are identical |")
    violations = [
        item
        for item in comparison.findings
        if item.classification.value == "FAIRNESS_VIOLATION"
    ]
    if violations:
        lines.extend(
            [
                "",
                "Fairness violations are recorded, not automatically dropped: the arms "
                "differ in ways that were not declared as the treatment or as acceptable "
                "variation, so the numbers below are not a fair comparison.",
            ]
        )

    lines.extend(
        [
            "",
            "## Metrics",
            "",
            "| metric | arm | n | mean | sd | median |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for metric in comparison.metrics:
        lines.append(
            f"| `{metric.key}`{ ' (primary)' if metric.primary else ''} | baseline | "
            f"{len(metric.baseline_values)} | {_fmt(metric.baseline_mean, unit=metric.unit)} | "
            f"{_fmt(metric.baseline_sd)} | {_fmt(metric.baseline_median, unit=metric.unit)} |"
        )
        lines.append(
            f"| `{metric.key}` | candidate | {len(metric.candidate_values)} | "
            f"{_fmt(metric.candidate_mean, unit=metric.unit)} | "
            f"{_fmt(metric.candidate_sd)} | {_fmt(metric.candidate_median, unit=metric.unit)} |"
        )
    lines.extend(
        [
            "",
            "| metric | difference (candidate − baseline) | 95% CI | method | t | df | p | adjusted p | Hedges g | favors |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for metric in comparison.metrics:
        ci = (
            f"[{_fmt(metric.ci_low)}, {_fmt(metric.ci_high)}]"
            if metric.ci_low is not None and metric.ci_high is not None
            else "—"
        )
        effect = (
            f"{_fmt(metric.effect_size)} "
            f"[{_fmt(metric.effect_size_ci_low)}, {_fmt(metric.effect_size_ci_high)}]"
            if metric.effect_size is not None
            else "—"
        )
        lines.append(
            f"| `{metric.key}` | {_fmt(metric.difference, unit=metric.unit)} | {ci} | "
            f"`{metric.method or '—'}` | {_fmt(metric.t_statistic)} | "
            f"{_fmt(metric.degrees_of_freedom, digits=3)} | {_p(metric.p_value)} | "
            f"{_p(metric.p_value_adjusted)} | {effect} | {metric.favors or '—'} |"
        )
    notes = [
        f"`{metric.key}`: {note}"
        for metric in comparison.metrics
        for note in metric.notes
    ]
    if notes:
        lines.extend(["", "### Statistical notes", ""])
        lines.extend(f"- {note}" for note in notes)

    lines.extend(["", "## Per-run observations", ""])
    lines.append("| metric | arm | values (one per usable run) |")
    lines.append("| --- | --- | --- |")
    for metric in comparison.metrics:
        lines.append(
            f"| `{metric.key}` | baseline | {_seed_values(metric.baseline_values)} |"
        )
        lines.append(
            f"| `{metric.key}` | candidate | {_seed_values(metric.candidate_values)} |"
        )

    if primary is not None and primary.within_margin is not None:
        lines.extend(
            [
                "",
                "## Non-inferiority",
                "",
                f"Declared margin: **{_fmt(candidate.non_inferiority_margin if candidate.non_inferiority_margin is not None else baseline.non_inferiority_margin, unit=primary.unit)}**; "
                f"result: **{'within margin' if primary.within_margin else 'outside margin'}** "
                "(one 95% interval on the primary metric; only this margin and only this metric).",
            ]
        )

    lines.extend(["", "## Limitations", ""])
    if comparison.limitations:
        lines.extend(f"- {item}" for item in comparison.limitations)
    else:
        lines.append("- None recorded. That is not proof of absence; inspect the protocol table.")

    lines.extend(["", "## Reproduce this comparison", ""])
    lines.extend(
        [
            f"- Baseline definition: `{baseline.id}` (sha256 `{baseline.content_sha256}`)",
            f"- Candidate definition: `{candidate.id}` (sha256 `{candidate.content_sha256}`)",
            f"- Baseline seeds: {', '.join(str(seed) for seed in baseline.seeds)}",
            f"- Candidate seeds: {', '.join(str(seed) for seed in candidate.seeds)}",
            f"- Baseline command: `{' '.join(baseline.command) or '(no command: imported metrics)'}`",
            f"- Candidate command: `{' '.join(candidate.command) or '(no command: imported metrics)'}`",
            f"- Metrics source: baseline `{baseline.metrics_source.kind.value}`, "
            f"candidate `{candidate.metrics_source.kind.value}`",
            "",
            "```bash",
            f"maya experiment approve {baseline.id}",
            f"maya experiment run {baseline.id}",
            f"maya experiment approve {candidate.id}",
            f"maya experiment run {candidate.id}",
            f"maya experiment compare {candidate.id} --baseline {baseline.id}",
            "```",
            "",
            "To re-run from the exported bundle, check `manifest.json` and the per-run "
            "manifests; each records the commit, patch hash, command, environment, "
            "limits and verified input hashes.",
        ]
    )

    if runs:
        lines.extend(
            [
                "",
                "## Recorded runs",
                "",
                "| run | arm | seed | attempt | status | metrics | duration | manifest sha256 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        baseline_ids = set(comparison.baseline_run_ids)
        for run in sorted(runs, key=lambda item: (item.experiment_id, item.seed, item.attempt)):
            arm = "baseline" if run.id in baseline_ids else "candidate"
            manifest = next(
                (item for item in manifests if item.id == run.manifest_id), None
            )
            lines.append(
                f"| `{run.id}` | {arm} | {run.seed} | {run.attempt} | `{run.status.value}` | "
                f"{'yes' if run.metrics_ingested else 'no'} | "
                f"{run.duration_ms if run.duration_ms is not None else '—'} ms | "
                f"`{(manifest.manifest_sha256 if manifest else '—')[:16]}` |"
            )

    if manifests:
        lines.extend(
            [
                "",
                "## Manifest hashes",
                "",
                "| experiment | seed | manifest sha256 | verified inputs |",
                "| --- | --- | --- | --- |",
            ]
        )
        for manifest in manifests:
            verified = ", ".join(
                f"{key}={value}"
                for key, value in sorted(manifest.verified_inputs.items())
                if value is not None
            ) or "—"
            lines.append(
                f"| `{manifest.experiment_id}` | {manifest.seed} | "
                f"`{manifest.manifest_sha256}` | `{_escape(verified)}` |"
            )

    lines.append("")
    return "\n".join(lines)


def render_summary(comparison: ExperimentComparison) -> str:
    """One-line summary used by CLI tables and verification findings."""
    primary = next((item for item in comparison.metrics if item.primary), None)
    if primary is None:
        return comparison.verdict.value
    return (
        f"{comparison.verdict.value} on {primary.key}: "
        f"{_fmt(primary.baseline_mean)} -> {_fmt(primary.candidate_mean)} "
        f"(diff {_fmt(primary.difference)}, 95% CI "
        f"[{_fmt(primary.ci_low)}, {_fmt(primary.ci_high)}], "
        f"n={len(primary.candidate_values)}/{len(primary.baseline_values)})"
    )


def statistics_payload(comparison: ExperimentComparison) -> dict[str, Any]:
    """Machine-readable summary, used by bundles and verification."""
    return {
        "verdict": comparison.verdict.value,
        "comparable": comparison.comparable,
        "design": comparison.design.value,
        "primary": next(
            (item.model_dump(mode="json") for item in comparison.metrics if item.primary),
            None,
        ),
        "metrics": [item.model_dump(mode="json") for item in comparison.metrics],
        "alpha": stats.DEFAULT_ALPHA,
    }


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


__all__ = ["render_comparison_markdown", "render_summary", "statistics_payload"]

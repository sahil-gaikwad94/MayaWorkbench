"""Protocol normalisation and fair-comparison classification (§14.3).

Two experiment arms may be compared only when the differences between them are
explained. This module flattens the declared protocol into dotted paths and
classifies every difference:

* ``EXPECTED_TREATMENT`` — the dimension the intervention intentionally changed
  (for example ``repository.patch`` or ``command``);
* ``ACCEPTABLE_VARIATION`` — nuisance variation the user declared acceptable
  (for example CPU count on a throughput benchmark), still shown in the report;
* ``FAIRNESS_VIOLATION`` — an undeclared difference in something that can
  change the measured outcome (dataset, splits, evaluation code, seeds, ...).

Unknown values (empty strings, unset fields) are reported as unknowns rather
than treated as equal. They do not by themselves invalidate a comparison, but
they always appear as limitations: the workbench cannot certify a field it
never recorded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from mayaworkbench.domain.enums import ProtocolComparison
from mayaworkbench.domain.models import (
    ExperimentDefinition,
    MetricSpec,
    ProtocolFinding,
    TREATMENT_DIMENSION_PATHS,
)
from mayaworkbench.errors import ExperimentError

#: Protocol paths whose mismatch (or shared unknown value) materially affects
#: whether a comparison is fair. Differences here must be declared, and an
#: unknown value here is reported as a limitation.
CORE_PATHS = frozenset(
    {
        "dataset.id",
        "dataset.fingerprint",
        "splits",
        "preprocessing.code_hash",
        "evaluation.script_hash",
        "model.identity",
        "model.checkpoint_hash",
        "environment.lockfile_hash",
        "environment.image_digest",
        "hardware.profile",
        "hardware.device",
        "determinism.seeded",
        "batch_size",
        "sequence_length",
    }
)

_MAX_VALUE_CHARS = 2000


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _clean(value: Any) -> Any:
    """Normalise a protocol value for comparison."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def protocol_values(
    definition: ExperimentDefinition,
    *,
    commit_sha: str,
    patch_hash: str | None,
) -> dict[str, Any]:
    """Flatten an experiment's protocol and code identity into dotted paths."""
    protocol = definition.protocol
    values: dict[str, Any] = {
        "command": list(definition.command),
        "repository.commit": commit_sha,
        "repository.patch": patch_hash,
        "dataset.id": protocol.dataset.id,
        "dataset.version": protocol.dataset.version,
        "dataset.fingerprint": protocol.dataset.fingerprint,
        "dataset.fingerprint_method": protocol.dataset.fingerprint_method,
        "splits": protocol.splits,
        "preprocessing.code_hash": protocol.preprocessing.code_hash,
        "preprocessing.description": protocol.preprocessing.description,
        "evaluation.script_path": protocol.evaluation.script_path,
        "evaluation.script_hash": protocol.evaluation.script_hash,
        "model.identity": protocol.model.identity,
        "model.checkpoint_hash": protocol.model.checkpoint_hash,
        "config.resolved": protocol.config.resolved,
        "config.hash": protocol.config.hash,
        "environment.lockfile_path": protocol.environment.lockfile_path,
        "environment.lockfile_hash": protocol.environment.lockfile_hash,
        "environment.image_digest": protocol.environment.image_digest,
        "environment.python_version": protocol.environment.python_version,
        "environment.framework_versions": protocol.environment.framework_versions,
        "hardware.profile": protocol.hardware.profile,
        "hardware.device": protocol.hardware.device,
        "hardware.cpu_count": protocol.hardware.cpu_count,
        "hardware.memory_mb": protocol.hardware.memory_mb,
        "determinism.seeded": protocol.determinism.seeded,
        "determinism.notes": protocol.determinism.notes,
        "batch_size": protocol.batch_size,
        "sequence_length": protocol.sequence_length,
        "stopping_rule": protocol.stopping_rule,
        "runtime_limit_seconds": protocol.runtime_limit_seconds,
    }
    return {path: _clean(value) for path, value in values.items()}


def validate_dimension(path: str, *, field_name: str) -> str:
    """Reject a mistyped treatment/variation path instead of ignoring it."""
    cleaned = path.strip()
    if not cleaned:
        raise ExperimentError(f"{field_name} contains an empty path")
    known = set(TREATMENT_DIMENSION_PATHS)
    if cleaned in known:
        return cleaned
    if any(item.startswith(cleaned + ".") for item in known):
        return cleaned
    raise ExperimentError(
        f"Unknown {field_name} path: {cleaned!r}",
        hint=(
            "Use a declared protocol path such as repository.patch, command, "
            "dataset.fingerprint, hardware.cpu_count, or a group prefix such as dataset."
        ),
    )


def _matches(path: str, declared: Iterable[str]) -> bool:
    for item in declared:
        if path == item or path.startswith(item + "."):
            return True
    return False


def compare_protocol(
    baseline: ExperimentDefinition,
    candidate: ExperimentDefinition,
    *,
    baseline_commit: str,
    candidate_commit: str,
    baseline_patch: str | None,
    candidate_patch: str | None,
) -> tuple[list[ProtocolFinding], list[str], bool]:
    """Return ``(findings, unknowns, comparable)`` for two experiment arms."""
    left = protocol_values(baseline, commit_sha=baseline_commit, patch_hash=baseline_patch)
    right = protocol_values(candidate, commit_sha=candidate_commit, patch_hash=candidate_patch)
    treatment = list(baseline.treatment_dimensions) + list(candidate.treatment_dimensions)
    variation = list(baseline.allowed_variation) + list(candidate.allowed_variation)
    findings: list[ProtocolFinding] = []
    unknowns: list[str] = []
    comparable = True

    for path in sorted(set(left) | set(right)):
        baseline_value = left.get(path)
        candidate_value = right.get(path)
        if baseline_value == candidate_value:
            if baseline_value is None and path in CORE_PATHS:
                unknowns.append(path)
                findings.append(
                    ProtocolFinding(
                        path=path,
                        classification=ProtocolComparison.MATCH,
                        baseline=None,
                        candidate=None,
                        note="both arms leave this field unknown; equality cannot be verified",
                    )
                )
            continue
        if _matches(path, treatment):
            classification = ProtocolComparison.EXPECTED_TREATMENT
            note = "declared as the intended treatment"
        elif _matches(path, variation):
            classification = ProtocolComparison.ACCEPTABLE_VARIATION
            note = "declared as acceptable variation"
        else:
            classification = ProtocolComparison.FAIRNESS_VIOLATION
            note = (
                "undeclared difference; declare it as a treatment dimension or "
                "acceptable variation, or make the arms match"
            )
            comparable = False
        findings.append(
            ProtocolFinding(
                path=path,
                classification=classification,
                baseline=_bounded(baseline_value),
                candidate=_bounded(candidate_value),
                note=note,
            )
        )

    findings.extend(_metric_definition_findings(baseline.metrics, candidate.metrics))
    if any(
        item.classification == ProtocolComparison.FAIRNESS_VIOLATION
        for item in findings
    ):
        comparable = False

    # A metric that only one arm declares cannot be compared at all.
    baseline_keys = {metric.key for metric in baseline.metrics}
    candidate_keys = {metric.key for metric in candidate.metrics}
    for key in sorted(baseline_keys ^ candidate_keys):
        unknowns.append(f"metrics.{key}")
    return findings, unknowns, comparable


def _metric_definition_findings(
    baseline: list[MetricSpec], candidate: list[MetricSpec]
) -> list[ProtocolFinding]:
    findings: list[ProtocolFinding] = []
    candidate_by_key = {metric.key: metric for metric in candidate}
    for metric in baseline:
        other = candidate_by_key.get(metric.key)
        if other is None:
            findings.append(
                ProtocolFinding(
                    path=f"metrics.{metric.key}",
                    classification=ProtocolComparison.FAIRNESS_VIOLATION,
                    baseline=metric.key,
                    candidate=None,
                    note="the candidate arm does not declare this metric",
                )
            )
            continue
        for field in ("direction", "unit", "aggregation"):
            left = getattr(metric, field)
            right = getattr(other, field)
            if left != right:
                findings.append(
                    ProtocolFinding(
                        path=f"metrics.{metric.key}.{field}",
                        classification=ProtocolComparison.FAIRNESS_VIOLATION,
                        baseline=left.value if hasattr(left, "value") else left,
                        candidate=right.value if hasattr(right, "value") else right,
                        note="metric definitions must match before comparison",
                    )
                )
    return findings


def _bounded(value: Any) -> Any:
    """Keep JSON evidence human-readable without hiding the shape of a diff."""
    if value is None:
        return None
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + "…"
    if isinstance(value, (list, dict)):
        text = canonical_json(value)
        if len(text) > _MAX_VALUE_CHARS:
            return text[:_MAX_VALUE_CHARS] + "…"
    return value


__all__ = [
    "CORE_PATHS",
    "canonical_json",
    "canonical_sha256",
    "protocol_values",
    "validate_dimension",
    "compare_protocol",
]

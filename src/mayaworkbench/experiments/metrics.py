"""Metric ingestion and replicate aggregation.

The metrics contract is deliberately small and explicit. A run emits either:

* a JSON document ``{"metrics": {"key": number, ...}}`` in a declared
  workspace-relative file (``JSON_FILE``);
* the same document as the last parseable stdout line (``STDOUT_JSON``);
* one such document per line, optionally with ``"step"`` or ``"sample"``
  (``STDOUT_JSONL``) for per-step curves.

Values must be finite numbers. Booleans are not numbers here (JSON ``true``
would otherwise become 1.0). Undeclared metric keys are stored as evidence and
reported as a finding, but they are never silently promoted into a comparison.

Metrics produced outside the workbench are ingested through
``MetricsSourceKind.IMPORTED``: the value is real, the execution is not ours,
and the run records that distinction.
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from mayaworkbench.domain.enums import MetricAggregation, MetricsSourceKind
from mayaworkbench.domain.models import MetricObservation, MetricSpec
from mayaworkbench.errors import ExperimentError

#: One raw value: ``(sample_index, step, value)``.
RawValue = tuple[int, float | None, float]


@dataclass
class ExtractedMetrics:
    values: dict[str, list[RawValue]] = field(default_factory=dict)
    source: str = ""
    undeclared: list[str] = field(default_factory=list)
    missing_declared: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_records: int = 0

    @property
    def total_observations(self) -> int:
        return sum(len(items) for items in self.values.values())


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _payload_metrics(payload: object, *, where: str) -> dict[str, float]:
    if not isinstance(payload, dict):
        raise ExperimentError(f"{where}: metrics payload must be a JSON object")
    metrics = payload.get("metrics")
    if metrics is None:
        raise ExperimentError(
            f"{where}: expected an object with a 'metrics' member",
            hint='Emit {"metrics": {"throughput": 1234.5}}.',
        )
    if not isinstance(metrics, dict) or not metrics:
        raise ExperimentError(f"{where}: 'metrics' must be a non-empty object")
    cleaned: dict[str, float] = {}
    for key, value in metrics.items():
        if not isinstance(key, str) or not key:
            raise ExperimentError(f"{where}: metric keys must be non-empty strings")
        if not _is_number(value):
            raise ExperimentError(
                f"{where}: metric {key!r} must be a number, found {type(value).__name__}"
            )
        number = float(value)
        if not math.isfinite(number):
            raise ExperimentError(
                f"{where}: metric {key!r} is not finite ({value!r})"
            )
        cleaned[key] = number
    return cleaned


def _optional_index(payload: object, name: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(name)
    if value is None:
        return None
    if not _is_number(value):
        raise ExperimentError(f"'{name}' must be a number when present")
    number = float(value)
    if not math.isfinite(number):
        raise ExperimentError(f"'{name}' must be finite")
    return number


@dataclass
class _Collector:
    declared: set[str]
    max_observations: int
    values: dict[str, list[RawValue]] = field(default_factory=dict)
    undeclared: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    raw_records: int = 0
    total: int = 0

    def add(self, metrics: dict[str, float], *, step: float | None, where: str) -> None:
        self.raw_records += 1
        for key, value in metrics.items():
            if not math.isfinite(value):
                raise ExperimentError(
                    f"{where}: metric {key!r} is not finite ({value!r})"
                )
            if key not in self.declared:
                self.undeclared.add(key)
            bucket = self.values.setdefault(key, [])
            if self.total >= self.max_observations:
                raise ExperimentError(
                    f"{where}: more than {self.max_observations} metric observations",
                    hint="Raise the experiment budget or emit fewer rows.",
                )
            bucket.append((len(bucket), step, value))
            self.total += 1


def extract_metrics(
    *,
    source: MetricsSourceKind,
    stdout: str,
    workspace: Path | None = None,
    path: str | None = None,
    metrics: list[MetricSpec],
    max_observations: int,
    max_file_bytes: int,
) -> ExtractedMetrics:
    """Extract declared and undeclared metric values from a run's output."""
    if source == MetricsSourceKind.IMPORTED:
        raise ExperimentError(
            "IMPORTED metrics do not come from a command; use the import path"
        )
    declared = {metric.key for metric in metrics}
    collector = _Collector(declared=declared, max_observations=max_observations)

    if source == MetricsSourceKind.JSON_FILE:
        if not path:
            raise ExperimentError(
                "JSON_FILE metrics require a workspace-relative metrics path"
            )
        if workspace is None:
            raise ExperimentError("JSON_FILE metrics require a workspace")
        target = _safe_workspace_path(workspace, path)
        if not target.exists() or not target.is_file():
            raise ExperimentError(
                f"Metrics file was not produced: {path}",
                hint="The command must write the declared metrics file inside the workspace.",
            )
        size = target.stat().st_size
        if size > max_file_bytes:
            raise ExperimentError(
                f"Metrics file is {size} bytes, above the {max_file_bytes}-byte limit"
            )
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentError(f"Cannot parse metrics file {path}: {exc}") from exc
        collector.add(_payload_metrics(payload, where=path), step=None, where=path)

    elif source == MetricsSourceKind.STDOUT_JSON:
        payload = _last_json_object(stdout)
        if payload is None:
            raise ExperimentError(
                "No JSON metrics document was found in stdout",
                hint='Print one line: {"metrics": {"key": 1.0}}.',
            )
        collector.add(
            _payload_metrics(payload, where="stdout"),
            step=_optional_index(payload, "step"),
            where="stdout",
        )

    elif source == MetricsSourceKind.STDOUT_JSONL:
        found = 0
        for line_number, line in enumerate(stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                collector.warnings.append(
                    f"stdout line {line_number} was not JSON and was ignored"
                )
                continue
            found += 1
            collector.add(
                _payload_metrics(payload, where=f"stdout line {line_number}"),
                step=_optional_index(payload, "step"),
                where=f"stdout line {line_number}",
            )
        if found == 0:
            raise ExperimentError(
                "No JSON metric lines were found in stdout",
                hint='Print one {"metrics": {...}} document per line.',
            )
    else:  # pragma: no cover - enum exhaustiveness
        raise ExperimentError(f"Unsupported metrics source: {source.value}")

    missing = sorted(declared - set(collector.values))
    return ExtractedMetrics(
        values=collector.values,
        source=source.value,
        undeclared=sorted(collector.undeclared),
        missing_declared=missing,
        warnings=collector.warnings,
        raw_records=collector.raw_records,
    )


def parse_imported_metrics(
    payload_text: str,
    *,
    metrics: list[MetricSpec],
    max_observations: int,
) -> ExtractedMetrics:
    """Parse an imported JSON document or JSON-lines file (user-owned run)."""
    declared = {metric.key for metric in metrics}
    collector = _Collector(declared=declared, max_observations=max_observations)
    text = payload_text.strip()
    if not text:
        raise ExperimentError("The imported metrics file is empty")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None
    if document is None and not text.lstrip().startswith(("{", "[")):
        return _parse_csv_metrics(
            text, declared=declared, max_observations=max_observations
        )
    if document is not None:
        if isinstance(document, list):
            if not document:
                raise ExperimentError("The imported metrics list is empty")
            for index, item in enumerate(document, start=1):
                collector.add(
                    _payload_metrics(item, where=f"record {index}"),
                    step=_optional_index(item, "step"),
                    where=f"record {index}",
                )
        elif isinstance(document, dict) and "runs" in document:
            runs = document["runs"]
            if not isinstance(runs, list) or not runs:
                raise ExperimentError("'runs' must be a non-empty list")
            for index, item in enumerate(runs, start=1):
                collector.add(
                    _payload_metrics(item, where=f"runs[{index}]"),
                    step=_optional_index(item, "step"),
                    where=f"runs[{index}]",
                )
        else:
            collector.add(
                _payload_metrics(document, where="imported document"),
                step=_optional_index(document, "step"),
                where="imported document",
            )
    else:
        parsed_lines = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExperimentError(
                    f"Imported line {line_number} is neither JSON nor part of a JSON document"
                ) from exc
            parsed_lines += 1
            collector.add(
                _payload_metrics(item, where=f"line {line_number}"),
                step=_optional_index(item, "step"),
                where=f"line {line_number}",
            )
        if parsed_lines == 0:
            raise ExperimentError("No metric records were found in the import")
    return ExtractedMetrics(
        values=collector.values,
        source=MetricsSourceKind.IMPORTED.value,
        undeclared=sorted(collector.undeclared),
        missing_declared=sorted(declared - set(collector.values)),
        warnings=collector.warnings,
        raw_records=collector.raw_records,
    )


def _parse_csv_metrics(
    text: str, *, declared: set[str], max_observations: int
) -> ExtractedMetrics:
    """Parse the documented CSV shapes for imported metrics.

    Tall form: ``metric,value[,step]`` (one observation per row).
    Wide form: one row per observation with a metric column per key; ``seed``,
    ``step`` and ``run`` columns are ignored. Any other header is a metric key.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ExperimentError("The imported CSV has no header row")
    headers = [str(name).strip() for name in reader.fieldnames]
    lowered = [name.lower() for name in headers]
    collector = _Collector(declared=declared, max_observations=max_observations)
    tall = "value" in lowered and ("metric" in lowered or "metric_key" in lowered)
    for row_number, row in enumerate(reader, start=2):
        cleaned = {str(key).strip(): value for key, value in row.items() if key is not None}
        # CSV headers are user data; match the documented names without
        # assuming the exporter's capitalisation.
        lowered_row = {name.lower(): value for name, value in cleaned.items()}
        if tall:
            metric_name = str(
                lowered_row.get("metric")
                or lowered_row.get("metric_key")
                or ""
            )
            metric_name = metric_name.strip()
            if not metric_name:
                collector.warnings.append(
                    f"CSV row {row_number} has no metric key and was ignored"
                )
                continue
            raw = lowered_row.get("value")
            try:
                value = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ExperimentError(
                    f"CSV row {row_number}: value is not a number: {raw!r}"
                ) from exc
            step_raw = lowered_row.get("step")
            step = None
            if step_raw not in (None, ""):
                try:
                    step = float(step_raw)
                except ValueError as exc:
                    raise ExperimentError(
                        f"CSV row {row_number}: step is not a number: {step_raw!r}"
                    ) from exc
            collector.add({metric_name: value}, step=step, where=f"CSV row {row_number}")
        else:
            metrics: dict[str, float] = {}
            step = None
            for header, raw in cleaned.items():
                name = header.strip()
                if name.lower() in {"seed", "run", "run_id"}:
                    continue
                if name.lower() == "step":
                    if raw not in (None, ""):
                        try:
                            step = float(raw)
                        except ValueError as exc:
                            raise ExperimentError(
                                f"CSV row {row_number}: step is not a number: {raw!r}"
                            ) from exc
                    continue
                if raw in (None, ""):
                    continue
                try:
                    metrics[name] = float(raw)
                except ValueError as exc:
                    raise ExperimentError(
                        f"CSV row {row_number}: {name} is not a number: {raw!r}"
                    ) from exc
            if not metrics:
                collector.warnings.append(
                    f"CSV row {row_number} has no numeric metric values and was ignored"
                )
                continue
            collector.add(metrics, step=step, where=f"CSV row {row_number}")
    return ExtractedMetrics(
        values=collector.values,
        source=MetricsSourceKind.IMPORTED.value,
        undeclared=sorted(collector.undeclared),
        missing_declared=sorted(declared - set(collector.values)),
        warnings=collector.warnings,
        raw_records=collector.raw_records,
    )


def _safe_workspace_path(workspace: Path, rel: str) -> Path:
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ExperimentError(
            f"Metrics path must be workspace-relative and may not contain '..': {rel}"
        )
    root = Path(workspace).resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ExperimentError(f"Metrics path escapes the workspace: {rel}")
    return target


def _last_json_object(stdout: str) -> object | None:
    """Return the last stdout line that parses as a JSON object.

    Only objects with a ``metrics`` member qualify, so an unrelated JSON log
    line cannot be mistaken for the metrics contract.
    """
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
            return payload
    return None


def aggregate(values: list[float], method: MetricAggregation) -> float | None:
    """Reduce raw values inside one run to one replicate value."""
    if not values:
        return None
    if method == MetricAggregation.LAST:
        return values[-1]
    if method == MetricAggregation.MEAN:
        return sum(values) / len(values)
    if method == MetricAggregation.MEDIAN:
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0
    if method == MetricAggregation.MAX:
        return max(values)
    if method == MetricAggregation.MIN:
        return min(values)
    if method == MetricAggregation.SUM:
        return sum(values)
    raise ExperimentError(f"Unknown aggregation: {method}")


def build_observations(
    *,
    run_id: str,
    experiment_id: str,
    project_id: str,
    extracted: ExtractedMetrics,
    recorded_at: object | None = None,
) -> list[MetricObservation]:
    """Turn extracted values into deterministic observation rows.

    The id is derived from run, metric, sample index and step, so importing the
    same run twice is a conflict rather than duplicated evidence.
    """
    from mayaworkbench.clock import utcnow
    from mayaworkbench.ids import short_hash

    timestamp = recorded_at or utcnow()
    rows: list[MetricObservation] = []
    for key, values in sorted(extracted.values.items()):
        for sample_index, step, value in values:
            rows.append(
                MetricObservation(
                    id="obs_"
                    + short_hash(
                        f"{run_id}:{key}:{sample_index}:{step}", 32
                    ),
                    experiment_id=experiment_id,
                    run_id=run_id,
                    project_id=project_id,
                    metric_key=key,
                    value=value,
                    sample_index=sample_index,
                    step=step,
                    source=extracted.source,
                    recorded_at=timestamp,
                )
            )
    return rows


__all__ = [
    "RawValue",
    "ExtractedMetrics",
    "extract_metrics",
    "parse_imported_metrics",
    "aggregate",
    "build_observations",
]

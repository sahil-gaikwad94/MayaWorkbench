"""Standard-format experiment adapters (MLflow / TensorBoard compatible).

These are export adapters only: they write deterministic, well-known formats
from stored comparisons. They neither contact a tracking server nor imply that
an imported file was produced by a trusted runner.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _metric_rows(comparison: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in comparison.get("metrics", []) or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "name": str(item.get("name") or item.get("key") or "metric"),
                "baseline": item.get("baseline"),
                "candidate": item.get("candidate"),
                "difference": item.get("difference"),
                "p_value": item.get("p_value"),
                "ci_low": item.get("ci_low"),
                "ci_high": item.get("ci_high"),
                "primary": bool(item.get("primary", False)),
            }
        )
    return rows


def export_mlflow(comparison: dict[str, Any], directory: str | Path) -> list[str]:
    """Write an MLflow-importable JSON run document plus a params file."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "experiment_name": str(comparison.get("definition_id") or "maya-experiment"),
        "run_name": str(comparison.get("id") or "comparison"),
        "metrics": {
            row["name"]: {
                "baseline": row["baseline"],
                "candidate": row["candidate"],
                "difference": row["difference"],
                "p_value": row["p_value"],
            }
            for row in _metric_rows(comparison)
        },
        "params": {
            "protocol_version": str(comparison.get("protocol_version", "")),
            "verdict": str(comparison.get("verdict", "")),
            "comparable": bool(comparison.get("comparable", False)),
        },
        "tags": {"source": "mayaworkbench", "attestation": "UNATTESTED"},
    }
    target = root / "mlflow-run.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    params = root / "mlflow-params.json"
    params.write_text(json.dumps(payload["params"], indent=2, sort_keys=True), encoding="utf-8")
    return [str(target), str(params)]


def export_tensorboard(comparison: dict[str, Any], directory: str | Path) -> list[str]:
    """Write TensorBoard-compatible scalar TSV for baseline and candidate."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "tb-scalars.csv"
    rows = _metric_rows(comparison)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["name", "arm", "value", "step"])
        for row in rows:
            for arm in ("baseline", "candidate"):
                value = row.get(arm)
                if isinstance(value, (int, float)):
                    writer.writerow([row["name"], arm, value, 0])
    notes = root / "tb-notes.json"
    notes.write_text(
        json.dumps(
            {
                "verdict": comparison.get("verdict"),
                "comparable": comparison.get("comparable"),
                "note": "Scalar export only; no event files or trusted signatures.",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return [str(target), str(notes)]


def export_adapter(
    comparison: dict[str, Any], *, format: str, directory: str | Path
) -> list[str]:
    if format == "mlflow":
        return export_mlflow(comparison, directory)
    if format == "tensorboard":
        return export_tensorboard(comparison, directory)
    raise ValueError(f"unknown experiment adapter format: {format}")


__all__ = [
    "export_mlflow",
    "export_tensorboard",
    "export_adapter",
]

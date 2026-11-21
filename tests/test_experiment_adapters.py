"""Experiment adapters write standard formats without contacting tracking servers."""

from __future__ import annotations

import csv
import json

from mayaworkbench.experiments.adapters import export_adapter

COMPARISON = {
    "id": "cmp_1",
    "definition_id": "exp_1",
    "protocol_version": "1.0",
    "verdict": "CANDIDATE_ADVANTAGE",
    "comparable": True,
    "metrics": [
        {
            "name": "accuracy",
            "baseline": 0.8,
            "candidate": 0.85,
            "difference": 0.05,
            "p_value": 0.01,
            "primary": True,
        }
    ],
}


def test_mlflow_export_is_deterministic(tmp_path):
    files = export_adapter(COMPARISON, format="mlflow", directory=tmp_path / "mlflow")
    payload = json.loads(json.loads(json.dumps(files)) and open(files[0]).read())
    assert payload["experiment_name"] == "exp_1"
    assert payload["metrics"]["accuracy"]["candidate"] == 0.85
    assert payload["tags"]["attestation"] == "UNATTESTED"


def test_tensorboard_export_is_tsv_and_bounded(tmp_path):
    files = export_adapter(COMPARISON, format="tensorboard", directory=tmp_path / "tb")
    with open(files[0], newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    assert rows[0] == ["name", "arm", "value", "step"]
    assert any(row[:3] == ["accuracy", "candidate", "0.85"] for row in rows)


def test_unknown_format_is_refused(tmp_path):
    try:
        export_adapter(COMPARISON, format="wandb", directory=tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown format must be refused")

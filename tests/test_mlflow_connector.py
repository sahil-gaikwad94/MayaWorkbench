"""The MLflow connector is read-only, allowlisted and never emits credentials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError
from mayaworkbench.models.connectors.mlflow import MlflowConnector

_URL = "https://mlflow.example.test"
_TOKEN = "mlflow-secret-canary-token"
_RUN = "abc123def456"


class _Response:
    def __init__(self, payload: dict, status: int = 200):
        self._raw = json.dumps(payload).encode()
        self.status = status

    def read(self, _limit):
        return self._raw

    def close(self):
        pass


def _opener(payload, status=200):
    calls = []

    def open_(request, *, timeout):
        calls.append(request)
        return _Response(payload, status)

    return open_, calls


def _payload():
    return {
        "run": {
            "info": {
                "run_id": _RUN,
                "experiment_id": "1",
                "status": "FINISHED",
                "source_name": "train.py",
                "source_version": "deadbeef",
            },
            "data": {
                "metrics": [
                    {"key": "accuracy", "value": 0.91, "step": 10, "timestamp": 1}
                ],
                "params": [{"key": "seed", "value": "7"}],
                "artifacts": [{"path": "model", "is_dir": True}],
            },
        }
    }


def _connector(opener):
    return MlflowConnector(
        tracking_url=_URL,
        token=_TOKEN,
        opener=opener,
        allowed_hosts=("mlflow.example.test",),
    )


def test_get_run_maps_provenance_and_stable_hash():
    opener, calls = _opener(_payload())
    connector = _connector(opener)
    result = connector.invoke("get_run", {"run_id": _RUN})
    assert result.connector == "mlflow"
    assert result.read_only is True
    assert result.data["run_id"] == _RUN
    assert result.data["metrics"][0]["key"] == "accuracy"
    assert result.data["params"][0]["value"] == "7"
    assert result.data["provenance"]["attestation"] == "UNATTESTED"
    assert result.data["provenance"]["execution"] == "IMPORTED"
    assert len(result.response_hash) == 64
    assert calls[0].get_header("Authorization") == f"Bearer {_TOKEN}"
    second = _connector(_opener(_payload())[0]).invoke("get_run", {"run_id": _RUN})
    assert second.response_hash == result.response_hash
    assert second.request_hash == result.request_hash


def test_refuses_non_allowlisted_http_and_missing_token():
    with pytest.raises(PolicyError):
        MlflowConnector(tracking_url="http://mlflow.example.test", token=_TOKEN).invoke(
            "get_run", {"run_id": _RUN}
        )
    with pytest.raises(PolicyError):
        MlflowConnector(
            tracking_url=_URL, token=None, opener=_opener(_payload())[0],
            allowed_hosts=("other.example.test",),
        ).invoke("get_run", {"run_id": _RUN})
    with pytest.raises(PolicyError):
        MlflowConnector(tracking_url=_URL, token="", opener=_opener(_payload())[0]).invoke(
            "get_run", {"run_id": _RUN}
        )


def test_bad_run_ids_and_unsupported_operations_are_refused():
    connector = _connector(_opener(_payload())[0])
    with pytest.raises(ConnectorError):
        connector.invoke("get_run", {"run_id": "../escape"})
    with pytest.raises(ConnectorError):
        connector.invoke("write_run", {"run_id": _RUN})


def test_provider_errors_are_redacted():
    def failing(_request, *, timeout):
        raise RuntimeError(f"request failed for {_TOKEN}")

    with pytest.raises(ConnectorError) as exc:
        _connector(failing).invoke("get_run", {"run_id": _RUN})
    assert _TOKEN not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_revoke_disables_future_calls():
    connector = _connector(_opener(_payload())[0])
    connector.invoke("get_run", {"run_id": _RUN})
    connector.revoke()
    assert connector.is_revoked()
    with pytest.raises(ConnectorError):
        connector.invoke("get_run", {"run_id": _RUN})


def test_registry_requires_grant_and_masks_secrets(monkeypatch):
    from mayaworkbench.models.connectors.registry import build_advanced_connector_registry

    monkeypatch.setenv("MAYA_MLFLOW_TRACKING_URL", _URL)
    monkeypatch.setenv("MAYA_MLFLOW_TOKEN", _TOKEN)
    registry = build_advanced_connector_registry(http=_opener(_payload())[0])
    names = registry.names()
    assert "mlflow" in names
    with pytest.raises(Exception):
        registry.invoke_with_consent("mlflow", "get_run", {"run_id": _RUN})
    registry.approve("mlflow", "get_run", {"run_id": _RUN})
    result = registry.invoke_with_consent("mlflow", "get_run", {"run_id": _RUN})
    assert result.data["run_id"] == _RUN
    assert _TOKEN not in json.dumps(result.model_dump(mode="json"))
    registry.revoke("mlflow")
    with pytest.raises(Exception):
        registry.invoke_with_consent("mlflow", "get_run", {"run_id": _RUN})

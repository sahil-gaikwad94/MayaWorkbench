"""Read-only MLflow tracking connector.

This consumes an MLflow tracking server's read API; it does not recreate an
experiment platform. External metrics are advisory, labelled IMPORTED/EXTERNAL,
and can only downgrade a reproduction claim. Credentials come from the
environment and are never logged, returned or written to artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.errors import PolicyError
from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult
from mayaworkbench.models.connectors.contracts import (
    CONNECTOR_READ_SCOPE,
    ConnectorContract,
    make_result,
    reject_inline_secrets,
)

CONTRACT = ConnectorContract(
    id="mlflow",
    version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "mlflow:read"],
    risk="MEDIUM",
    side_effects=[],
    evidence_outputs=["run_id", "experiment_id", "metrics", "params", "artifacts", "provenance"],
    operation_scopes={"get_run": CONNECTOR_READ_SCOPE},
)

_TOKEN_ENV = "MAYA_MLFLOW_TOKEN"
_URL_ENV = "MAYA_MLFLOW_TRACKING_URL"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_RESPONSE_BYTES = 2_000_000
_SECRET_KEYS = ("token", "authorization", "api_key", "password", "secret")


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    text = str(value)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return text[:500]


class MlflowConnector:
    """Fetch one MLflow run from an allowlisted tracking endpoint."""

    name = "mlflow"
    _OPERATIONS = ("get_run",)

    def __init__(
        self,
        *,
        tracking_url: str | None = None,
        token: str | None = None,
        opener: Callable[..., Any] | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ):
        self._tracking_url = (tracking_url or os.environ.get(_URL_ENV) or "").rstrip("/")
        self._token = token  # injected for tests; production reads env at invoke
        self._token_env = _TOKEN_ENV
        self._opener = opener
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._revoked = False

    # ----------------------------------------------------------- introspection
    def operations(self) -> tuple[str, ...]:
        return self._OPERATIONS

    def supports(self, operation: str) -> bool:
        return operation in self._OPERATIONS

    def is_revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        self._revoked = True

    def describe(self) -> dict[str, Any]:
        parsed = urlsplit(self._tracking_url) if self._tracking_url else None
        return {
            "name": self.name,
            "operations": list(self._OPERATIONS),
            "target": parsed.hostname if parsed else None,
            "token_env": self._token_env,
            "key_values_emitted": False,
            "risk": CONTRACT.risk.value if hasattr(CONTRACT.risk, "value") else str(CONTRACT.risk),
        }

    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        return f"mlflow:{params.get('run_id', '<unset>')}"

    # --------------------------------------------------------------- execution
    def _validate_endpoint(self) -> tuple[str, str]:
        if not self._tracking_url:
            raise PolicyError(
                f"No MLflow tracking URL configured; set ${_URL_ENV} to an allowlisted https host",
                code="MLFLOW_ENDPOINT_MISSING",
            )
        parsed = urlsplit(self._tracking_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PolicyError("The MLflow endpoint must be an https URL without embedded credentials")
        if parsed.port not in (None, 443):
            raise PolicyError("The MLflow endpoint must use the default https port 443")
        if self._allowed_hosts and parsed.hostname.lower() not in self._allowed_hosts:
            raise PolicyError(f"MLflow host {parsed.hostname!r} is not allowlisted")
        return self._tracking_url, parsed.hostname

    def _token_value(self) -> str:
        token = self._token if self._token is not None else os.environ.get(self._token_env)
        if not token:
            raise PolicyError(f"No MLflow token found in ${self._token_env}")
        return token

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError("The MLflow connector has been revoked")
        if not self.supports(operation):
            raise ConnectorError(f"MLflow connector does not support {operation!r}")
        run_id = str(params.get("run_id") or "")
        if not _RUN_ID_RE.match(run_id):
            raise ConnectorError("A valid MLflow run id is required")
        endpoint, _host = self._validate_endpoint()
        token = self._token_value()
        import urllib.request

        url = f"{endpoint}/api/2.0/mlflow/runs/get?run_id={run_id}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        opener = self._opener or urllib.request.urlopen
        try:
            response = opener(request, timeout=10)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            status = getattr(response, "status", 200)
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except Exception as exc:  # noqa: BLE001 - never leak provider error details
            raise ConnectorError(
                f"MLflow request failed: {_redact(str(exc), (token,))}"
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ConnectorError("MLflow response exceeded the byte budget")
        if int(status) != 200:
            raise ConnectorError(f"MLflow returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ConnectorError("MLflow returned malformed JSON") from None
        run = payload.get("run") if isinstance(payload.get("run"), dict) else None
        if run is None:
            raise ConnectorError("MLflow response contained no run")
        info = run.get("info") if isinstance(run.get("info"), dict) else {}
        data = run.get("data") if isinstance(run.get("data"), dict) else {}
        metrics = [
            {
                "key": str(item.get("key", "")),
                "value": item.get("value"),
                "step": item.get("step"),
                "timestamp": item.get("timestamp"),
            }
            for item in (data.get("metrics") or [])
            if isinstance(item, dict)
        ]
        mlflow_params = [
            {"key": str(item.get("key", "")), "value": str(item.get("value", ""))[:500]}
            for item in (data.get("params") or [])
            if isinstance(item, dict)
        ]
        artifacts = [
            {"path": str(item.get("path", "")), "is_dir": bool(item.get("is_dir"))}
            for item in (data.get("artifacts") or [])
            if isinstance(item, dict)
        ]
        result_data = {
            "run_id": str(info.get("run_id") or run_id),
            "experiment_id": str(info.get("experiment_id") or ""),
            "status": str(info.get("status") or ""),
            "source_name": str(info.get("source_name") or ""),
            "source_version": str(info.get("source_version") or ""),
            "metrics": metrics,
            "params": mlflow_params,
            "artifacts": artifacts,
            "provenance": {
                "kind": "EXTERNAL",
                "attestation": "UNATTESTED",
                "execution": "IMPORTED",
                "note": (
                    "An external MLflow metric is advisory provenance, not local "
                    "verification evidence. It can only downgrade reproduction claims."
                ),
                "retrieved_at": to_iso(utcnow()),
            },
        }
        return make_result(
            connector=self.name,
            operation=operation,
            target=self.describe_target(operation, params),
            data=result_data,
            request_payload={"run_id": run_id, "endpoint": endpoint},
            raw_response=raw,
        )


__all__ = ["MlflowConnector", "CONTRACT", "_TOKEN_ENV", "_URL_ENV"]

# Spec — MLflow tracking connector (WP-MLFLOW-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — read-only allowlisted tracking connector, consent grant, advisory link artifact/event.
**Vision source:** Audit A §6.5; Audit B Upgrade 2.

## Problem

Maya already exports deterministic MLflow/TensorBoard-compatible JSON from
`experiments/adapters.py`, but does not consume a live MLflow tracking server.
The audit calls for an adapter that consumes MLflow's tracking API and links an
MLflow run to Maya's task, base commit, candidate patch, dataset fingerprint and
verification record — rather than recreating an experiment platform.

## Product outcome

An opt-in, read-only connector that fetches an MLflow run by id from an allowlisted
tracking endpoint, records provenance (run id, experiment id, metrics, params,
artifacts, source version), and links it to a Maya task/run as advisory evidence.
No hosted service, no write operations, no secrets in artifacts.

## Scope (in)

- `models/connectors/mlflow.py`: `MlflowConnector` using stdlib HTTP against an
  allowlisted https host from configuration; API key/token from environment only;
  `GET /api/2.0/mlflow/runs/get`, bounded timeouts/response bytes, SSRF-safe
  host/pin checks, redaction of headers and error bodies; returns a typed result
  with a content hash and explicit `UNATTESTED`/external provenance.
- Registration through `AdvancedConnectorRegistry` with `connector.read`
  capability and the existing consent/grant path; no new authority.
- `maya connector mlflow-link <run_id> --task <task_id> --run <maya_run_id>`
  (or equivalent existing CLI pattern): stores the link as a content-addressed
  artifact plus an append-only event; refuses when the task/run ids do not match
  the caller's project.
- Tests with a fake HTTP opener: successful fetch/mapping, allowlist refusal,
  secret redaction, hash stability, grant requirement, and link event/artifact.
- Status docs updated.

## Explicitly out

- MLflow writes, model registry, hosted MLflow, auto-starting servers.
- Treating an external MLflow metric as local verification evidence.

## Design constraints

- Any external result is labelled `IMPORTED`/`EXTERNAL` and can only downgrade
  reproduction claims, never raise them.
- No secret value is written to SQLite, artifacts, events or logs.
- Fail closed if the endpoint is not allowlisted or the token is absent.

## Acceptance criteria

1. A fake MLflow response maps to a typed result with metrics/params/artifacts and
   a stable content hash.
2. Non-allowlisted endpoints and missing tokens are refused with typed errors.
3. Linking a run writes an artifact and event, and is rejected for a foreign task.
4. Full suite green; status docs updated.

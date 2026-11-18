-- MayaWorkbench schema 0005: verification and evidence (Phase 4).
-- Conventions match migrations 0001-0004: ULID text keys, ISO-8601 UTC text
-- timestamps, JSON text for flexible payloads, core identities relational.
-- A verification attempt is immutable evidence: it is never updated in place,
-- only superseded by a later attempt for the same workflow run.

PRAGMA foreign_keys = ON;

CREATE TABLE verification_runs (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    attempt             INTEGER NOT NULL,
    plan_sha256         TEXT NOT NULL,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, attempt)
);
CREATE INDEX idx_verification_runs_run ON verification_runs(run_id, attempt DESC);

CREATE TABLE verification_checks (
    id                  TEXT PRIMARY KEY,
    verification_id     TEXT NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    check_key           TEXT NOT NULL,
    gate                TEXT NOT NULL,
    kind                TEXT NOT NULL,
    title               TEXT NOT NULL,
    method              TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    required            INTEGER NOT NULL DEFAULT 1,
    criteria_json       TEXT NOT NULL DEFAULT '[]',
    command_json        TEXT NOT NULL DEFAULT '[]',
    exit_code           INTEGER,
    duration_ms         INTEGER,
    artifact_id         TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    summary             TEXT NOT NULL DEFAULT '',
    details_json        TEXT NOT NULL DEFAULT '{}',
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    schema_version      TEXT NOT NULL DEFAULT '1.0',
    UNIQUE (verification_id, check_key)
);
CREATE INDEX idx_verification_checks_run ON verification_checks(run_id, gate);
CREATE INDEX idx_verification_checks_outcome ON verification_checks(run_id, outcome);

CREATE TABLE verification_findings (
    id                  TEXT PRIMARY KEY,
    verification_id     TEXT NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    check_id            TEXT NOT NULL REFERENCES verification_checks(id) ON DELETE CASCADE,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    severity            TEXT NOT NULL,
    code                TEXT NOT NULL,
    message             TEXT NOT NULL,
    path                TEXT,
    line                INTEGER,
    criterion_ref       TEXT,
    source              TEXT NOT NULL DEFAULT 'deterministic',
    details_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_verification_findings_run ON verification_findings(run_id, severity);
CREATE INDEX idx_verification_findings_check ON verification_findings(check_id);

CREATE TABLE verification_reports (
    id                  TEXT PRIMARY KEY,
    verification_id     TEXT NOT NULL UNIQUE REFERENCES verification_runs(id) ON DELETE CASCADE,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    decision            TEXT NOT NULL,
    summary             TEXT NOT NULL,
    criteria_json       TEXT NOT NULL DEFAULT '[]',
    check_ids_json      TEXT NOT NULL DEFAULT '[]',
    blocking_json       TEXT NOT NULL DEFAULT '[]',
    limitations_json    TEXT NOT NULL DEFAULT '[]',
    report_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    json_sha256         TEXT NOT NULL,
    reviewer_invocation_id TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_verification_reports_run ON verification_reports(run_id, created_at DESC);

CREATE TABLE evidence_links (
    id                  TEXT PRIMARY KEY,
    verification_id     TEXT NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    report_id           TEXT REFERENCES verification_reports(id) ON DELETE CASCADE,
    from_kind           TEXT NOT NULL,
    from_ref            TEXT NOT NULL,
    relation            TEXT NOT NULL,
    to_kind             TEXT NOT NULL,
    to_ref              TEXT NOT NULL,
    note                TEXT NOT NULL DEFAULT '',
    sha256              TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_evidence_links_run ON evidence_links(run_id, relation);
CREATE INDEX idx_evidence_links_report ON evidence_links(report_id);

ALTER TABLE task_contracts ADD COLUMN verification_checks_json TEXT NOT NULL DEFAULT '[]';

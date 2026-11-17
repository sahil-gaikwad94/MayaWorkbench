-- MayaWorkbench schema 0003: controlled engineering agent (Phase 3).
-- Conventions match migrations 0001/0002: ULID text keys, ISO-8601 UTC text
-- timestamps, JSON text for flexible payloads, core identities relational.
-- Note: the migration splitter treats a semicolon at end of line as a
-- statement boundary, so comments avoid semicolons.

PRAGMA foreign_keys = ON;

CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    goal            TEXT NOT NULL,
    details_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_tasks_project ON tasks(project_id, created_at);

CREATE TABLE task_contracts (
    id                      TEXT PRIMARY KEY,
    task_id                 TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version                 INTEGER NOT NULL,
    task_type               TEXT NOT NULL,
    goal                    TEXT NOT NULL,
    repository_id           TEXT NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    snapshot_id             TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE RESTRICT,
    scope_json              TEXT NOT NULL,
    constraints_json        TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    unknowns_json           TEXT NOT NULL DEFAULT '[]',
    required_approvals_json TEXT NOT NULL DEFAULT '[]',
    contract_version        TEXT NOT NULL DEFAULT '1.0',
    content_sha256          TEXT NOT NULL,
    created_by              TEXT NOT NULL DEFAULT 'local-user',
    created_at              TEXT NOT NULL,
    UNIQUE (task_id, version)
);
CREATE INDEX idx_contracts_task ON task_contracts(task_id, version);
CREATE INDEX idx_contracts_snapshot ON task_contracts(snapshot_id);

CREATE TABLE task_plans (
    id                      TEXT PRIMARY KEY,
    task_id                 TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id                  TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version                 INTEGER NOT NULL,
    summary                 TEXT NOT NULL,
    steps_json              TEXT NOT NULL,
    files_json              TEXT NOT NULL DEFAULT '[]',
    checks_json             TEXT NOT NULL DEFAULT '[]',
    risks_json              TEXT NOT NULL DEFAULT '[]',
    unknowns_json           TEXT NOT NULL DEFAULT '[]',
    estimated_steps         INTEGER NOT NULL DEFAULT 1,
    plan_sha256             TEXT NOT NULL,
    model_invocation_id     TEXT REFERENCES model_invocations(id) ON DELETE SET NULL,
    artifact_id             TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL,
    UNIQUE (run_id, version)
);
CREATE INDEX idx_plans_task ON task_plans(task_id, created_at);
CREATE INDEX idx_plans_run ON task_plans(run_id, version);

CREATE TABLE workflow_runs (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    contract_id         TEXT NOT NULL REFERENCES task_contracts(id) ON DELETE RESTRICT,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repository_id       TEXT NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    snapshot_id         TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE RESTRICT,
    state               TEXT NOT NULL,
    previous_state      TEXT,
    base_commit_sha     TEXT NOT NULL,
    plan_id             TEXT REFERENCES task_plans(id) ON DELETE SET NULL,
    workspace_id        TEXT,
    step_count          INTEGER NOT NULL DEFAULT 0,
    tool_call_count     INTEGER NOT NULL DEFAULT 0,
    model_tokens        INTEGER NOT NULL DEFAULT 0,
    model_cost_usd      REAL NOT NULL DEFAULT 0,
    pause_requested     INTEGER NOT NULL DEFAULT 0,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    finish_summary      TEXT,
    limitations_json    TEXT NOT NULL DEFAULT '[]',
    error               TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);
CREATE INDEX idx_runs_task ON workflow_runs(task_id, created_at);
CREATE INDEX idx_runs_state ON workflow_runs(state, updated_at);
CREATE INDEX idx_runs_project ON workflow_runs(project_id, created_at);

CREATE TABLE approval_requests (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    status          TEXT NOT NULL,
    resource_scope_json TEXT NOT NULL DEFAULT '{}',
    spec_sha256     TEXT NOT NULL,
    nonce           TEXT NOT NULL UNIQUE,
    requested_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    consumed_at     TEXT,
    reason          TEXT,
    details_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_approvals_run ON approval_requests(run_id, requested_at);
CREATE INDEX idx_approvals_status ON approval_requests(status, expires_at);

CREATE TABLE execution_workspaces (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path                TEXT NOT NULL UNIQUE,
    base_commit_sha     TEXT NOT NULL,
    status              TEXT NOT NULL,
    patch_artifact_id   TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    diff_sha256         TEXT,
    file_count          INTEGER NOT NULL DEFAULT 0,
    details_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    finalized_at        TEXT,
    removed_at          TEXT
);
CREATE INDEX idx_workspaces_run ON execution_workspaces(run_id);
CREATE INDEX idx_workspaces_status ON execution_workspaces(status, created_at);

CREATE TABLE tool_invocations (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    tool_name           TEXT NOT NULL,
    arguments_json      TEXT NOT NULL DEFAULT '{}',
    arguments_sha256    TEXT NOT NULL,
    status              TEXT NOT NULL,
    approval_id         TEXT REFERENCES approval_requests(id) ON DELETE SET NULL,
    output_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    exit_code           INTEGER,
    duration_ms         INTEGER,
    error               TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_tools_run ON tool_invocations(run_id, started_at);
CREATE INDEX idx_tools_tool ON tool_invocations(tool_name, status);

CREATE TABLE task_diffs (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workspace_id        TEXT NOT NULL REFERENCES execution_workspaces(id) ON DELETE CASCADE,
    base_commit_sha     TEXT NOT NULL,
    patch_artifact_id   TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
    patch_sha256        TEXT NOT NULL,
    files_json          TEXT NOT NULL DEFAULT '[]',
    additions           INTEGER NOT NULL DEFAULT 0,
    deletions           INTEGER NOT NULL DEFAULT 0,
    scope_violations_json TEXT NOT NULL DEFAULT '[]',
    warnings_json       TEXT NOT NULL DEFAULT '[]',
    original_checkout_unchanged INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_diffs_task ON task_diffs(task_id, created_at);

CREATE TABLE task_reviews (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    diff_id             TEXT NOT NULL REFERENCES task_diffs(id) ON DELETE CASCADE,
    scope_ok            INTEGER NOT NULL DEFAULT 0,
    secrets_found       INTEGER NOT NULL DEFAULT 0,
    tests_modified_json TEXT NOT NULL DEFAULT '[]',
    dependency_changes_json TEXT NOT NULL DEFAULT '[]',
    findings_json       TEXT NOT NULL DEFAULT '[]',
    report_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_reviews_run ON task_reviews(run_id);

CREATE TABLE worker_jobs (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status              TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    lease_owner         TEXT,
    lease_expires_at    TEXT,
    enqueued_at         TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    error               TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_jobs_status ON worker_jobs(status, enqueued_at);
CREATE INDEX idx_jobs_run ON worker_jobs(run_id);

ALTER TABLE workflow_events ADD COLUMN task_id TEXT;
ALTER TABLE workflow_events ADD COLUMN run_id TEXT;
CREATE INDEX idx_events_task ON workflow_events(task_id, seq);
CREATE INDEX idx_events_run ON workflow_events(run_id, seq);

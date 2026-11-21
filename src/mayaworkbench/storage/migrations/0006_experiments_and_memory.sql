-- MayaWorkbench schema 0006: Experiment Lab and durable project memory (Phase 5).
-- Conventions match migrations 0001-0005: ULID text keys, ISO-8601 UTC text
-- timestamps, JSON text for flexible payloads, core identities relational.
--
-- Experiment definitions are pre-registrations: they are approved before any
-- execution and hashed in full. Manifests are created and hashed *before* the
-- command starts, so a run cannot rewrite its own provenance after seeing a
-- result. Runs and raw metric observations are append-only evidence.

PRAGMA foreign_keys = ON;

CREATE TABLE experiment_definitions (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repository_id       TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    snapshot_id         TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    base_commit_sha     TEXT NOT NULL,
    task_id             TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    name                TEXT NOT NULL,
    hypothesis          TEXT NOT NULL,
    role                TEXT NOT NULL,
    profile             TEXT NOT NULL,
    command_json        TEXT NOT NULL DEFAULT '[]',
    timeout_seconds     INTEGER NOT NULL,
    seeds_json          TEXT NOT NULL DEFAULT '[]',
    metrics_json        TEXT NOT NULL DEFAULT '[]',
    metrics_source_json TEXT NOT NULL DEFAULT '{}',
    protocol_json       TEXT NOT NULL DEFAULT '{}',
    treatment_json      TEXT NOT NULL DEFAULT '[]',
    variation_json      TEXT NOT NULL DEFAULT '[]',
    budget_json         TEXT NOT NULL DEFAULT '{}',
    min_seeds           INTEGER NOT NULL DEFAULT 2,
    non_inferiority_margin REAL,
    status              TEXT NOT NULL,
    approved_by         TEXT,
    approved_at         TEXT,
    approval_sha256     TEXT,
    revoked_reason      TEXT,
    content_sha256      TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_experiment_definitions_project
    ON experiment_definitions(project_id, created_at DESC);
CREATE INDEX idx_experiment_definitions_task ON experiment_definitions(task_id);
CREATE INDEX idx_experiment_definitions_snapshot ON experiment_definitions(snapshot_id);

CREATE TABLE experiment_manifests (
    id                  TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES experiment_definitions(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_id         TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,
    seed                INTEGER NOT NULL,
    attempt             INTEGER NOT NULL,
    definition_sha256   TEXT NOT NULL,
    repository_id       TEXT NOT NULL,
    commit_sha          TEXT NOT NULL,
    snapshot_kind       TEXT NOT NULL,
    includes_worktree_patch INTEGER NOT NULL DEFAULT 0,
    patch_hash          TEXT,
    protocol_json       TEXT NOT NULL DEFAULT '{}',
    command_json        TEXT NOT NULL DEFAULT '[]',
    limits_json         TEXT NOT NULL DEFAULT '{}',
    environment_json    TEXT NOT NULL DEFAULT '{}',
    verified_inputs_json TEXT NOT NULL DEFAULT '{}',
    manifest_sha256     TEXT NOT NULL,
    schema_version      TEXT NOT NULL DEFAULT '1.0',
    created_at          TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}',
    UNIQUE (experiment_id, seed, attempt)
);
CREATE INDEX idx_experiment_manifests_experiment ON experiment_manifests(experiment_id, seed);

CREATE TABLE experiment_runs (
    id                  TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES experiment_definitions(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id             TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    manifest_id         TEXT REFERENCES experiment_manifests(id) ON DELETE SET NULL,
    seed                INTEGER NOT NULL,
    attempt             INTEGER NOT NULL,
    status              TEXT NOT NULL,
    command_json        TEXT NOT NULL DEFAULT '[]',
    exit_code           INTEGER,
    timed_out           INTEGER NOT NULL DEFAULT 0,
    blocked             INTEGER NOT NULL DEFAULT 0,
    cancelled           INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER,
    cpu_user_ms         INTEGER,
    cpu_system_ms       INTEGER,
    max_rss_kb          INTEGER,
    output_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    metrics_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    metrics_ingested    INTEGER NOT NULL DEFAULT 0,
    observation_count   INTEGER NOT NULL DEFAULT 0,
    metrics_error       TEXT,
    error               TEXT,
    termination_reason  TEXT NOT NULL DEFAULT '',
    workspace_kept      INTEGER NOT NULL DEFAULT 0,
    workspace_path      TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}',
    UNIQUE (experiment_id, seed, attempt)
);
CREATE INDEX idx_experiment_runs_experiment ON experiment_runs(experiment_id, seed, attempt DESC);
CREATE INDEX idx_experiment_runs_status ON experiment_runs(status);
CREATE INDEX idx_experiment_runs_task ON experiment_runs(task_id);

CREATE TABLE metric_observations (
    id                  TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES experiment_definitions(id) ON DELETE CASCADE,
    run_id              TEXT NOT NULL REFERENCES experiment_runs(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    metric_key          TEXT NOT NULL,
    value               REAL NOT NULL,
    sample_index        INTEGER NOT NULL DEFAULT 0,
    step                REAL,
    source              TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_metric_observations_run ON metric_observations(run_id, metric_key, sample_index);
CREATE INDEX idx_metric_observations_experiment ON metric_observations(experiment_id, metric_key);

CREATE TABLE experiment_comparisons (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id             TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    baseline_experiment_id TEXT NOT NULL REFERENCES experiment_definitions(id) ON DELETE CASCADE,
    candidate_experiment_id TEXT NOT NULL REFERENCES experiment_definitions(id) ON DELETE CASCADE,
    baseline_definition_sha256 TEXT NOT NULL,
    candidate_definition_sha256 TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    design              TEXT NOT NULL,
    comparable          INTEGER NOT NULL DEFAULT 0,
    findings_json       TEXT NOT NULL DEFAULT '[]',
    metrics_json        TEXT NOT NULL DEFAULT '[]',
    baseline_run_ids_json TEXT NOT NULL DEFAULT '[]',
    candidate_run_ids_json TEXT NOT NULL DEFAULT '[]',
    limitations_json    TEXT NOT NULL DEFAULT '[]',
    report_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    json_sha256         TEXT NOT NULL,
    schema_version      TEXT NOT NULL DEFAULT '1.0',
    created_at          TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_experiment_comparisons_candidate
    ON experiment_comparisons(candidate_experiment_id, created_at DESC);
CREATE INDEX idx_experiment_comparisons_task ON experiment_comparisons(task_id);

CREATE TABLE project_memories (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL,
    author_kind         TEXT NOT NULL,
    title               TEXT NOT NULL,
    statement           TEXT NOT NULL,
    scope               TEXT NOT NULL DEFAULT 'project',
    repository_id       TEXT REFERENCES repositories(id) ON DELETE SET NULL,
    snapshot_id         TEXT REFERENCES repository_snapshots(id) ON DELETE SET NULL,
    commit_sha          TEXT,
    confidence          TEXT NOT NULL DEFAULT 'MEDIUM',
    supersedes_id       TEXT,
    last_validated_at   TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_project_memories_project ON project_memories(project_id, kind, status);
CREATE INDEX idx_project_memories_repository ON project_memories(repository_id, commit_sha);

CREATE TABLE memory_sources (
    id                  TEXT PRIMARY KEY,
    memory_id           TEXT NOT NULL REFERENCES project_memories(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    ref                 TEXT NOT NULL,
    sha256              TEXT,
    note                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_memory_sources_memory ON memory_sources(memory_id);

ALTER TABLE task_contracts ADD COLUMN require_experiment INTEGER NOT NULL DEFAULT 0;

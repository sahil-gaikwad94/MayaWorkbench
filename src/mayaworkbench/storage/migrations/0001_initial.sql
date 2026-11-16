-- MayaWorkbench schema 0001: Phase 1 repository intelligence foundation.
-- Conventions:
--   * ULID text primary keys; append-only events carry an integer autoincrement
--     order key plus a public event_id.
--   * Booleans are stored as 0/1 integers, timestamps as ISO-8601 UTC text.
--   * Large payloads live in the artifact store; only metadata and hashes are
--     stored relationally.

PRAGMA foreign_keys = ON;

CREATE TABLE projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    settings_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE artifacts (
    id              TEXT PRIMARY KEY,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    media_type      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    retention       TEXT NOT NULL DEFAULT 'PROJECT',
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL,
    UNIQUE (sha256, kind)
);
CREATE INDEX idx_artifacts_kind ON artifacts(kind, created_at);

CREATE TABLE repositories (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    root_path       TEXT NOT NULL,
    vcs             TEXT NOT NULL DEFAULT 'git',
    remote_url      TEXT,
    default_branch  TEXT,
    details_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (project_id, root_path)
);
CREATE INDEX idx_repositories_project ON repositories(project_id, created_at);

CREATE TABLE repository_snapshots (
    id                              TEXT PRIMARY KEY,
    repository_id                   TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind                            TEXT NOT NULL,
    status                          TEXT NOT NULL,
    commit_sha                      TEXT,
    tree_sha                        TEXT,
    branch                          TEXT,
    base_commit_sha                 TEXT,
    dirty                           INTEGER NOT NULL DEFAULT 0,
    includes_worktree_patch         INTEGER NOT NULL DEFAULT 0,
    patch_hash                      TEXT,
    patch_artifact_id               TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    untracked_manifest_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    included_paths_json             TEXT NOT NULL DEFAULT '[]',
    submodules_json                 TEXT NOT NULL DEFAULT '{}',
    indexer_version                 TEXT NOT NULL,
    indexed_at                      TEXT,
    file_count                      INTEGER NOT NULL DEFAULT 0,
    symbol_count                    INTEGER NOT NULL DEFAULT 0,
    total_bytes                     INTEGER NOT NULL DEFAULT 0,
    error                           TEXT,
    index_metadata_json             TEXT NOT NULL DEFAULT '{}',
    created_at                      TEXT NOT NULL
);
CREATE INDEX idx_snapshots_repository ON repository_snapshots(repository_id, created_at DESC);
CREATE INDEX idx_snapshots_status ON repository_snapshots(status);

CREATE TABLE indexed_files (
    id                  TEXT PRIMARY KEY,
    snapshot_id         TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    path                TEXT NOT NULL,
    directory           TEXT NOT NULL,
    extension           TEXT NOT NULL,
    language            TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    line_count          INTEGER NOT NULL,
    content_sha256      TEXT NOT NULL,
    git_blob_sha        TEXT,
    mode                TEXT,
    status              TEXT NOT NULL,
    parse_coverage      TEXT NOT NULL,
    is_test             INTEGER NOT NULL DEFAULT 0,
    is_doc              INTEGER NOT NULL DEFAULT 0,
    is_config           INTEGER NOT NULL DEFAULT 0,
    is_binary           INTEGER NOT NULL DEFAULT 0,
    skip_reason         TEXT,
    content_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    UNIQUE (snapshot_id, path)
);
CREATE INDEX idx_files_snapshot_status ON indexed_files(snapshot_id, status);
CREATE INDEX idx_files_snapshot_language ON indexed_files(snapshot_id, language);
CREATE INDEX idx_files_snapshot_dir ON indexed_files(snapshot_id, directory);

-- Per-line raw text for exact and regular-expression scanning.
CREATE TABLE file_lines (
    id          INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    file_id     TEXT NOT NULL REFERENCES indexed_files(id) ON DELETE CASCADE,
    line_no     INTEGER NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX idx_file_lines_file ON file_lines(file_id, line_no);
CREATE INDEX idx_file_lines_snapshot ON file_lines(snapshot_id);

-- Token index for lexical search. ``tokens`` is a derived, identifier-aware
-- expansion of the raw line (camelCase and snake_case are split), so a query for
-- "attention" matches ``CausalAttentionModule`` and ``attention_mask``. Raw text
-- stays in file_lines, giving exact and regex search an unmodified source.
CREATE VIRTUAL TABLE search_lines USING fts5(
    snapshot_id UNINDEXED,
    file_id UNINDEXED,
    line_no UNINDEXED,
    tokens,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE symbols (
    id              TEXT PRIMARY KEY,
    snapshot_id     TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    file_id         TEXT NOT NULL REFERENCES indexed_files(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    signature       TEXT NOT NULL DEFAULT '',
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    docstring       TEXT,
    decorators_json TEXT NOT NULL DEFAULT '[]',
    confidence      TEXT NOT NULL DEFAULT 'HIGH',
    parent_symbol_id TEXT REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE INDEX idx_symbols_snapshot_kind ON symbols(snapshot_id, kind);
CREATE INDEX idx_symbols_snapshot_name ON symbols(snapshot_id, name);
CREATE INDEX idx_symbols_snapshot_qualified ON symbols(snapshot_id, qualified_name);
CREATE INDEX idx_symbols_file ON symbols(file_id, start_line);
CREATE INDEX idx_symbols_parent ON symbols(parent_symbol_id);

CREATE TABLE symbol_edges (
    id              TEXT PRIMARY KEY,
    snapshot_id     TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    source_symbol_id TEXT REFERENCES symbols(id) ON DELETE CASCADE,
    source_file_id  TEXT NOT NULL REFERENCES indexed_files(id) ON DELETE CASCADE,
    target_symbol_id TEXT REFERENCES symbols(id) ON DELETE SET NULL,
    target_file_id  TEXT REFERENCES indexed_files(id) ON DELETE SET NULL,
    edge_type       TEXT NOT NULL,
    target_name     TEXT NOT NULL,
    line            INTEGER NOT NULL,
    confidence      TEXT NOT NULL DEFAULT 'MEDIUM',
    provenance      TEXT NOT NULL DEFAULT 'ast'
);
CREATE INDEX idx_edges_snapshot_type ON symbol_edges(snapshot_id, edge_type);
CREATE INDEX idx_edges_source ON symbol_edges(source_symbol_id, edge_type);
CREATE INDEX idx_edges_target ON symbol_edges(target_symbol_id, edge_type);
CREATE INDEX idx_edges_source_file ON symbol_edges(source_file_id);

-- Import aliases are index facts (not user data): they let call resolution map
-- a local name back to the module or symbol it was imported from.
CREATE TABLE import_aliases (
    id          TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    file_id     TEXT NOT NULL REFERENCES indexed_files(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    module      TEXT NOT NULL,
    imported_name TEXT NOT NULL,
    level       INTEGER NOT NULL DEFAULT 0,
    is_from     INTEGER NOT NULL DEFAULT 0,
    line        INTEGER NOT NULL
);
CREATE INDEX idx_import_aliases_file ON import_aliases(file_id, alias);
CREATE INDEX idx_import_aliases_snapshot ON import_aliases(snapshot_id);

CREATE TABLE index_issues (
    id          TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    file_id     TEXT REFERENCES indexed_files(id) ON DELETE CASCADE,
    path        TEXT,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_issues_snapshot_kind ON index_issues(snapshot_id, kind);

CREATE TABLE workflow_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    occurred_at     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    repository_id   TEXT,
    snapshot_id     TEXT,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    actor_kind      TEXT NOT NULL,
    actor_name      TEXT NOT NULL,
    correlation_id  TEXT,
    schema_version  TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_project ON workflow_events(project_id, seq);
CREATE INDEX idx_events_entity ON workflow_events(entity_type, entity_id, seq);
CREATE INDEX idx_events_snapshot ON workflow_events(snapshot_id, seq);
CREATE INDEX idx_events_correlation ON workflow_events(correlation_id, seq);

CREATE TABLE model_invocations (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_id         TEXT REFERENCES repository_snapshots(id) ON DELETE SET NULL,
    purpose             TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    status              TEXT NOT NULL,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    latency_ms          INTEGER,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    error_class         TEXT,
    provider_request_id TEXT,
    cost_usd            REAL,
    cost_basis          TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_invocations_project ON model_invocations(project_id, started_at);
CREATE INDEX idx_invocations_snapshot ON model_invocations(snapshot_id, started_at);

CREATE TABLE qa_records (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_id             TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    question                TEXT NOT NULL,
    status                  TEXT NOT NULL,
    answer_markdown         TEXT NOT NULL,
    citations_json          TEXT NOT NULL DEFAULT '[]',
    uncertainties_json      TEXT NOT NULL DEFAULT '[]',
    unsupported_claims_json TEXT NOT NULL DEFAULT '[]',
    model_invocation_id     TEXT REFERENCES model_invocations(id) ON DELETE SET NULL,
    artifact_id             TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    context_token_estimate  INTEGER NOT NULL DEFAULT 0,
    token_budget            INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    details_json            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_qa_project ON qa_records(project_id, created_at DESC);
CREATE INDEX idx_qa_snapshot ON qa_records(snapshot_id, created_at DESC);

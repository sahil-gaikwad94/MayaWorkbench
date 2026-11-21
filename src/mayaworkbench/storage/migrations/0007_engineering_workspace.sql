-- Engineering Workspace (WP4).
--
-- A Workspace is a durable scope spanning multiple resources; the existing
-- Project/Repository/Snapshot model is left untouched. Task-owned
-- ExecutionWorkspace records and ToolInvocation rows from 0003 remain as they
-- are: they describe a run, not the durable user-facing scope.

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT,
    description_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_workspaces_project_slug ON workspaces(project_id, slug);

CREATE TABLE resource_attachments (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    ref TEXT NOT NULL,
    revision TEXT,
    hash_sha256 TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    attached_at TEXT NOT NULL
);
CREATE INDEX ix_resource_attachments_workspace ON resource_attachments(workspace_id, kind);

CREATE TABLE environment_specs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    descriptor_path TEXT,
    lockfile_path TEXT,
    runtime TEXT NOT NULL DEFAULT 'LOCAL_TRUSTED',
    toolchain_json TEXT NOT NULL DEFAULT '{}',
    hash_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_environment_specs_workspace_name ON environment_specs(workspace_id, name);

CREATE TABLE workspace_sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    runtime_id TEXT,
    command_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_workspace_sessions_workspace ON workspace_sessions(workspace_id, state);

CREATE TABLE evidence_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    resource_id TEXT,
    claim TEXT NOT NULL,
    artifact_id TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
    hash_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_evidence_items_workspace ON evidence_items(workspace_id, status);

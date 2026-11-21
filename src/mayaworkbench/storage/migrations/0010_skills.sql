-- Persistent, reusable skills (playbooks and invariant templates).
-- A skill is a versioned procedure or checker; execution never implies authority.

CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'procedure',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'project',
    enabled INTEGER NOT NULL DEFAULT 1,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_skills_key_version ON skills(project_id, key, version);
CREATE INDEX ix_skills_project_enabled ON skills(project_id, enabled, category);

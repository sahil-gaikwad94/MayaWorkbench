-- Research evidence quality (WP-RESEARCH-1, spec §9.2-9.6; audits MW-09 / B §4).
-- Typed, append-only records for version-bound citations and cross-axis evidence
-- disagreements, plus editorial status on source metadata. These are inputs to
-- readiness/reproduction, never new verdict authorities. Existing source rows
-- are backfilled to ACTIVE by the column default; no applied migration is edited.

CREATE TABLE claim_citations (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    claim_id              TEXT NOT NULL,
    document_id           TEXT NOT NULL,
    document_version      INTEGER NOT NULL,
    source_metadata_id    TEXT,
    passage_id            TEXT NOT NULL,
    quote                 TEXT NOT NULL,
    quote_hash            TEXT NOT NULL,
    page                  INTEGER,
    page_end              INTEGER,
    section               TEXT NOT NULL DEFAULT '',
    equation              TEXT,
    char_start            INTEGER,
    char_end              INTEGER,
    granularity           TEXT NOT NULL DEFAULT 'PASSAGE',
    relation              TEXT NOT NULL DEFAULT 'SUPPORTS',
    confidence            TEXT NOT NULL DEFAULT 'HIGH',
    verified_at           TEXT,
    verified_by           TEXT NOT NULL DEFAULT 'local-user',
    supersedes_citation_id TEXT,
    created_at            TEXT NOT NULL
);
CREATE INDEX ix_claim_citations_project ON claim_citations(project_id, created_at DESC);
CREATE INDEX ix_claim_citations_claim ON claim_citations(claim_id, created_at);
CREATE INDEX ix_claim_citations_document ON claim_citations(document_id, document_version);
CREATE INDEX ix_claim_citations_source ON claim_citations(source_metadata_id);

CREATE TABLE evidence_disagreements (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    claim_id    TEXT NOT NULL,
    document_id TEXT,
    passage_id  TEXT,
    mapping_id  TEXT,
    run_id      TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    resolution  TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT 'local-user',
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX ix_evidence_disagreements_project ON evidence_disagreements(project_id, created_at DESC);
CREATE INDEX ix_evidence_disagreements_claim ON evidence_disagreements(claim_id, created_at);
CREATE INDEX ix_evidence_disagreements_document ON evidence_disagreements(document_id);

ALTER TABLE source_metadata ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE source_metadata ADD COLUMN version_label TEXT;
ALTER TABLE source_metadata ADD COLUMN correction_of TEXT;
ALTER TABLE source_metadata ADD COLUMN corrected_by TEXT;
ALTER TABLE source_metadata ADD COLUMN retraction_date TEXT;
ALTER TABLE source_metadata ADD COLUMN retraction_reason TEXT;
ALTER TABLE source_metadata ADD COLUMN relation_note TEXT;
CREATE INDEX ix_source_metadata_status ON source_metadata(project_id, status, document_key);

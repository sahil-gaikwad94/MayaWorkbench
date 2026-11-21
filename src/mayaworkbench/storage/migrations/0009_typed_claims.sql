-- Typed, versioned claim graph (WP6, audit §7.1).
-- ``claim_type`` stays as the legacy compatibility axis; ``kind`` is the new
-- typed axis. Existing rows are backfilled to the closest typed kind so no
-- stored claim is left without a kind. The discovery metadata index is scoped
-- to a project and key and points at immutable, content-addressed artifacts.

ALTER TABLE research_claims ADD COLUMN kind TEXT;
ALTER TABLE research_claims ADD COLUMN explicitness TEXT NOT NULL DEFAULT 'EXPLICIT';
ALTER TABLE research_claims ADD COLUMN target_resources_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE research_claims ADD COLUMN validation_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE research_claims ADD COLUMN uncertainty_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE research_claims ADD COLUMN mapping_status TEXT NOT NULL DEFAULT 'NOT_CHECKED';
ALTER TABLE research_claims ADD COLUMN graph_version TEXT NOT NULL DEFAULT '1.0';

UPDATE research_claims
SET kind = CASE claim_type
    WHEN 'EXPLICIT_SOURCE_CLAIM' THEN 'PAPER_METHOD'
    WHEN 'INFERRED_REQUIREMENT' THEN 'INFERRED_REQUIREMENT'
    WHEN 'USER_REQUIREMENT' THEN 'USER_REQUIREMENT'
    WHEN 'IMPLEMENTATION_DECISION' THEN 'REPRODUCTION_REQUIREMENT'
    ELSE 'PAPER_METHOD'
END
WHERE kind IS NULL OR kind = '';

UPDATE research_claims SET explicitness = 'INFERRED' WHERE claim_type = 'INFERRED_REQUIREMENT';

CREATE INDEX ix_research_claims_kind ON research_claims(project_id, kind);

CREATE TABLE source_metadata (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_key        TEXT NOT NULL,
    provider            TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    title               TEXT NOT NULL DEFAULT '',
    authors_json        TEXT NOT NULL DEFAULT '[]',
    doi                 TEXT,
    url                 TEXT,
    published           TEXT,
    paper_version       TEXT,
    retrieval_url       TEXT,
    metadata_sha256     TEXT NOT NULL,
    retrieved_at        TEXT NOT NULL,
    artifact_id         TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    protocol_version    TEXT NOT NULL DEFAULT '1.0',
    created_at          TEXT NOT NULL
);
CREATE INDEX ix_source_metadata_key ON source_metadata(project_id, document_key, created_at DESC);
CREATE INDEX ix_source_metadata_hash ON source_metadata(project_id, metadata_sha256);

-- MayaWorkbench schema 0002: research intelligence and claim-to-code mapping.
--
-- Design rules carried from 0001:
--   * ULID text primary keys, ISO-8601 UTC timestamps, booleans as 0/1.
--   * Large payloads (document bytes, extracted text, claim sets, matrices,
--     memos) live in the artifact store; the rows hold identity, hashes and
--     relationships.
--   * Every derived row names the exact document version or repository snapshot
--     it came from, so evidence can always be traced back to immutable content.
--
-- Research documents are immutable content versions. Editing a document is not
-- an update: it is a new row with the same document_key and version + 1.

CREATE TABLE research_documents (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_key            TEXT NOT NULL,
    version                 INTEGER NOT NULL DEFAULT 1,
    supersedes_document_id  TEXT REFERENCES research_documents(id) ON DELETE SET NULL,
    title                   TEXT NOT NULL,
    authors_json            TEXT NOT NULL DEFAULT '[]',
    organization            TEXT,
    published_at            TEXT,
    media_kind              TEXT NOT NULL,
    origin                  TEXT NOT NULL,
    origin_uri              TEXT,
    source_url              TEXT,
    doi                     TEXT,
    license                 TEXT,
    content_sha256          TEXT NOT NULL,
    size_bytes              INTEGER NOT NULL,
    document_artifact_id    TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    text_artifact_id        TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    extraction_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    extraction_status       TEXT NOT NULL,
    extraction_tool         TEXT NOT NULL DEFAULT '',
    extraction_version      TEXT NOT NULL DEFAULT '',
    extraction_warnings_json TEXT NOT NULL DEFAULT '[]',
    missing_pages_json      TEXT NOT NULL DEFAULT '[]',
    page_count              INTEGER,
    section_count           INTEGER NOT NULL DEFAULT 0,
    passage_count           INTEGER NOT NULL DEFAULT 0,
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL,
    extracted_at            TEXT,
    UNIQUE (document_key, version)
);
CREATE INDEX idx_research_documents_project ON research_documents(project_id, created_at DESC);
CREATE INDEX idx_research_documents_key ON research_documents(document_key, version DESC);
CREATE INDEX idx_research_documents_hash ON research_documents(project_id, content_sha256);

-- A passage is citable identity, not a search index: text_sha256 and quote_hash
-- both derive from the passage text, and char_start/char_end index the stored
-- extracted-text artifact so a quote can be re-verified byte for byte.
CREATE TABLE source_passages (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    page            INTEGER,
    page_end        INTEGER,
    section_path    TEXT NOT NULL DEFAULT '',
    section_title   TEXT,
    heading_level   INTEGER,
    line_start      INTEGER,
    line_end        INTEGER,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    text_sha256     TEXT NOT NULL,
    quote_hash      TEXT NOT NULL,
    equation_label  TEXT,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (document_id, ordinal)
);
CREATE INDEX idx_source_passages_document ON source_passages(document_id, ordinal);
CREATE INDEX idx_source_passages_section ON source_passages(document_id, section_path);
CREATE INDEX idx_source_passages_quote ON source_passages(document_id, quote_hash);

CREATE TABLE research_claims (
    id                          TEXT PRIMARY KEY,
    project_id                  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id                 TEXT REFERENCES research_documents(id) ON DELETE CASCADE,
    parent_claim_id             TEXT REFERENCES research_claims(id) ON DELETE SET NULL,
    ordinal                     INTEGER NOT NULL DEFAULT 0,
    claim_type                  TEXT NOT NULL,
    title                       TEXT NOT NULL,
    statement                   TEXT NOT NULL,
    implementation_requirement  TEXT NOT NULL DEFAULT '',
    source_locations_json       TEXT NOT NULL DEFAULT '[]',
    basis_passage_ids_json      TEXT NOT NULL DEFAULT '[]',
    expected_code_signals_json  TEXT NOT NULL DEFAULT '[]',
    forbidden_code_signals_json TEXT NOT NULL DEFAULT '[]',
    target_paths_json           TEXT NOT NULL DEFAULT '[]',
    target_symbols_json         TEXT NOT NULL DEFAULT '[]',
    missing_details_json        TEXT NOT NULL DEFAULT '[]',
    validation_methods_json     TEXT NOT NULL DEFAULT '[]',
    derivation_note             TEXT NOT NULL DEFAULT '',
    created_by                  TEXT NOT NULL,
    created_by_detail           TEXT NOT NULL DEFAULT '',
    confidence                  TEXT NOT NULL,
    review_status               TEXT NOT NULL,
    review_note                 TEXT,
    reviewed_by                 TEXT,
    reviewed_at                 TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);
CREATE INDEX idx_research_claims_project ON research_claims(project_id, ordinal);
CREATE INDEX idx_research_claims_document ON research_claims(document_id, ordinal);
CREATE INDEX idx_research_claims_parent ON research_claims(parent_claim_id);
CREATE INDEX idx_research_claims_review ON research_claims(project_id, review_status);

CREATE TABLE claim_dependencies (
    id                  TEXT PRIMARY KEY,
    claim_id            TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
    depends_on_claim_id TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
    note                TEXT NOT NULL DEFAULT '',
    UNIQUE (claim_id, depends_on_claim_id)
);
CREATE INDEX idx_claim_dependencies_claim ON claim_dependencies(claim_id);
CREATE INDEX idx_claim_dependencies_target ON claim_dependencies(depends_on_claim_id);

-- Manual corrections are append-only. The claim row keeps current state; a
-- revision records exactly what changed, by whom and why.
CREATE TABLE claim_revisions (
    id          TEXT PRIMARY KEY,
    claim_id    TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    changed_at  TEXT NOT NULL,
    changed_by  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL,
    after_json  TEXT NOT NULL,
    UNIQUE (claim_id, revision)
);
CREATE INDEX idx_claim_revisions_claim ON claim_revisions(claim_id, revision);

CREATE TABLE claim_risk_factors (
    id          TEXT PRIMARY KEY,
    claim_id    TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    detail      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_claim_risk_factors_claim ON claim_risk_factors(claim_id, category);

-- One current mapping per (claim, snapshot). Re-mapping updates this row and
-- emits an event; the immutable JSON matrix artifact keeps the history.
CREATE TABLE claim_mappings (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    claim_id            TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
    snapshot_id         TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    status              TEXT NOT NULL,
    status_reason       TEXT NOT NULL DEFAULT '',
    rationale           TEXT NOT NULL DEFAULT '',
    gaps_json           TEXT NOT NULL DEFAULT '[]',
    counter_evidence_json TEXT NOT NULL DEFAULT '[]',
    validation_method   TEXT NOT NULL DEFAULT '',
    rule_pack_version   TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    model_invocation_id TEXT REFERENCES model_invocations(id) ON DELETE SET NULL,
    review_status       TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (claim_id, snapshot_id)
);
CREATE INDEX idx_claim_mappings_snapshot ON claim_mappings(snapshot_id, status);
CREATE INDEX idx_claim_mappings_project ON claim_mappings(project_id, updated_at DESC);

CREATE TABLE mapping_evidence (
    id              TEXT PRIMARY KEY,
    mapping_id      TEXT NOT NULL REFERENCES claim_mappings(id) ON DELETE CASCADE,
    snapshot_id     TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
    file_id         TEXT REFERENCES indexed_files(id) ON DELETE SET NULL,
    symbol_id       TEXT REFERENCES symbols(id) ON DELETE SET NULL,
    qualified_name  TEXT,
    path            TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    relation        TEXT NOT NULL,
    strength        TEXT NOT NULL,
    excerpt         TEXT NOT NULL,
    excerpt_sha256  TEXT NOT NULL,
    explanation     TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL,
    matched_signals_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_mapping_evidence_mapping ON mapping_evidence(mapping_id, path, start_line);
CREATE INDEX idx_mapping_evidence_snapshot ON mapping_evidence(snapshot_id);

-- Extraction runs make re-extraction auditable and idempotent: the same document
-- plus method plus algorithm version plus parameters is the same run.
CREATE TABLE research_extractions (
    id                    TEXT PRIMARY KEY,
    document_id           TEXT NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
    method                TEXT NOT NULL,
    algorithm_version     TEXT NOT NULL,
    extraction_key        TEXT NOT NULL,
    model_invocation_id   TEXT REFERENCES model_invocations(id) ON DELETE SET NULL,
    claims_created        INTEGER NOT NULL DEFAULT 0,
    claims_preserved      INTEGER NOT NULL DEFAULT 0,
    claims_dropped        INTEGER NOT NULL DEFAULT 0,
    artifact_id           TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    warnings_json         TEXT NOT NULL DEFAULT '[]',
    parameters_json       TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    UNIQUE (document_id, extraction_key)
);
CREATE INDEX idx_research_extractions_document ON research_extractions(document_id, created_at DESC);

CREATE TABLE research_memos (
    id                            TEXT PRIMARY KEY,
    project_id                    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id                   TEXT REFERENCES research_documents(id) ON DELETE CASCADE,
    snapshot_id                   TEXT REFERENCES repository_snapshots(id) ON DELETE SET NULL,
    version                       INTEGER NOT NULL,
    supersedes_memo_id            TEXT REFERENCES research_memos(id) ON DELETE SET NULL,
    title                         TEXT NOT NULL,
    body_markdown                 TEXT NOT NULL,
    artifact_id                   TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    source_interpretation_sha256  TEXT NOT NULL,
    readiness_json                TEXT NOT NULL DEFAULT '{}',
    created_at                    TEXT NOT NULL
);
CREATE INDEX idx_research_memos_project ON research_memos(project_id, created_at DESC);
CREATE INDEX idx_research_memos_document ON research_memos(document_id, version DESC);

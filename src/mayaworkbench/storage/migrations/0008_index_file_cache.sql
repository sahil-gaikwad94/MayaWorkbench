-- Durable per-file prepared-row cache for incremental indexing (WP5).
-- The artifact store is content-addressed by the bytes it holds, so the
-- logical cache key (indexer version + policy + path + content hash) needs its
-- own lookup table. A key change invalidates only that file's cached rows.

CREATE TABLE index_file_cache (
    cache_key TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    snapshot_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_index_file_cache_snapshot ON index_file_cache(snapshot_id);

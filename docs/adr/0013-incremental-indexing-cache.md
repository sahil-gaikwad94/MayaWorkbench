# ADR 0013 — Durable per-file index cache (WP5)

- Status: Accepted
- Date: 2026-11-21
- Applies to: `indexing/indexer.py`, `storage/migrations/0008_index_file_cache.sql`,
  `storage/stores.py`

## Context

WP5 required content-hash-based incremental indexing: re-index changed files, rebuild only
affected edges, persist commit/file hashes and parser version, and invalidate only affected
language records on a parser upgrade. The indexer already rebuilt all derived rows in one
atomic transaction and spilled prepared batches to a JSONL file, so the expensive part was
decode + AST/heuristic parsing in the prepare phase.

## Decision

- Serialize each file's prepared batch (files, lines, symbols, edges, aliases, issues,
  manifest entries) into a content-addressed artifact.
- Store a lookup from a logical cache key to that artifact in the `index_file_cache` table.
  The key is `sha256(INDEXER_VERSION | discovery policy hash | store_file_content flag |
  snapshot_id | path | sha256(file bytes))`. Every input that can change prepared rows is
  in the key; a parser upgrade or policy change invalidates affected files, not the repo.
- Reuse cached prepared rows on re-index, while still rebuilding all derived rows inside
  the single transaction. `IndexReport.files_reused` and index metadata record reuse.
- Scope reuse to the **same snapshot**: file and symbol ids are derived from the snapshot
  id, so a new commit rebuilds from source. This preserves correctness after rename, move
  and deletion; cross-snapshot id remapping is not attempted.

## Consequences

- Measured on a generated 120-file repository: cold 1531 ms → warm re-index 246 ms
  (~6.2x), with the write phase unchanged (34–45 ms). See `docs/wiki/performance.md`.
- A version or policy change invalidates every cached key; a changed invalidates only
  that file. Tests verify reuse count, invalidation and stale-symbol removal.
- `test_parsing_runs_outside_the_index_write_transaction` now forces a cache miss so the
  original guarantee is still exercised.

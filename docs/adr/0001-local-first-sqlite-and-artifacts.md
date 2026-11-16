# ADR 0001 — Local-first SQLite with a content-addressed artifact store

Status: accepted (Phase 1)

## Context

MayaWorkbench must persist projects, snapshots, indexes, QA records and evidence
on a developer machine with no server, no account and no background daemon. It
also stores large payloads (file content, diffs, manifests, context packs,
answers) that must remain identifiable and tamper-evident.

Options considered:

1. **SQLite + files for blobs** — zero-ops, single file, FTS5 available.
2. PostgreSQL — strong relational features, but requires a service and a
   deployment story that contradicts local-first.
3. JSON files per entity — simple, but no transactions, no FTS, poor querying.
4. Embedded KV (DuckDB, LMDB) — fast analytics/KV, but weaker relational and
   FTS ergonomics for this workload.

## Decision

Use SQLite (WAL, foreign keys on, `synchronous=NORMAL`) for all relational
state, and a content-addressed directory (`artifacts/<sha[:2]>/<sha[2:4]>/<sha>`)
for blobs, with metadata rows in SQLite.

Rules that follow from it:

- All SQL lives in `storage/stores.py`; the rest of the code sees models.
- Migrations are packaged SQL files with checksums; editing applied migrations
  is refused.
- Blob writes are atomic (temp + rename + fsync) and rows commit after bytes.
- Blob reads re-hash and fail with `ConflictError` on mismatch.
- FTS5 is a derived table (`search_lines`), rebuildable from `file_lines`.

## Consequences

- Positives: no service to run, portable state (copy `$MAYA_HOME`), transactional
  index rebuilds, real full-text search, simple testing with temp directories.
- Negatives: one writer at a time; large repositories need batching; the
  database and blob directory must be backed up together; no built-in
  encryption at rest (relies on OS/file permissions).
- Revisit if: multi-user/hosted access or concurrent agent runs become
  requirements (would need a server database and object storage, with row-level
  tenancy).

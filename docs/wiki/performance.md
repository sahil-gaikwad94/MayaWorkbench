# Performance measurements (dated)

## Incremental indexing (2026-11-21)

Measured with a generated 120-file Python repository, same process/hardware, Python 3.12.3.
Command: a small script using `Services.open`, `repository_service.attach/create_snapshot`,
`indexer.index`, timing `time.monotonic()` around `index()`.

| Run | Total | Prepare | Write | files_reused |
|---|---|---|---|---|
| Cold (fresh `$MAYA_HOME`) | 1531 ms | 1410 ms | 45 ms | 0 |
| Warm, same snapshot re-indexed | 246 ms | 134 ms | 34 ms | 120 |

Result: ~6.2x faster re-index; the inserted-row write phase is unchanged (~34–45 ms), so the
gain comes from skipping decode/parse in the prepare phase. This clears the skill's
"beat the noise" bar (the delta is far larger than run-to-run variance), so the cache is
kept. The correctness gate also passed: 380 tests green.

## Cache key and staleness

Key = sha256 of `INDEXER_VERSION | discovery policy hash | store_file_content flag |
snapshot_id | path | sha256(file bytes)`, stored in `index_file_cache` (migration 0008)
pointing at a content-addressed artifact of prepared rows. Every input that changes the
prepared rows is in the key. A parser/indexer version bump invalidates all cached records;
a policy change invalidates the affected files; a changed file invalidates only itself.
Reuse is scoped to the same snapshot because symbol/file ids are snapshot-derived — a new
commit rebuilds from source, which preserves correctness after rename/delete.

## Guard

`tests/test_incremental.py` verifies reuse count, invalidation on version change, and stale
symbol removal. `tests/test_scale_guards.py` forces a cache miss so it still proves parsing
runs outside the write transaction.

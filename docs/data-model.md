# Data model

Storage is a single SQLite database at `$MAYA_HOME/maya.db` (WAL journal,
foreign keys on, synchronous=NORMAL) plus content-addressed artifact files
under `$MAYA_HOME/artifacts/`.

Design rules:

- ULID text primary keys, sortable by creation time.
- Booleans are 0/1 integers; timestamps are ISO-8601 UTC text.
- Large payloads (file content, patches, manifests, context packs, answers) live
  in the artifact store; the database stores identity, hashes and relationships.
- Enumerations are validated by Pydantic before they reach SQL, and checked by
  the schema comment that they are part of the persisted contract.
- Everything except `projects`, `repositories`, `artifacts` and events is
  derived from exactly one snapshot and is deleted/replaced atomically.

## 1. Tables

### `projects`

One local workspace. `slug` is unique and used for lookups; `settings_json` is
reserved for per-project overrides (currently unused and validated as JSON).

### `repositories`

A local Git repository attached to a project. `(project_id, root_path)` is
unique, so attaching the same path twice is idempotent. `remote_url` and
`default_branch` are metadata only; Phase 1 never fetches.

### `repository_snapshots`

The core immutability record:

| Column | Meaning |
| --- | --- |
| `kind` | `committed` or `worktree` |
| `status` | `CREATED` → `INDEXING` → `INDEXED`, or `FAILED` |
| `commit_sha`, `tree_sha` | Resolved Git identity at capture time |
| `branch`, `base_commit_sha` | Context for worktree snapshots |
| `dirty` | `git status --porcelain` was non-empty at capture |
| `includes_worktree_patch` | Content includes uncommitted changes |
| `patch_hash` | `sha256(diff \|\| 0x00 \|\| manifest_json)` for worktree snapshots |
| `patch_artifact_id` | Git diff artifact (`SNAPSHOT_PATCH`) |
| `untracked_manifest_artifact_id` | `{path: {sha256, size}}` artifact |
| `included_paths_json` | Paths whose content the snapshot covers |
| `submodules_json` | Path → recorded commit for submodule entries |
| `indexer_version` | Version of the parsing/indexing contract |
| `file_count`, `symbol_count`, `total_bytes` | Index summary |
| `error` | Last failure text (kept even when a prior index is still valid) |
| `index_metadata_json` | Commit subject, author time, discovery policy hash |

An equivalent snapshot is reused: equality is `(kind, commit, tree, patch_hash,
included paths, submodules)` for the same repository.

### `indexed_files`

One row per file in a snapshot, unique by `(snapshot_id, path)`. Stores
language, size, line count, content SHA-256, optional Git blob SHA and mode,
`status` (`PARSED`/`LEXICAL_ONLY`/`SKIPPED`/`ERROR`), `parse_coverage`
(`FULL`/`PARTIAL`/`LEXICAL_ONLY`/`NONE`), classification flags
(`is_test`, `is_doc`, `is_config`, `is_binary`), and `skip_reason`.
`content_artifact_id` points at the exact bytes that were read when the file
was stored as an artifact.

### `file_lines`

Raw line text for exact and regex search, one row per `(file_id, line_no)`.
Lines longer than 8,000 characters are truncated with a `LINE_TRUNCATED` issue.
This table is the ground truth for citation quote verification.

### `search_lines` (FTS5)

A standalone FTS5 table (`unicode61`) over identifier-expanded tokens:
`snapshot_id`, `file_id`, `line_no` are `UNINDEXED`, `tokens` is indexed.
Expansion splits `CausalAttentionModule` into
`causal attention module causalattentionmodule …` while keeping the raw
identifier, so both natural-language and identifier queries work. Raw text
stays in `file_lines`; the FTS table never replaces it.

### `symbols`

Module/class/function/method/constant definitions with exact 1-based inclusive
spans, signature text, docstring, decorators, confidence and optional parent
symbol (methods point at their class). Unique semantics are enforced by the
indexer, not by a database constraint, because qualified names can legitimately
repeat in different scopes.

### `symbol_edges`

`IMPORTS`, `CALLS`, `INHERITS`, `DECORATES` edges from a source symbol/file to a
target name and, when resolved, a target symbol/file. `confidence` is `HIGH`
(exact definition under the stated rule), `MEDIUM` (module resolved, symbol
name unique project-wide) or `LOW` (unresolved / external). `provenance` is
`ast` for Phase 1.

### `import_aliases`

Index facts for resolution: `(file_id, alias) → (module, imported_name, level,
is_from)`. They let a call to `CausalAttention` resolve through
`from attention import CausalAttention` without re-reading the file. They are
deleted with the snapshot.

### `index_issues`

Every skip, truncation, parse failure and unresolved import, with severity and
a human-readable detail. `file_id` is null for snapshot-level issues.

### `workflow_events`

Append-only log: `seq` (autoincrement order), `event_id` (ULID), type, project,
optional repository/snapshot, entity type/id, actor kind/name, correlation id,
schema version, JSON payload. No update or delete path exists in the service
layer.

### `model_invocations`

One row per attempted model call, including refused and failed attempts.
Records provider, model, purpose, status, token counts, latency, retry count,
error class, provider request id, and cost only when the user supplied prices
(with `cost_basis`). It never stores prompts, completions or API keys.

### `qa_records`

One row per question: snapshot, status, answer markdown, citations JSON,
uncertainties, unsupported claims, model invocation (nullable), answer artifact,
context token estimate/budget, and details JSON. Rows are never deleted by
normal operation.

## 2. Relationships

```
projects 1─* repositories 1─* repository_snapshots 1─* indexed_files 1─* file_lines
                                                  1─* symbols 1─* symbol_edges
                                                  1─* import_aliases
                                                  1─* index_issues
projects 1─* workflow_events (project_id required; repo/snapshot optional)
projects 1─* model_invocations 1─? qa_records
projects 1─* qa_records *─1 repository_snapshots
artifacts 1─? repository_snapshots (patch, manifest)
artifacts 1─? qa_records (answer)
artifacts 1─? indexed_files (content)
```

Deleting a project cascades to repositories, snapshots, files, lines, symbols,
edges, aliases, issues, invocations and QA records. Artifact blobs are not
deleted automatically in Phase 1 (retention is explicit metadata only).

## 3. Artifact store

Layout: `artifacts/<sha[:2]>/<sha[2:4]>/<sha256>` — content addressed, written
via a temp file + atomic rename, deduplicated by `(sha256, kind)`.

| Kind | Content |
| --- | --- |
| `FILE_CONTENT` | Exact bytes of an indexed file (optional) |
| `SNAPSHOT_PATCH` | `git diff --binary` output for a worktree snapshot |
| `UNTRACKED_MANIFEST` | JSON `{path: {sha256, size}}` for untracked files |
| `FILE_MANIFEST` | Index manifest: per-file hashes, policy, commit, indexer version |
| `INDEX_MAP` | Repository map exported by `maya inspect --save` |
| `CONTEXT_PACK` | Full retrieval context handed to the model (or digest) |
| `QA_ANSWER` | Answer record payload, including rejected model text |

Every read re-hashes the bytes and raises `ConflictError` on mismatch; a missing
blob raises `NotFoundError`. Writes are fsync'd before the row is committed, so
a committed artifact row always has bytes on disk.

## 4. Migrations

Migrations are ordered `NNNN_name.sql` files packaged with the module. Applied
ones are recorded in `schema_migrations(version, name, checksum, applied_at)`.
On startup:

1. create the table if absent;
2. compare the checksum of every applied migration with its file;
3. refuse to run on mismatch (`DatabaseError`);
4. apply pending migrations inside one transaction per file.

`schema_version` (currently `1`) is stored in `PRAGMA user_version` and exposed
by `maya db info` / `maya doctor`. Editing a released migration is not allowed;
behaviour changes require a new file.

## 5. Retention

`retention` on artifacts (`PROJECT`, `TASK`, `EPHEMERAL`) is recorded now so a
future cleanup command can be safe. Phase 1 has no deletion command: removing
`$MAYA_HOME` is the documented way to delete local data. Patches, manifests and
file content can be large; a 96 MB total discovery budget bounds how much a
single snapshot can pull into the index.

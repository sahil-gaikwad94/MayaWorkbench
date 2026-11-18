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
projects 1─* research_documents 1─* source_passages
projects 1─* research_claims *─? research_documents
research_claims *─* research_claims (claim_dependencies, parent_claim_id)
research_claims 1─* claim_revisions / claim_risk_factors
research_claims 1─* claim_mappings 1─* mapping_evidence
projects 1─* research_memos *─? research_documents / repository_snapshots
artifacts 1─? research_documents (source, text, extraction report)
artifacts 1─? research_claims extractions (claim set)
artifacts 1─? claim_mappings (matrix artifact)
artifacts 1─? research_memos (memo body)
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
| `SOURCE_DOCUMENT` | Exact bytes of a registered research document version |
| `SOURCE_TEXT` | Extracted text a passage's `char_start`/`char_end` index into |
| `EXTRACTION_REPORT` | Tool, version, hashes, warnings, missing pages, metadata provenance |
| `CLAIM_SET` | Validated claims (plus dependencies and risks) from one extraction run |
| `CLAIM_MATRIX` | Claim-to-code matrix for one snapshot, including verified evidence |
| `RESEARCH_MEMO` | Rendered memo body (source interpretation + appended deviations) |

Every read re-hashes the bytes and raises `ConflictError` on mismatch; a missing
blob raises `NotFoundError`. Writes are fsync'd before the row is committed, so
a committed artifact row always has bytes on disk.

Versioning policy:

- The task workflow/approval/plan protocol is versioned as `TASK_PROTOCOL_VERSION`;
  the typed tool/command contract as `TOOL_GATEWAY_VERSION`.
- **Additive fields with defaults** — allowed in a minor bundle version.
- **Renamed/removed/retyped fields** — require a migration and a major bundle
  version; old artifacts remain readable or are explicitly marked legacy.
- **Enum members** — adding is additive; removing requires a migration that
  rewrites stored rows.
- `schema_version` on settings (`"1"`), events (`"1.0"`) and the schema bundle
  are independent and are recorded separately from the data they describe.

## 4. Migrations

Migrations are ordered `NNNN_name.sql` files packaged with the module. Applied
ones are recorded in `schema_migrations(version, name, checksum, applied_at)`.
On startup:

1. create the table if absent;
2. compare the checksum of every applied migration with its file;
3. refuse to run on mismatch (`DatabaseError`);
4. apply pending migrations inside one transaction per file.

`schema_version` (currently `2`) is stored in `PRAGMA user_version` and exposed
by `maya db info` / `maya doctor`. Editing a released migration is not allowed;
behaviour changes require a new file. `0002_research_and_claims.sql` is
purely additive: it creates the research tables and leaves Phase 1 rows
untouched.

## 5. Retention

`retention` on artifacts (`PROJECT`, `TASK`, `EPHEMERAL`) is recorded now so a
future cleanup command can be safe. Phase 1 has no deletion command: removing
`$MAYA_HOME` is the documented way to delete local data. Patches, manifests and
file content can be large; a 96 MB total discovery budget bounds how much a
single snapshot can pull into the index.

## 6. Research and claims (schema 2)

### `research_documents`

One immutable content version of a paper or note. Versions share
`document_key` and increment `version`; `supersedes_document_id` links the
chain, so an earlier quote remains citable after a new version is registered.
Columns cover bibliographic identity (`title`, `authors_json`,
`organization`, `published_at`, `doi`, `license`), origin (`media_kind`,
`origin`, `origin_uri`, `source_url`), content (`content_sha256`,
`size_bytes`), the three artifacts (source bytes, extracted text, extraction
report), extraction state (`extraction_status`, `extraction_tool`,
`extraction_version`, `extraction_warnings_json`, `missing_pages_json`,
`page_count`, `section_count`, `passage_count`) and `metadata_json`.

`metadata_json` records per-field provenance in `sources` (for example
`title: front_matter`, `title: filename`, `title: cli`) plus `extra` (such as a
PDF creation date) and retrieval facts. A filename stem used as a display title
is always flagged `TITLE_NOT_IN_SOURCE` in `extraction_warnings`.

### `source_passages`

Citable chunks of the extracted text. `char_start`/`char_end` index the
`SOURCE_TEXT` artifact; `text_sha256` hashes the raw slice and `quote_hash`
hashes the whitespace-normalised text. `page`/`page_end` are 1-based PDF pages
(null for Markdown/text). `section_path` is the heading trail, `heading_level`,
`line_start`/`line_end` and `equation_label` add structure. Kinds are
`HEADING`, `PARAGRAPH`, `LIST`, `TABLE`, `CODE`, `EQUATION`, `CAPTION` and
`CHUNK`; `chunk_index` marks oversize blocks split at sentence/word boundaries.

### `research_extractions`

One audit record per extraction run: `document_id`, `method`
(`DETERMINISTIC`/`MODEL`), `algorithm_version`, `extraction_key` (method,
parameters and content hash), optional `model_invocation_id`, claim counts,
`artifact_id` (the `CLAIM_SET`) and `parameters`.

### `research_claims`

One claim node. `claim_type` is never merged: `EXPLICIT_SOURCE_CLAIM`,
`INFERRED_REQUIREMENT`, `USER_REQUIREMENT` or `IMPLEMENTATION_DECISION`.
`source_locations_json` holds verified quotes (`passage_id`, `quote`,
`quote_hash`, page/section/equation); `basis_passage_ids_json` holds the
passages a non-explicit claim derives from. `expected_code_signals_json` and
`forbidden_code_signals_json` are the machine-checkable acceptance criteria;
`target_paths_json`/`target_symbols_json` narrow discovery. `missing_details_json`
records what the paper does not state (never filled in), and
`validation_methods_json` records how the claim would be tested. Author
(`HEURISTIC`/`MODEL`/`USER`), confidence and review state
(`NEEDS_REVIEW`/`ACCEPTED`/`REJECTED`) are separate fields; `reviewed_by`/
`reviewed_at` make the human decision auditable.

### `claim_dependencies`

Edges of `claim_id requires depends_on_claim_id`, unique per pair. Cycles,
missing targets, self-edges and cross-document edges are reported by
`validate_claim_dag` as issues; they are never silently repaired.

### `claim_revisions`

Append-only manual corrections: `revision`, `changed_by`, `reason` and
`before_json`/`after_json` snapshots of the claim. Editing a claim never
destroys the previous state.

### `claim_risk_factors`

Explicit reproduction risks attached to a claim: `category` (`DATA_ACCESS`,
`COMPUTE`, `LICENSING`, `EVALUATION`, `PAPER_DETAIL`, `OTHER`), `severity`
(`INFO`/`WARNING`/`BLOCKER`) and human-readable `detail`.

### `claim_mappings`

One current verdict per `(claim_id, snapshot_id)`: `status`
(`PRESENT`/`PARTIAL`/`ABSENT`/`UNCERTAIN`/`CONFLICTING`/`NOT_APPLICABLE`),
`status_reason`, `rationale`, `gaps`, `counter_evidence`, `validation_method`,
`rule_pack_version`, `created_by` (`DETERMINISTIC`/`MODEL`/`USER`), optional
model invocation, its own review status and a `details` JSON audit blob.
Remapping updates this row and emits an event; each run also writes an
immutable `CLAIM_MATRIX` artifact.

### `mapping_evidence`

Exact spans cited for a mapping: `snapshot_id`, `file_id`, optional
`symbol_id`/`qualified_name`, `path`, 1-based `start_line`/`end_line`,
`relation` (`IMPLEMENTS`/`SUPPORTS`/`CONTRADICTS`/`TEST_COVERS`), `strength`
(`STRONG`/`MODERATE`/`WEAK`), verbatim `excerpt`, `excerpt_sha256`, `source`
(`AST`/`LEXICAL`/`MANUAL`/`MODEL`) and `matched_signals`. Verification re-reads
those lines from the pinned snapshot and requires exact equality with the
stored excerpt and hash.

### `research_memos`

Versioned memo per `(project, document, snapshot)`: `version`,
`supersedes_memo_id`, `title`, `body_markdown`, `artifact_id`,
`source_interpretation_sha256` and the serialised readiness assessment.
Appending copies the earlier source-interpretation block verbatim after
verifying its hash; deviations are appended, never rewritten.

### Research artefacts

All research output is content-addressed and listed in the artifact table
above. Citations in a memo name a passage id and quote hash, so a reader can
re-derive the quote from the stored `SOURCE_TEXT` even after the workbench is
restored from a checkpoint.

## 7. Tasks and workspaces (schema 3)

Migration `0003_tasks_and_workspaces.sql` adds the controlled-agent records.
| Table | Purpose | Key columns |
| --- | --- | --- |
| `tasks` | User objective | `project_id`, `task_type`, `goal` |
| `task_contracts` | Immutable scope/budget authorization | `task_id`, `version`, `scope_json`, `constraints_json`, `research_context_json`, `verification_checks_json`, `content_sha256` |
| `task_plans` | Reviewable implementation plan | `run_id`, `version`, `plan_sha256`, `artifact_id` |
| `workflow_runs` | One attempt and its state | `state`, `base_commit_sha`, `plan_id`, `workspace_id`, usage counters |
| `approval_requests` | Durable scoped human authorization | `action`, `status`, `spec_sha256`, `nonce`, `expires_at`, `consumed_at` |
| `execution_workspaces` | Task-owned detached worktree | `run_id` (unique), `path`, `base_commit_sha`, `status`, `patch_artifact_id` |
| `tool_invocations` | Every gateway call | `tool_name`, `arguments_json`, `arguments_sha256`, `status`, `output_artifact_id` |
| `task_diffs` | Candidate patch and review facts | `patch_artifact_id`, `patch_sha256`, `files_json`, `scope_violations_json`, `original_checkout_unchanged` |
| `task_reviews` | Structural review findings | `diff_id`, `scope_ok`, `secrets_found`, `findings_json` |
| `worker_jobs` | Durable worker queue | `run_id`, `status`, `attempts`, `lease_owner`, `lease_expires_at` |

`workflow_events` gained nullable `task_id` and `run_id` columns. Existing
events keep their entity ids and remain readable. Task/contract/plan/workspace
tables use foreign keys with restrictive deletes where the evidence must not be
silently orphaned, and cascade only from project/task ownership.

The candidate patch, tool outputs, diff review and exported bundle are
content-addressed artifacts (`TASK_PATCH`, `TOOL_OUTPUT`, `COMMAND_OUTPUT`,
`DIFF_REVIEW`, `TASK_CONTRACT`, `IMPLEMENTATION_PLAN`). The database stores
metadata and hashes; it does not store full patches or command output inline.

## 8. Verification and evidence (schema 5)

Migration `0005_verification_and_evidence.sql` adds the append-only evidence
records. `task_contracts.verification_checks_json` stores the declared
`CheckSpec` list that was hashed into the contract.

| Table | Purpose | Key columns |
| --- | --- | --- |
| `verification_runs` | One attempt over one run | `run_id`, `attempt` (unique with run), `plan_sha256`, `status`, `details_json` |
| `verification_checks` | Recorded outcome of one check | `verification_id`, `check_key`, `gate`, `kind`, `method`, `outcome`, `required`, `criteria_json`, `command_json`, `artifact_id`, `details_json` |
| `verification_findings` | Check/test/reviewer findings | `verification_id`, `check_id`, `severity`, `code`, `path`, `line`, `criterion_ref`, `source`, `details_json` |
| `verification_reports` | Immutable decision | `verification_id`, `decision`, `criteria_json`, `check_ids_json`, `blocking_json`, `limitations_json`, `json_sha256`, `report_artifact_id`, `reviewer_invocation_id` |
| `evidence_links` | Typed edge of the evidence graph | `report_id`, `from_kind`, `from_ref`, `relation`, `to_kind`, `to_ref`, `sha256` |

`CHECK`/`PATCH`/`CRITERION`/`COMMAND`/`MODEL_REVIEW` links make the report
mechanically inspectable: every criterion names the checks that support or
contradict it, and every check names the command/artifact it came from. Reports
are never updated; a new attempt writes new rows. `verification_checks` has a
unique `(verification_id, check_key)` constraint; findings cascade from their
check. Command output (stdout/stderr) lives in `COMMAND_OUTPUT` artifacts, and
evidence bundles copy the artifacts they cite.

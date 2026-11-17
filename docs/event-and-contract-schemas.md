# Events and contract schemas

Persisted shapes are contracts. They are versioned, exported as JSON Schema
(`maya schemas export <dir>`) and validated with Pydantic (`extra="forbid"`).
Changing a field's meaning requires a migration and an entry in
[docs/adr/](adr/).

## 1. Event envelope

Every workflow event is stored in `workflow_events` and exposed by
`maya events [--project] [--snapshot] [--entity] [--type] [--limit]`.

| Field | Type | Notes |
| --- | --- | --- |
| `event_id` | string (ULID) | Stable public identifier |
| `seq` | integer | Autoincrement order; not exposed as a model field |
| `occurred_at` | ISO-8601 UTC | Wall-clock time of the transition |
| `event_type` | enum | See catalogue below |
| `project_id` | string | Always present |
| `repository_id` | string? | When the event concerns a repository |
| `snapshot_id` | string? | When the event concerns a snapshot |
| `entity_type` | string | `project`, `repository`, `snapshot`, `qa_record`, … |
| `entity_id` | string | Identifier of the affected entity |
| `actor_kind` | enum | `USER`, `SYSTEM`, `MODEL`, `TOOL` |
| `actor_name` | string | e.g. `SYSTEM:indexer`, `SYSTEM:qa`, `SYSTEM:model-gateway` |
| `correlation_id` | string? | Groups events from one operation |
| `schema_version` | string | Envelope/payload version (`1.0`) |
| `payload` | object | Type-specific, small; large data goes to artifacts |

Events are append-only: there is no update/delete API. `maya events` renders the
payload as compact JSON.

## 2. Event catalogue (Phases 1–2)

| Type | Emitted when | Payload (implemented keys) |
| --- | --- | --- |
| `PROJECT_CREATED` | `maya init` / `project create` | `name`, `slug` |
| `REPOSITORY_ATTACHED` | `repo add` | `name`, `root_path`, `created`, `remote_url`, `default_branch` |
| `SNAPSHOT_PINNED` | snapshot created (or reused) | `identity`, `kind`, `dirty`, `commit_sha`, `patch_hash`, `reused`, `file_count_hint`, `warnings` |
| `INDEX_STARTED` | indexing begins | `snapshot_id`, `indexer_version` |
| `INDEX_COMPLETED` | transaction committed | `files_indexed`, `lines_indexed`, `symbols`, `edges`, `edges_resolved`, `issues`, `skipped`, `bytes_indexed`, `duration_ms`, `manifest_artifact_id`, `coverage` |
| `INDEX_FAILED` | indexing aborted | `error`, `preserved_previous_index` |
| `MODEL_INVOCATION_STARTED` | policy allowed a call | `purpose`, `provider`, `model` |
| `MODEL_INVOCATION_COMPLETED` | provider returned valid output | `provider`, `model`, `total_tokens`, `latency_ms`, `purpose` |
| `MODEL_INVOCATION_FAILED` | refusal, size limit, provider error | `refused`, `reason`, `error_class`, `message` (truncated) |
| `QA_ANSWERED` | answer with verified citations | `qa_record_id`, `status`, `citations`, `valid_citations`, `model_invocation_id`, `token_estimate` |
| `QA_EVIDENCE_ONLY` | `NO_MODEL` digest stored | same shape as above, `status = NO_MODEL` |
| `QA_REJECTED` | insufficient evidence, rejected citations, model failure, unavailable | `qa_record_id`, `status`, `citations`, `valid_citations`, `reason` |

Phase 2 adds the research transitions:

| Type | Emitted when | Payload (implemented keys) |
| --- | --- | --- |
| `SOURCE_INGESTED` | a document version is registered and extracted | `document_key`, `version`, `title`, `media_kind`, `content_sha256`, `passages`, `sections`, `page_count`, `warnings`, `extraction_tool`, `extraction_version`, `supersedes` |
| `SOURCE_EXTRACTION_FAILED` | bytes could not be turned into text; a `FAILED` version row is kept | `document_key`, `version`, `filename`, `media_kind`, `error` |
| `CLAIMS_EXTRACTED` | a deterministic or model extraction run is stored | `extraction_id`, `method`, `algorithm_version`, `claims_created`, `claims_preserved`, `claims_dropped`, `warnings`, `artifact_id` |
| `CLAIM_CREATED` | a claim is authored by hand | `claim_type`, `title`, `document_id`, `review_status`, `dependencies`, `source_locations` |
| `CLAIM_UPDATED` | a manual correction appends a revision | `revision`, `changed_fields`, `dependencies_replaced`, `risk_factors_replaced`, `reason` |
| `CLAIM_REVIEWED` | a claim is accepted or rejected | `review_status`, `note` |
| `CLAIM_DAG_VALIDATED` | `maya research dag` validates a claim graph | `claim_count`, `edge_count`, `errors`, `warnings`, `cycles` |
| `CLAIM_MAPPING_CREATED` | first mapping for a claim and snapshot | `claim_id`, `snapshot_id`, `status`, `author`, `rule_pack_version`, `evidence_spans`, `model_invocation_id`, `gaps` |
| `CLAIM_MAPPING_UPDATED` | a mapping is replaced (rules, manual or model) | `previous_status`, `status`, `author`, `evidence_spans`, `reason` |
| `CLAIM_MAPPING_REJECTED` | model adjudication was refused and the rule verdict kept | `claim_id`, `snapshot_id`, `reason`, `model_invocation_id` |
| `RESEARCH_MEMO_CREATED` | a memo version is stored | `version`, `document_id`, `snapshot_id`, `claims`, `readiness_label`, `readiness_score_version`, `artifact_id`, `appended` |

Events for tasks, approvals, patches and runs are specified for later phases and
are intentionally absent from the enum until those transitions exist (no empty
scaffolding).

## 3. Snapshot contract

`RepositorySnapshot` (JSON Schema exported) carries:

- identity: `id`, `repository_id`, `kind`, `commit_sha`, `tree_sha`, `branch`;
- worktree evidence: `dirty`, `includes_worktree_patch`, `patch_hash`,
  `patch_artifact_id`, `untracked_manifest_artifact_id`, `included_paths`,
  `submodules`;
- indexing state: `status`, `indexer_version`, `indexed_at`, counts, `error`,
  `index_metadata`.

Human-readable identity:

```
committed:  <commit_sha[:12]>
worktree:   <commit_sha[:12]>+patch:<patch_hash[:12]>
```

`patch_hash = sha256(git_diff_bytes || 0x00 || manifest_json_bytes)` where the
manifest is canonical JSON (`sort_keys`, compact separators) of
`{path: {sha256, size}}` for untracked files. This makes the identity depend on
content, not on timestamps or file order.

Worktree capture refuses to proceed if `HEAD` or `git status --porcelain`
changes between the start and end of capture; the error names the changed head
or the status difference.

## 4. QA contract

Every `ask` produces a `QaRecord` and a `QA_ANSWER` artifact containing:

```json
{
  "protocol_version": "1.0",
  "created_at": "...",
  "question": "...",
  "status": "ANSWERED | ...",
  "answer_markdown": "...",
  "citations": [
    {"path": "...", "start_line": 1, "end_line": 2, "quote": "...",
     "reason": "...", "valid": true, "validation": "verified"}
  ],
  "uncertainties": ["..."],
  "unsupported_claims": ["..."],
  "snapshot": {"snapshot_id": "...", "repository_id": "...", "kind": "committed",
                "commit_sha": "...", "patch_hash": null, "indexer_version": "1.0.0"},
  "retrieval": {"identifiers": ["..."], "phrases": [], "fts_expression": "...",
                 "text_candidates": 12, "symbol_candidates": 3, "mode": "auto",
                 "notes": ["..."]},
  "context": {"artifact_id": "...", "token_estimate": 1006, "token_budget": 8000,
               "estimator": "...", "files_covered": ["..."], "truncated": false,
               "excluded_count": 4},
  "model": {"used": true, "invocation_id": "...", "provider": "...",
             "model": "...", "structured_output": true}
}
```

Rejected model text is stored in the artifact under `rejected_model_answer` and
the record status is `CITATIONS_REJECTED`; it is never presented as the answer.

Model output is requested as JSON with this schema:

```json
{
  "answer_markdown": "string",
  "citations": [{"path": "string", "start_line": 1, "end_line": 2,
                  "quote": "string", "reason": "string"}],
  "uncertainties": ["string"],
  "unsupported_claims": ["string"]
}
```

`answer_markdown` is bounded in length by the provider output limit, and the
whole object is re-validated locally before use.

## 5. Retrieval contract

`ContextPack` items are `(path, file_id, start_line, end_line, text, reason,
kind, score, token_estimate, truncated)` where `text` is rendered as
`<line_no>: <line>` for every line in the span. This rendering is what citation
validation parses; changing it is a breaking change to citation checking and
requires updating both sides plus tests.

`RetrievalDiagnostics` records the question, extracted identifiers and phrases,
the FTS expression actually used, candidate counts, mode and free-form notes
(e.g. "context was truncated to a 600-token budget"). It is stored in the answer
artifact so ranking decisions can be audited.

## 6. Research contracts (Phase 2)

`ResearchDocument` carries identity (`document_key`, `version`,
supersedes), bibliographic fields, media/origin, content hash, the three
artifact ids, extraction status/tool/version/warnings/missing pages and
`metadata`. `metadata.sources` is the provenance map; consumers must treat a
field whose source is `filename` or `cli` as supplied, not as stated by the
paper.

`SourcePassage` carries `page`/`page_end`, `section_path`, `section_title`,
`heading_level`, `line_start`/`line_end`, `char_start`/`char_end`, `text`,
`text_sha256`, `quote_hash`, `equation_label` and `chunk_index`. The invariant is
machine-checked at ingestion:

```
stored_text[char_start:char_end] == passage.text
quote_hash == sha256(normalise(passage.text))
```

`ResearchClaim` carries the category, title/statement,
`implementation_requirement`, verified `source_locations`, `basis_passage_ids`,
declared/forbidden code signals, target paths/symbols, `missing_details`,
`validation_methods`, `derivation_note`, author/confidence and review state.
The model-extraction payload is a separate schema (`ClaimExtractionPayload`)
whose claims may only be `EXPLICIT_SOURCE_CLAIM` or `INFERRED_REQUIREMENT`;
a model may not mint user requirements or engineering decisions.

`ClaimDagValidation` reports `issues` (typed codes with severity), `cycles`,
`topological_order`, `unorderable`, `depth`, `roots` and `leaves`. The JSON
serialisation also exposes `valid` through the CLI, derived from the issues.

`ClaimToCodeMatrix` is the review surface: one row per claim with its status,
reason, gaps, counter-evidence, rule pack, author and verified evidence spans
(`VerifiedEvidence` = evidence + `verified` + `verification` reason). The
matrix is written as a `CLAIM_MATRIX` artifact on every run.

`ReadinessAssessment` carries `score_version`, the weight map, per-factor
state/risk/explanation/evidence, `overall_risk` (nullable),
`label`, blockers, assumptions, limitations, claim/mapping counts,
`known_weight` and `is_probability = false`.

Memos use explicit markers:

```
<!-- maya:interpretation:start --> … <!-- maya:interpretation:end -->
<!-- maya:deviations:start --> … <!-- maya:deviations:end -->
```

`source_interpretation_sha256` is the SHA-256 of the interpretation block's
inner text. A later version must reproduce that block byte for byte or the
append is refused.

## 7. Export and versioning

```bash
maya schemas export ./schemas
```

writes one `<Model>.schema.json` per contract plus `schemas.json`:

```json
{"bundle_version": "1.0", "models": {"RepositoryMap": "RepositoryMap.schema.json", ...}}
```

Exported models include `Project`, `Repository`, `RepositorySnapshot`,
`IndexedFile`, `Symbol`, `SymbolEdge`, `IndexIssue`, `Artifact`,
`WorkflowEvent`, `ModelInvocation`, `Citation`, `QaRecord`, `ContextPack`,
`ContextItem`, `RetrievalDiagnostics`, `RepositoryMap`, `IndexReport`,
`ResearchDocument`, `SourcePassage`, `ResearchClaim`, `ClaimDependency`,
`ClaimRevision`, `ClaimRiskFactor`, `MappingEvidence`, `ClaimMapping`,
`ResearchExtraction`, `ResearchMemo`, `ExtractionReport`,
`ClaimExtractionSummary`, `ClaimExtractionPayload`, `ClaimDagValidation`,
`ClaimToCodeMatrix`, `ReadinessAssessment`, `MappingAdjudicationPayload` and
`ModelEvidence`.

The bundle version is `1.1` (Phase 2 added schemas; no Phase 1 schema changed
shape), while the event envelope stays at `1.0`.

Versioning policy:

- **Additive fields with defaults** — allowed in a minor bundle version.
- **Renamed/removed/retyped fields** — require a migration and a major bundle
  version; old artifacts remain readable or are explicitly marked legacy.
- **Enum members** — adding is additive; removing requires a migration that
  rewrites stored rows.
- `schema_version` on settings (`"1"`), events (`"1.0"`) and the schema bundle
  are independent and are recorded separately from the data they describe.

## 8. Task and workspace contracts (Phase 3)

Every task event carries the project id and, where relevant, `task_id` and
`run_id`; the event envelope is unchanged. Phase 3 adds:

`TASK_CREATED`, `CONTRACT_CREATED`, `PLAN_CREATED`, `PLAN_APPROVED`,
`APPROVAL_REQUESTED`, `APPROVAL_GRANTED`, `APPROVAL_DENIED`, `APPROVAL_EXPIRED`,
`APPROVAL_CONSUMED`, `TASK_STATE_CHANGED`, `TASK_PAUSED`, `TASK_RESUMED`,
`TASK_COMPLETED`, `TASK_FAILED`, `TASK_BLOCKED`, `TASK_CANCELLED`,
`WORKTREE_CREATED`, `WORKTREE_REMOVED`, `PATCH_APPLIED`, `TOOL_INVOKED`,
`TOOL_BLOCKED`, `COMMAND_STARTED`, `COMMAND_COMPLETED`, `COMMAND_TIMED_OUT`,
`COMMAND_BLOCKED`, `DIFF_CAPTURED`, `DIFF_REVIEWED`, `WORKER_STARTED`,
`WORKER_LEASE_ACQUIRED`, `WORKER_LEASE_LOST` and `WORKER_FINISHED`.

Exported JSON Schemas (bundle `1.2`) cover `Task`, `TaskContract`, `TaskPlan`,
`WorkflowRun`, `ApprovalRequest`, `ExecutionWorkspace`, `ToolCall`, `TaskDiff`,
`TaskReview` and `WorkerJob`. `AgentAction` and `PlanPayload` are the structured
model contracts used by the worker; they are internal but versioned with
`TASK_PROTOCOL_VERSION`.

Safety-relevant payload rules:

- approval events contain the action, spec hash, expiry and scope, never a
  command's argv, file content or nonce;
- tool-call rows store argument hashes and redacted summaries for edits, plus
  argv for commands; output lives in artifacts;
- a `DIFF_REVIEWED` event carries counts and booleans, not an acceptance claim.

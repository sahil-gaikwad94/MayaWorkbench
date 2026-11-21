# Architecture

## 1. Shape

MayaWorkbench is a local Python application with a strict dependency direction:

```
CLI (argparse, rendering)
  └── application services (app/container.py)
        ├── repositories/  Git, discovery, snapshot capture
        ├── indexing/      parsing, edge resolution, map
        ├── retrieval/     search, context packing
        ├── qa/            prompts, citation validation, answer pipeline
        ├── research/      documents, passages, claims, DAG, readiness, memos
        ├── mapping/       claim signals, evidence discovery, rule pack
        ├── experiments/   protocols, runner, statistics, comparison, bundles
        ├── memory/        scoped statements, confirmation, staleness
        ├── models/        provider gateway and adapters
        └── storage/       SQLite, stores, artifact store
              └── domain/  enums and persisted models (no I/O)
```

`domain/` imports nothing from the layers above it. `models/` knows nothing
about the CLI. The CLI never executes SQL. The composition root
(`app/container.py`) is the only place that wires concrete implementations
together, which is what makes the providers replaceable in tests.

## 2. Module responsibilities

| Module | Responsibility | Notable invariants |
| --- | --- | --- |
| `config.py` | Resolve `$MAYA_HOME`, load/save strict settings, env overrides | Secrets never persisted; unknown keys rejected |
| `ids.py` | Prefixed ULIDs, stable hashes | IDs sort by creation time; hashes are `sha256` |
| `domain/enums.py` | Persisted vocabulary | Changing a member needs a migration |
| `domain/models.py` | Pydantic contracts for every persisted row | `extra="forbid"`; validation at the boundary |
| `storage/db.py` | Connections, PRAGMAs, migrations, transactions | One migration transaction per file; checksums verified |
| `storage/stores.py` | All SQL, row↔model mapping | No SQL outside this module and migrations |
| `storage/artifacts.py` | Content-addressed blobs | SHA-256 verified on read and write |
| `repositories/git.py` | Git subprocess adapter | argv only, no shell, sanitised env, batch reads |
| `repositories/discovery.py` | Choose indexable files | Every skip becomes an `IndexIssue` |
| `repositories/source.py` | Read pinned content | Committed = Git objects; worktree = verified disk reads |
| `repositories/snapshots.py` | Attach, snapshot, verify | Never modifies the checkout |
| `indexing/python_parser.py` | AST → symbols/imports/calls | Parse errors returned, never raised |
| `indexing/resolve.py` | Edge → definition resolution | Confidence reflects the evidence used |
| `indexing/indexer.py` | Atomic rebuild of derived rows | One transaction; failure preserves prior index |
| `indexing/map.py` | Deterministic repository map | Counts come from stored rows, not re-scan |
| `retrieval/search.py` | FTS/exact/regex/symbol search | No result means no result |
| `retrieval/context.py` | Budgeted context pack | Budget respected; exclusions recorded |
| `retrieval/service.py` | Retrieval orchestration | Diagnostics name every stage |
| `models/gateway.py` | Policy, invocation accounting, events | `LOCAL_ONLY` refuses before adapter construction |
| `qa/citations.py` | Deterministic citation verification | No model judgement involved |
| `qa/service.py` | Question → context → answer → record | No evidence ⇒ no answer |
| `research/pdf.py` | PDF text extraction | Missing text is reported, never guessed |
| `research/fetch.py` | URL/DOI retrieval | http(s) only, public addresses only, bounded |
| `research/ingestion.py` | Document versions, artifacts, passages | A version is immutable; quotes index stored text |
| `research/claims.py` | Extraction, authoring, review, revisions | Explicit claims carry verified quotes only |
| `research/dag.py` | Claim graph validation | Cycles and missing edges are reported |
| `research/readiness.py` | Reproduction risk assessment | Unknown factors stay unknown |
| `research/memo.py` | Versioned memo generation | Prior interpretation is reused verbatim |
| `mapping/signals.py` | Whole-token behavioural signals | Prose never counts as code evidence |
| `mapping/rules.py` | Deterministic mapping rule pack | `PRESENT` needs definition-level code evidence |
| `mapping/service.py` | Mapping persistence, evidence, adjudication | Evidence is re-verified against the snapshot |
| `experiments/protocol.py` | Comparable protocol surface and fairness classification | An undeclared difference is `NOT_COMPARABLE` |
| `experiments/runner.py` | Manifest-hashed execution in a fresh worktree | Inputs verified before the command starts |
| `experiments/metrics.py` | Metric extraction from stdout/JSON/CSV | A missing declared metric makes the run unusable |
| `experiments/stats.py` | t distribution, t-tests, Hedges' g, Holm | No p-value is fabricated for zero variance |
| `experiments/comparison.py` | Verdicts, paired/Welch design, effect sizes | The primary metric alone drives the verdict |
| `experiments/report.py` | Markdown/JSON experiment report | Every claim is tied to declared metrics and runs |
| `experiments/bundle.py` | Reproduction export and verification | Both arm commits and every file hash are checked |
| `experiments/service.py` | Definition, run, compare, memory orchestration | Definitions are immutable and approval is explicit |
| `memory/service.py` | Scoped project statements and provenance | Only the user confirms; commit-scoped memory goes `STALE` |

## 3. Lifecycles

### 3.1 Initialisation

`maya init` resolves paths, creates the home directory, opens SQLite (WAL),
applies checksummed migrations, writes a default `config.json`, creates the
project, and emits `PROJECT_CREATED`. Re-running is idempotent for an existing
project slug.

### 3.2 Snapshot capture

```
attach(repo)  → resolve toplevel, HEAD, branch, remote
create_snapshot(repo, include_working_tree?)
   ├─ verify HEAD / `git status --porcelain` unchanged across capture
   ├─ committed: ls-tree -r -z HEAD + cat-file --batch (binary-safe)
   ├─ worktree:  git diff --binary + `git status --porcelain -z`
   │             + untracked manifest {path: {sha256, size}}
   │             + patch_hash = sha256(diff || 0x00 || manifest_json)
   ├─ discovery: gitignore (via git), excluded dirs/globs, secrets,
   │             binary detection, size/count budgets
   ├─ store patch and untracked manifest as artifacts (worktree only)
   ├─ compute identity and reuse an equivalent snapshot if present
   └─ emit SNAPSHOT_PINNED
```

Committed snapshots read Git objects, so they are immune to later edits of the
working tree. Worktree snapshots record each file's content hash and the patch
that produced it; indexing re-verifies HEAD, status and patch hash first.

### 3.3 Indexing

```
index(snapshot)
  ├─ status = INDEXING (committed separately so crashes are visible)
  ├─ re-verify snapshot sources (worktree: HEAD/status/patch hash)
  ├─ BEGIN
  │    ├─ delete derived rows for this snapshot only
  │    ├─ insert discovery issues
  │    ├─ per file: detect language, read pinned content, hash, classify,
  │    │            split lines (FTS + line table), parse Python AST
  │    ├─ insert symbols, import aliases, edges (unresolved initially)
  │    ├─ resolve edges (same file → imported alias → unique project-wide)
  │    ├─ store manifest artifact
  │    └─ snapshot.status = INDEXED, coverage and counts
  └─ COMMIT
  on failure: ROLLBACK, restore status, record error, emit INDEX_FAILED
```

The whole rebuild is one transaction. A crash mid-index therefore leaves the
previous index intact and the snapshot visibly not `INDEXED`.

### 3.4 Question answering

```
ask(question, snapshot)
  ├─ identifiers + quoted phrases + compound identifier forms
  ├─ symbol search, lexical search (AND first, OR broadening when thin)
  ├─ one-hop expansion through resolved edges
  ├─ context pack: merge overlapping spans, add surrounding lines, greedy
  │                budget fill, record every excluded candidate and reason
  ├─ no evidence → INSUFFICIENT_EVIDENCE (no model call)
  ├─ model disabled/policy-blocked → NO_MODEL (deterministic digest,
  │                                     retrieval citations marked unverified)
  ├─ model call → structured JSON → schema validation
  ├─ citation verification against the exact context pack
  │     any invalid → CITATIONS_REJECTED (answer withheld, still stored)
  └─ persist QA record + answer/context artifacts + events
```

The context pack is the authority for citations. A model cannot cite a file it
was not given, and cannot widen a line range beyond the span it received.

## 4. Invariants

1. **I1 — Snapshot determinism.** Indexing never reads content that is not
   covered by the snapshot identity.
2. **I2 — Atomic derived state.** All rows for `(snapshot_id)` are replaced in
   one transaction or not at all.
3. **I3 — Faithful spans.** Stored line numbers are 1-based and inclusive, and
   match the file bytes read for that snapshot.
4. **I4 — Verified artifacts.** Reads re-hash content; a mismatch is an error,
   not a warning.
5. **I5 — Verified citations.** `ANSWERED` implies every citation is valid.
6. **I6 — Policy before I/O.** `LOCAL_ONLY` blocks remote calls before an
   adapter is constructed, and refusal is recorded.
7. **I7 — No silent exclusions.** Anything skipped, truncated or unresolved is
   persisted with a reason.
8. **I8 — Checkout immutability.** No command writes to the repository; only
   `git` read commands are issued.
9. **I9 — Durable evidence.** Answers, contexts, invocations and events survive
   process exit and are inspectable later.
10. **I10 — No fabricated relevance.** Empty retrieval returns empty evidence.
11. **I11 — Exact paper quotes.** An explicit claim's quote appears verbatim in
    a stored passage; the quote hash is re-derived, never trusted.
12. **I12 — Mechanical mapping.** `PRESENT` requires every declared signal in
    executable code, at least one in a definition-level span with full parser
    coverage, and no forbidden signal. Derived signals cannot produce it.
13. **I13 — Unknowns stay unknown.** A readiness factor with no recorded facts is
    `UNKNOWN`; no overall value is reported below 75% known weight.
14. **I14 — Immutable interpretations.** A memo version reuses the earlier
    source interpretation byte for byte or the append is refused.

## 5. Failure behaviour

| Failure | Behaviour |
| --- | --- |
| Git missing / not a repo | `RepositoryError`, exit 1, no partial rows |
| No commits | snapshot refused with a clear message |
| Working tree moved after snapshot | `SnapshotError` → indexing refused, snapshot `FAILED` |
| File unreadable/undecodable | file marked `ERROR`/`LEXICAL_ONLY` + issue; index continues |
| Python syntax error | lexical indexing continues; `PARSE_ERROR` issue |
| Edge resolution crash | transaction rollback, prior index preserved, `INDEX_FAILED` |
| Model timeout/HTTP error | retries with backoff, then `FAILED` record, no answer text |
| Model output violates schema | discarded, record `FAILED`, invocation details kept |
| Citation invalid | answer withheld, status `CITATIONS_REJECTED`, raw answer kept in artifact |
| Artifact tampered | `ConflictError` on read |

## 6. Performance characteristics

- Indexing is two-phase. Phase 1 reads Git objects, decodes, parses and writes
  content artifacts with **no write transaction open**, spilling prepared rows
  to a private JSONL file in bounded batches. Phase 2 replays those rows inside
  one short `BEGIN IMMEDIATE` transaction, so readers see either the old index
  or the new one and other writers are not blocked for the parse. Merely
  holding rows in memory would scale with repository size; the spill file keeps
  memory bounded by one batch. `prepare_ms`/`write_ms` are recorded on the
  index report, snapshot metadata and `INDEX_COMPLETED` event.
- FTS search is index-backed (`bm25`); exact/regex search scans the stored
  per-line table for the snapshot, which is linear in indexed lines but avoids
  false negatives from tokenisation.
- Context packing is greedy over ranked candidates with a hard token budget, so
  model input size is bounded regardless of repository size.
- Measured numbers on the fixture repository are in
  [phase-1-report.md](phase-1-report.md); no extrapolated claims are made here.

## 7. Extension points

- **New language parser**: implement the parser protocol used by
  `indexing/languages.py`, register it by extension, return the same symbol and
  edge shapes. Coverage labels already distinguish AST from lexical files.
- **New provider**: implement `ModelProvider` (`generate`, `capabilities`) and
  register it in the gateway factory; the domain contract does not change.
- **New search mode**: add a mode to `retrieval/search.py` and expose it in the
  CLI; retrieval scoring already merges multiple candidate sources.
- **Verification check**: add a built-in to `verification/registry.py` (with a
  gate, kind and evidence) or a new invariant template returning a validated
  script; the report and evidence graph consume the same `VerificationCheck`
  rows. New checks must not mutate a past attempt.

## 8. Task execution (Phase 3)

```text
CLI task create/start          CLI task run
      |                             |
      v                             v
TaskService  ---> WorkflowService (state machine + events)
      |                             ^
      +--> ApprovalService ---------+
      +--> PlanService (model/JSON)
      +--> WorktreeManager --------> detached Git worktree
      +--> WorkerJobStore ---------> separate `mayaworkbench.worker` process
                                           |
                                           v
                                    ControlledAgent
                                           |
                                    ToolGateway -----> CommandRunner
                                           |            (argv, timeout, caps)
                                           v
                                    artifacts + tool_invocations
```

Note: in Phase 3 the diagram reads top-to-bottom as an architectural map, not
as a live pipeline. Control flow is single-threaded per run: the CLI enqueues,
the worker owns the state machine and agent loop, and the CLI only observes.

The CLI never executes repository code. It creates durable task/run records,
starts the worker and then polls the database. The worker owns the agent loop
and closes the run with a state transition; only `WorkflowService` calls
`compare_and_set_state`.

## 9. Verification and evidence (Phase 4)

```text
TaskService.verify_run
      |
      v
VerificationService
  |-- built-in checks (scope, approvals, budget, snapshot, diff, report)
  |-- declared checks --> BaselineWorkspace (git worktree @ base + patch)
  |                     --> CommandRunner (candidate)
  |-- test integrity  --> base/candidate content from Git objects
  |-- reviewer        --> read-only prompt + structured findings
  |-- report          --> decision rules + evidence links
  v
verification_runs / checks / findings / reports / evidence_links
      |
      v
bundle (report.json, checks.json, criteria.json, patch.diff, artifacts)
```

Attempts are append-only; a re-run inserts attempt *n+1*. Built-ins always run;
command and ML-invariant checks only run when declared in the immutable task
contract, and command checks additionally require a consumed
`COMMAND_EXECUTION` approval matching the current contract/plan hash. Baseline
and candidate execution go through the same `CommandRunner`, so argv validation,
timeouts, process-group cleanup and rlimits apply to both. The reviewer is
advisory and cannot change a deterministic outcome. `WorkflowService` is still
the only writer of run state; `task finish` refuses to accept a run without a
report and gates on the fixed decision rules (with an explicit, recorded
`--override`).

## 10. Experiments and memory (Phase 5)

```text
ExperimentService
  |-- definition ---> content hash, DRAFT -> APPROVED (user) -> REVOKED
  |-- plan ----------> blockers, unknown protocol fields
  |-- run -----------> manifest (hashed before execution)
  |                    -> fresh worktree at the pinned snapshot
  |                    -> input hash verification (script, declared files)
  |                    -> CommandRunner (offline, rlimits, timeout)
  |                    -> metrics extracted; observations appended
  |-- compare -------> protocol classification
  |                    -> paired t (same seeds) or Welch
  |                    -> Holm for secondaries; Hedges' g
  |                    -> verdict + limitations
  |-- bundle --------> definitions, comparison, manifests, observations,
  |                    outputs, report; per-arm commits; root hash
  v
experiment_definitions / manifests / runs / metric_observations / comparisons
  |
  v
verification Gate F (`experiment.evidence`) and project_memories
```

A definition is a pre-registration: it is content-addressed, approved by the
user, and never edited. Each run executes one seed in a fresh worktree and
records its own manifest; the manifest is hashed before the command starts, so
a run cannot rewrite its provenance after seeing a result. The comparison
classifies every protocol field (`MATCH`, `EXPECTED_TREATMENT`,
`UNEXPECTED_TREATMENT`, `MISSING`, `UNKNOWN`) and only uses the difference
statistics fixed by the definition. Verification Gate F consumes a comparison as
mandatory evidence when the task contract asks for one.

Memory is a separate, deliberately narrow store: `FACT`, `DECISION` and
`NEGATIVE_RESULT` statements with provenance. Model-authored and
comparison-derived entries are `UNCONFIRMED`; only the user confirms them. A
memory pinned to a commit becomes `STALE` when the project moves on, and secrets
are refused before storage.

## 11. Inside the agent loop (Phase 6)

`ControlledAgent` remains one role, one action, one step. Phase 6 adds four
things around that loop:

- `tasks/orientation.py` builds a bounded `repository_orientation` payload from
the pinned index for the first steps: retrieval spans for the goal, top-level
symbols in the allowed paths, relevant test files and index issues that make a
file unreliable evidence. It is an aid, never a gate, and it says it describes
the base revision.
- `tasks/inloop.py` owns `DeclaredCheckRunner`. When a `finish` action arrives it
runs the contract's checks through the same `CommandRunner` as verification,
classifies each result as a code failure or an infrastructure failure, and
either returns typed repair feedback (bounded rounds) or records the limitation.
It never writes `verification_checks` rows.
- the loop derives `workspace_ledger` from the audited `ToolCall` rows instead of
storing it, so pause/resume and prompt truncation cannot lose what the run
already did;
- repeated read-only actions are refused before they consume a tool call and the
run blocks after four identical attempts.

The model-facing authority is unchanged: the gateway validates every argument,
approvals are consumed records, and the path policy resolves every path.

## 12. Deliberate non-architecture

- No plugin loading from repositories (would execute untrusted code).
- No daemon, no background indexing, no file watcher (Phase 1 is explicit
  commands over pinned state).
- No embeddings in Phase 1: exact identifiers and symbols are the baseline that
  must be correct before approximate similarity is introduced.

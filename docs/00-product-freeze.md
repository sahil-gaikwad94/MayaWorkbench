# Product freeze (Phase 0)

Status: frozen for Phase 0–5. Changes require a documented decision record in
[docs/adr/](adr/) and an update to the affected contract schemas.

## 1. Who this is for

**Primary user — an engineer directing an AI coding agent on a repository they
do not fully remember.** They need to know what the code actually does, where a
behaviour lives, and whether a claimed change is real, without trusting the
agent's summary and without handing the repository to a hosted service.

**Secondary user — a reviewer or research engineer** who needs a reproducible
record of what was inspected, what evidence supported an answer, and what the
tool could not see.

Not the target user (yet): teams needing multi-user permissions, hosted
dashboards, or automated merge bots.

## 2. Jobs to be done

| # | Job | Failure it replaces |
| --- | --- | --- |
| J1 | "Show me what this repository is made of, honestly." | Stale READMEs, guessed architecture, silent parser gaps |
| J2 | "Find the code behind this concept." | Grep that misses identifier variants; embeddings that hide uncertainty |
| J3 | "Answer my question using this exact revision, and cite it." | Model answers grounded in nothing, or in a different checkout |
| J4 | "Include my uncommitted work deliberately, not by accident." | Agents editing/reading a dirty tree while claiming committed state |
| J5 | "Tell me what you could not analyse." | Coverage claims that are really hopes |
| J6 | "What does this paper require, and how much of it does the code actually implement?" | Summaries that merge the paper, guesses and code into one confident story |

J7–J9 (task contracts, isolated worktrees, verified patches) are implemented in
Phases 3–4. Phase 5 adds the follow-on job "is the candidate actually better,
and can I reproduce that claim?" through pre-registered experiments and
reproduction bundles, plus scoped memory for decisions and negative results.
Labelled corpora and evaluation scoring are specified but not implemented; the
evidence records needed to score runs now exist.

## 3. Product principles (non-negotiable)

Derived from the master specification and enforced in code and tests:

1. **Evidence over confidence.** No answer is `ANSWERED` without at least one
   citation that passes deterministic verification.
2. **No self-granted authority.** The model never decides whether its own
   output is acceptable, and never chooses its own permissions or data policy.
3. **Snapshots are immutable.** Every index and answer names exactly one
   snapshot identity; content is read through hashed, pinned sources.
4. **A dirty tree is never silently mixed.** Committed snapshots exclude
   uncommitted changes (and say so); worktree snapshots capture them as a patch
   with a stable hash and refuse to index if the tree moved on.
5. **Coverage is labelled.** `FULL`, `PARTIAL`, `LEXICAL_ONLY`, `NONE` are
   recorded per file and summarised in the repository map.
6. **Failures are visible.** Skipped, truncated, unresolved and failed cases
   become persisted issues/events, never silent drops.
7. **No symbol-name-only claims.** A symbol edge is only "resolved" when a
   definition in the snapshot matches under a stated rule and confidence.
8. **Domain logic is provider- and framework-independent.** No SDK types cross
   the domain boundary; the CLI is a thin adapter over services.
9. **Local-first by default.** Remote model calls require an explicit policy
   change and a key supplied through the environment.
10. **No empty scaffolding.** A Phase 2 table or command exists only when its
    behaviour is implemented and tested.

## 4. Scope

### Phase 0 — contracts and design (delivered as documentation)

Product freeze (this file), architecture, threat model, data model, event and
contract schemas, evaluation plan, CLI/UI flow, gap analysis, ADRs.

### Phase 1 — repository intelligence (implemented)

- Project and repository registration for local Git repositories.
- Pinned committed snapshots and explicit worktree snapshots with patch
  artifacts and untracked manifests.
- Discovery with gitignore respect, excluded directories/globs, size and count
  budgets, secret filename/content detection, symlink and submodule handling.
- Python AST indexing (modules, classes, methods, functions, constants,
  imports, calls, inheritance, decorators, docstrings) with exact spans.
- Lexical indexing for every other text file, with identifier-aware tokens.
- Edge resolution across files with `HIGH`/`MEDIUM`/`LOW` confidence.
- Repository map with languages, entry points, tests, coverage and skips.
- Search: lexical (FTS5/BM25), exact, regex, symbol search and symbol detail.
- Grounded Q&A: retrieval, budgeted context pack, optional model with
  structured output, deterministic citation validation, durable records.
- SQLite persistence, content-addressed artifacts, checksummed migrations,
  append-only workflow events, model invocation accounting.
- CLI: `init`, `doctor`, `project`, `repo`, `snapshot`, `index`, `inspect`,
  `search`, `symbol`, `ask`, `qa`, `issues`, `events`, `config`, `model`,
  `schemas`, `db`.

### Phase 2 — research intelligence and claim mapping (implemented)

- Document registration and immutable versioning for Markdown, plain text and
  PDFs, from a file, an http(s) URL or a DOI, with guarded retrieval.
- Page/section/chunk-aware passages with exact character spans, quote hashes and
  PDF missing-page reporting.
- Claim extraction (deterministic by default, optional model with no silent
  fallback) into four separate categories, with manual authoring, correction,
  review and append-only revisions.
- Claim DAG validation (cycles, missing dependencies, cross-document edges).
- Claim-to-code mapping against a pinned snapshot: `PRESENT`, `PARTIAL`,
  `ABSENT`, `UNCERTAIN`, `CONFLICTING` and manual `NOT_APPLICABLE`, each with
  verified evidence spans; `PRESENT` requires declared signals in executable
  code inside a definition-level span.
- Optional model adjudication restricted to supplied spans, with deterministic
  re-verification and refusal of invalid verdicts.
- Reproduction Readiness / Risk Assessment with explicit `UNKNOWN` factors and
  `INSUFFICIENT_INFORMATION` instead of a confident score.
- Versioned research memo with an immutable, hash-verified source
  interpretation and appended deviations.
- CLI: `research add|list|show|extract|dag|readiness|memo`, `research claim
  add|list|show|edit|review`, and `map [run|set|show]`.

### Phase 3 — controlled engineering agent (implemented)

- Immutable, hash-addressed task contracts: scope, budgets, approvals,
  acceptance criteria and declared verification checks.
- Reviewable plans (model-generated or supplied as a reviewed JSON file) and
  durable, scoped, single-use approvals for writes and command execution.
- Detached Git worktrees at the pinned commit; the user's checkout is never
  modified. Captured patches include untracked files.
- Bounded agent loop with a tool gateway, per-run budgets, pause/cancel, a
  durable worker queue with leases, and crash-safe settlement.
- Structural diff review (scope, secrets, test changes, dependencies) and
  content-addressed tool/command output.

### Phase 4 — verification and evidence (implemented)

- Append-only verification attempts; deterministic check registry (scope,
  approvals, budgets, snapshot, diff, patch reproducibility, test integrity,
  report integrity).
- Baseline comparison: declared commands run on a detached worktree at the base
  revision with the snapshot patch applied, so regressions and pre-existing
  failures are distinguished by execution.
- Test-integrity warnings from an AST comparison of test files and config.
- Eight parameterised ML invariant templates executed through the same command
  policy, including an explicit SKIP marker for unavailable dependencies.
- Read-only, grounded model reviewer whose findings can never overrule a
  deterministic result.
- Fixed decision rules, per-criterion evidence assessment, an evidence graph,
  hashed JSON/Markdown reports and a self-verifying export bundle.

### Phase 5 — Experiment Lab and memory (implemented)

- Immutable, content-hashed experiment definitions approved before execution;
  any change creates a new definition.
- One replicate per run, with a manifest hashed before the command starts and
  observed input hashes recorded.
- Fairness classification of the protocol surface; undeclared drift yields
  `NOT_COMPARABLE`, unknown fields become limitations.
- Fixed statistics: paired t when seeds match, Welch otherwise, Holm for
  secondaries, Hedges' g, constant-difference handling.
- Conservative verdicts (`INCONCLUSIVE`, `NO_DETECTABLE_DIFFERENCE`,
  `CANDIDATE_ADVANTAGE`, `BASELINE_ADVANTAGE`, `CANDIDATE_NON_INFERIOR`).
- Reproduction bundles with per-arm commits, file hashes and verification.
- Verification Gate F (`task_contracts.require_experiment`,
  `experiment.evidence`) makes an approved comparison mandatory evidence.
- Scoped project memory (`FACT`, `DECISION`, `NEGATIVE_RESULT`) where only the
  user confirms, provenance is validated, secrets are refused and commit-scoped
  memories become `STALE`.

### Explicitly out of scope

Multi-repository reasoning, embeddings/vector search, hosted service,
authentication, UI beyond the CLI, and labelled-corpus evaluation scoring.

## 5. Success criteria (Phase 1)

| Criterion | Target | Verified by |
| --- | --- | --- |
| Repository onboarding on a real repo | < 60 s for a 10k-file repo, indexing included | `maya index` timing, `INDEX_COMPLETED` duration |
| Coverage honesty | every non-Python or unparseable file labelled | `maya inspect`, `maya issues` |
| Snapshot integrity | indexing refuses a moved working tree | `test_stale_worktree_snapshot_is_refused_at_index_time` |
| Checkout safety | no command writes to the repository | `test_snapshot_creation_never_modifies_the_user_checkout` |
| Citation integrity | zero accepted citations that fail verification | citation tests, `CITATIONS_REJECTED` path |
| Durability | records survive process restart | storage durability tests, CLI workflow test |
| Explainability | every answer names snapshot, context, model, citations | `maya qa show`, `--json` output |
| Default privacy | no network call under `LOCAL_ONLY` | policy test, gateway guard |

The 10k-file timing target is a design budget; the Phase 1 report records the
measured numbers on the fixture repository rather than extrapolating.

### Phase 2 success criteria

| Criterion | Target | Verified by |
| --- | --- | --- |
| Quote integrity | every explicit claim quote matches a stored passage | `verify_sources`, `test_claims.py` |
| No name-only `PRESENT` | `PRESENT` requires every declared signal in executable, definition-level code | rule tests, `test_mapping.py` |
| Model cannot overrule evidence | invalid `PRESENT`/`ABSENT`/`CONFLICTING` is refused and recorded | adjudication refusal tests |
| Absence is bounded | `ABSENT` is withheld when coverage/discovery is limited | rule tests, `UNCERTAIN` paths |
| Memo history | appended versions reproduce the earlier interpretation byte for byte | hash and tamper tests |
| Honest readiness | unknown factors stay unknown and suppress an overall value below 75% known weight | `test_readiness_memo.py` |

### Phase 4 success criteria

| Criterion | Target | Verified by |
| --- | --- | --- |
| Every success/failure claim links to evidence | each check row names a command/artifact, each criterion names check keys | report payload, `evidence_links` |
| No self-grading | reviewer can only add findings; deterministic checks decide | reviewer grounding tests |
| Regressions vs pre-existing failures | both measured on the base revision | baseline execution tests |
| Test weakening is visible | detectable patterns produce warnings with path/line | `test_verification_evidence.py` |
| Reports are reproducible | append-only attempts; bundle hashes verify | bundle tamper tests |
| No silent acceptance | missing/blocked checks yield `INCONCLUSIVE`/`BLOCKED` | decision matrix tests |

### Phase 5 success criteria

| Criterion | Target | Verified by |
| --- | --- | --- |
| No post-hoc protocol | definition hash covers protocol, metrics, seeds and command; edits create a new definition | definition immutability tests |
| Honest comparison | undeclared protocol drift yields `NOT_COMPARABLE`; unknowns are limitations | comparison tests, real-repo bundle |
| No fabricated significance | constant differences have no p-value; underpowered samples are `INCONCLUSIVE` | stats/comparison tests |
| Reproducible claim | bundle hashes verify and records both arm commits | cross-commit bundle test |
| Correctness not traded for speed | a declared secondary can contradict the primary without upgrading it | comparison tests |
| No self-confirmed memory | model/system memories stay `UNCONFIRMED` until the user confirms | `test_memory.py` |
| Gate F cannot be bypassed | missing/inconclusive experiment evidence blocks or limits acceptance | verification Gate F tests |

## 6. Constraints and decisions already made

- Python 3.11+, one runtime dependency (`pydantic`), stdlib for Git/SQLite/HTTP.
- Local-first SQLite with WAL and FTS5; no server process.
- Domain models are Pydantic v2 models with strict extra-field rejection.
- Provider integration is behind a narrow adapter interface
  (`ModelProvider.generate`), currently one OpenAI-compatible implementation.
- No telemetry. Nothing leaves the machine unless the user opts into a remote
  model call.

## 7. Open questions tracked for later phases

- Whether task contracts should be stored as YAML in the repository or only in
  the workbench database (Phase 3 decision; the database is authoritative
  either way).
- Which patch-verification checks are mandatory before a task can be marked
  complete. Phase 4 resolves the mechanism (the contract declares checks; no
  declared functional check ⇒ `INCONCLUSIVE`) but not the policy for which
  checks a given task type must require.
- Whether multi-repository projects need a cross-repository snapshot group
  (not needed for Phase 1; would change snapshot identity semantics).

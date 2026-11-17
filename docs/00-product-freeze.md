# Product freeze (Phase 0)

Status: frozen for Phase 0/1/2. Changes require a documented decision record in
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

Later phases add J7–J9 (task contracts, isolated worktrees, verified patches,
evaluation runs); those are specified but not implemented.

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

### Explicitly out of scope for Phases 1–2 (deferred to Phases 3–4)

Task contracts, approval gates, worktrees/branches for agents, patch
verification, test execution, multi-repository reasoning, embeddings/vector
search, hosted service, authentication, UI beyond the CLI.

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
  complete (Phase 4 evaluation will inform this).
- Whether multi-repository projects need a cross-repository snapshot group
  (not needed for Phase 1; would change snapshot identity semantics).

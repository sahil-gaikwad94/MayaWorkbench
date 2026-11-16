# Implementation gap analysis

This document compares the master specification's Phase 0 and Phase 1
deliverables with what is actually implemented, and states what is deliberately
absent. "Implemented" means: exercised by the test suite or the recorded CLI
run, with failure behaviour included.

## 1. Phase 0 deliverables

| Spec deliverable | Status | Where |
| --- | --- | --- |
| Product definition and freeze | Done | `docs/00-product-freeze.md` |
| Architecture and module boundaries | Done | `docs/architecture.md` |
| Threat model | Done | `docs/threat-model.md` |
| Data model | Done | `docs/data-model.md`, `migrations/0001_initial.sql` |
| Event and contract schemas | Done | `docs/event-and-contract-schemas.md`, `maya schemas export` |
| Evidence model (citation verification) | Done and implemented | `src/mayaworkbench/qa/citations.py` |
| Evaluation plan | Done | `docs/evaluation-plan.md` |
| Low-fidelity CLI/UI flow | Done | `docs/ui-flow.md` |
| Gap analysis | This file | |
| Architecture decision records | Done | `docs/adr/0001`–`0006` |

## 2. Phase 1 deliverables

| Capability | Status | Evidence |
| --- | --- | --- |
| Project registration | Implemented | `maya init`, `project create/list/show`; `PROJECT_CREATED` |
| Repository registration | Implemented | `repo add/list`; duplicate attach is idempotent |
| Committed snapshot pinning | Implemented | Git-object reads, HEAD/tree recorded, dirty flag, warnings |
| Worktree snapshot with patch | Implemented | patch artifact, untracked manifest, `patch_hash`, identity reuse |
| Snapshot reuse/dedupe | Implemented | equivalent snapshots reuse the existing row |
| Stale-worktree refusal | Implemented | HEAD/status/patch hash re-verified before indexing |
| Discovery and exclusions | Implemented | gitignore via Git, dirs/globs, binary, size/count budgets |
| Secret exclusion | Implemented (best-effort) | filename patterns + signature scan; `SKIPPED_SECRET` |
| Python AST index | Implemented | modules, classes, methods, functions, constants, decorators, docstrings |
| Imports/calls/inheritance edges | Implemented | `import_aliases`, confidence levels |
| Non-Python files | Lexical only, labelled | `LEXICAL_ONLY` coverage and file status |
| Source spans | Implemented | 1-based inclusive; verified against `file_lines` in tests |
| Repository map | Implemented | `maya inspect`, optional `INDEX_MAP` artifact |
| Lexical search | Implemented | FTS5 + identifier expansion, BM25, AND→OR broadening |
| Exact and regex search | Implemented | raw `file_lines`; invalid regex is a usage error |
| Symbol search and detail | Implemented | ranking, spans, source, relationships |
| Grounded Q&A with evidence | Implemented | retrieval → context pack → model/optional → citations |
| Deterministic citation validation | Implemented | path, span containment, normalised quote |
| Withholding on invalid citations | Implemented | `CITATIONS_REJECTED` + rejected artifact |
| No-model evidence digest | Implemented | `NO_MODEL`, citations marked unverified |
| SQLite persistence | Implemented | WAL, foreign keys, checksummed migrations |
| Content-addressed artifacts | Implemented | SHA-256 verified on read |
| Workflow events | Implemented | append-only log, filters, CLI |
| Model invocation accounting | Implemented | tokens/latency/cost basis; no prompts stored |
| CLI commands (`init|inspect|index|search|ask`) | Implemented | plus `doctor`, `symbol`, `qa`, `issues`, `events`, `config`, `model`, `schemas`, `db` |
| JSON output | Implemented | global `--json`, one object per command |

## 3. Deliberate deferrals

These are specified for later phases. They are not stubbed, because empty tables
or commands would imply capability that does not exist.

| Deferred | Phase | Why now is the wrong time | Prerequisite already in place |
| --- | --- | --- | --- |
| Task contracts and acceptance criteria | 3 | Needs snapshot + QA contracts to reference | snapshot IDs, events, artifacts |
| Approval gates and authority rules | 3 | Needs a task object to approve | policy layer, event log |
| Agent worktrees/branches | 3 | Needs task identity and isolation policy | Git adapter, snapshot capture |
| Patch verification (diff, tests) | 4 | Running repository code is arbitrary execution; needs sandboxing | artifact store, snapshot identity |
| Test runner integration | 4 | Same execution boundary as above | — |
| Multi-repository projects | 3+ | Changes snapshot identity/grouping semantics | repository model |
| Embeddings/vector search | 2+ | Exact identifiers must be correct first; false confidence is worse than none | lexical search, symbol index |
| Hosted API/UI, auth, tenancy | 3+ | Product is local-first; no security model for multi-user yet | clean service layer, JSON contracts |

## 4. Deviations from the spec text

| Spec expectation | Implemented behaviour | Reason |
| --- | --- | --- |
| "UI flow" | CLI plus a UI mapping document | Phase 1 has no GUI; the CLI is the product surface and JSON is the integration contract |
| Snapshot of a dirty tree | Explicit `--include-working-tree`, otherwise excluded with a warning | Spec forbids silently mixing uncommitted work; both choices are explicit |
| Symbol coverage | Python AST only | A parser that lies about other languages would violate the coverage rule; other files are lexically indexed and labelled |
| Edge resolution | Three confidence levels, unresolved edges kept | Name-only "resolution" is explicitly forbidden by the spec |
| Cost reporting | Only when the user provides prices | The tool must not invent cost; `cost_basis` records the basis |

## 5. Known limitations (and what they mean for a user)

1. **Python-only semantic index.** Non-Python code is searchable but has no
   symbols or edges. `maya inspect` labels this; `maya symbol show` will find
   nothing for those files.
2. **Static resolution limits.** Dynamic imports, `getattr`, DI containers and
   string-based dispatch are unresolved (`LOW`). A "no callers found" result is
   not proof of dead code.
3. **Heuristic token estimates.** Budgets use `chars / 3.6 + 8` per span unless
   the provider supplies a tokenizer; the estimator name is recorded in the
   context pack.
4. **Index bounds.** 1.5 MB per file, 96 MB total, 20k files, 200k lines per
   file, 5,000 symbols per file, 8,000 characters per line. Overruns are
   reported as issues, not silently ignored.
5. **Secret scanning is signature-based.** A novel token format can slip
   through; the tool never claims otherwise.
6. **Git is trusted to parse its own inputs.** A malicious repository plus a
   Git vulnerability is outside the sandbox; only read commands are used and
   known helper vectors are disabled.
7. **Single-user local state.** No authentication; anyone with filesystem access
   to `$MAYA_HOME` can read the index and QA history.
8. **No automated evaluation harness yet.** `docs/evaluation-plan.md` defines
   the metrics; the labelled corpora and CI scoring are Phase 2 work.
9. **English-centric tokenisation.** Identifier splitting is ASCII-oriented;
   natural-language tokenisation is `unicode61` but stopwords and compound
   generation are English.
10. **No answer streaming or cancellation.** Model calls complete or time out;
    there is no partial-answer protocol.

## 6. Risks to the next phase

| Risk | Mitigation |
| --- | --- |
| Task contracts designed without real patch-verification data | Run the Phase 4 evaluation corpus manually first; keep contract v0 internal |
| Worktree isolation colliding with user's own uncommitted work | Require a clean base or a dedicated worktree root; test both |
| Test execution becoming an escape hatch | Sandbox boundary, explicit approval, no network by default |
| Retrieval quality plateauing | Measure Hit@5 on the labelled corpus before adding embeddings |
| Scope creep into hosted multi-user | Keep JSON contracts and the service layer; add auth only with a tenancy design |

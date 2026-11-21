# Implementation gap analysis

This document compares the master specification's Phase 0–6 deliverables with
what is actually implemented, and states what is deliberately absent.
"Implemented" means: exercised by the test suite or the recorded CLI run, with
failure behaviour included.

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
| Labelled evaluation corpus and CI scoring | 4+ | Needs real run bundles and human judgements to score against | verification reports, evidence bundles, decision rules |
| Multi-repository projects | 5+ | Changes snapshot identity/grouping semantics | repository model |
| Embeddings/vector search | 5+ | Exact identifiers must be correct first; false confidence is worse than none | lexical search, symbol index |
| Hosted API/UI, auth, tenancy | 5+ | Product is local-first; no security model for multi-user yet | clean service layer, JSON contracts |
| Multi-host execution | 5+ | SQLite and worktrees are same-host by design | local filesystem guard, worker leases |

Task contracts, approvals, worktrees, patch capture, test execution, ML
invariants, independent review, reports and bundles are implemented in Phases
3–4; see [phase-4-report.md](phase-4-report.md).

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
   the metrics; the labelled corpora and CI scoring are not implemented. Per-run
   evidence (checks, criteria, decisions, bundles) is now recorded, so a corpus
   can be scored without adding new product surface.
9. **English-centric tokenisation.** Identifier splitting is ASCII-oriented;
   natural-language tokenisation is `unicode61` but stopwords and compound
   generation are English.
10. **No answer streaming or cancellation.** Model calls complete or time out;
    there is no partial-answer protocol.
11. **Experiments are single-host and wall-clock based.** Concurrency on the
    same machine is not controlled; the statistics model run-to-run variation,
    not host interference. Unknown environment fields are reported as gaps.
12. **No power analysis.** `budget_runs` caps a definition; it does not tell the
    user how many seeds are needed to detect a given effect.
13. **Memory has no semantic reconciliation.** It stores scoped statements with
    provenance but does not deduplicate near-identical entries or resolve
    contradictions between confirmed memories.

## 6. Phase 5 deliverables

| Capability | Status | Evidence |
| --- | --- | --- |
| Immutable experiment definitions | Implemented | content hash, DRAFT→APPROVED→REVOKED, no update path |
| Pre-execution manifests | Implemented | `experiment_manifests`, hash before run, observed input hashes |
| One replicate per run | Implemented | `seed` unique per attempt; retry is a new attempt |
| Metrics from stdout/JSON/CSV | Implemented | typed contract; undeclared values recorded but excluded |
| Fairness classification | Implemented | protocol field classification; undeclared drift is `NOT_COMPARABLE` |
| Fixed statistics | Implemented | paired t/Welch, Holm, Hedges' g, constant-difference handling |
| Honest verdicts | Implemented | `INCONCLUSIVE`/`NDD`/advantage/non-inferiority |
| Reproduction bundles | Implemented | hashes, per-arm commits, verify-bundle |
| Verification Gate F | Implemented | `require_experiment`, `experiment.evidence` |
| Scoped memory | Implemented | user-only confirmation, provenance, staleness, secret refusal |
| Imported/external results | Implemented with limitation | `IMPORTED` runs are labelled and not environment-verified |
| CLI surfaces | Implemented | create/plan/approve/run/compare/show/bundle/verify-bundle; memory commands |
| Real-repository run | Done | see [phase-5-report](phase-5-report.md) |

## 7. Phase 6 deliverables

Phase 6 in the specification is "design partners and public alpha". The parts
that require external users (onboarding, design-partner workflows, repeat-use
measurement, same-model comparison on held-out tasks) remain open. The
engineering prerequisites for a public alpha are implemented:

| Capability | Status | Evidence |
| --- | --- | --- |
| Deterministic finish gate | Implemented | declared checks run before a finish request is honoured; failure is never a pass |
| Bounded repair loop | Implemented | typed `REPAIR_REQUIRED` feedback, `max_repair_attempts` rounds, same budgets |
| Checker-vs-code failure attribution | Implemented | pytest exit 1 vs 3/4/5/6, blocked/cancelled/no-verdict handled separately |
| Retrieval-grounded orientation | Implemented | bounded base-revision spans, scoped symbols, test files, index issues |
| Durable working memory | Implemented | ledger derived from audited tool calls; survives pause/resume and prompt truncation |
| Read-only progress guard | Implemented | repeated identical observation is refused, then the run blocks |
| Declared-check and symbol tools | Implemented | `run_check` (contract keys only, approval-gated), `find_symbol` (excluded paths filtered) |
| Encoding robustness | Implemented | UTF-8/UTF-16 BOM decoding, labelled lossy reads, UTF-32 refusal |
| Write fidelity | Implemented | CRLF/CR and BOM preservation on rewrite |
| Path hardening | Implemented | control characters, bidi overrides, 255-byte components |
| CLI robustness | Implemented | broken pipe is success; non-encodable output replaced |
| Real-repository validation | Done | [phase-6-report](phase-6-report.md) §5.2 |

Deliberately still absent after Phase 6: runner-specific exit-code semantics for
non-pytest checkers, cross-run repair memory, automatic failure localisation,
language coverage beyond Python, hosted isolation and multi-user auth, and the
labelled evaluation corpus. Each is a limitation statement in the README rather
than a hidden stub.

## 8. Risks to the next phase

| Risk | Mitigation |
| --- | --- |
| Claiming verification effectiveness without corpus data | Collect exported bundles and human decisions; do not generalise from one repository |
| Repair loops increasing spend without improving outcomes | `max_repair_attempts` is explicit; measure repair success per round before raising defaults |
| Treating an in-loop pass as evidence | In-loop results are labelled advisory and never written as verification checks |
| Worktree isolation colliding with user's own uncommitted work | Require a clean base or a dedicated worktree root; test both |
| Test execution becoming an escape hatch | Sandbox boundary, explicit approval, no network by default |
| Retrieval quality plateauing | Measure Hit@5 on the labelled corpus before adding embeddings |
| Scope creep into hosted multi-user | Keep JSON contracts and the service layer; add auth only with a tenancy design |

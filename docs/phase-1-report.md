# Phase 1 report — repository intelligence

Status: complete for the scope in
[docs/00-product-freeze.md](00-product-freeze.md). Phase 2–4 are documented as
deferred, not stubbed.

## 1. What was built

A Python package (`mayaworkbench`, ~11k lines of package code plus ~1.3k lines of tests) implementing:

- project/repository registration and local Git snapshots (committed and
  explicit worktree with patch evidence);
- discovery with gitignore, exclusion, budget and secret rules, every skip
  recorded;
- Python AST indexing (symbols, imports, calls, inheritance, decorators,
  docstrings) with exact spans, plus lexical indexing for all text files;
- edge resolution with `HIGH`/`MEDIUM`/`LOW` confidence and unresolved edges
  retained;
- repository map with coverage, languages, entry points, tests and skips;
- FTS5 lexical search with identifier expansion, exact and regex search, symbol
  search and symbol detail;
- grounded Q&A with budgeted context packs, optional model, deterministic
  citation validation, and durable QA/artifact/event records;
- SQLite persistence with checksummed migrations, content-addressed artifacts,
  append-only events and model invocation accounting;
- a complete CLI (`init` … `db`) with human and `--json` output.

Nothing in Phase 1 modifies a user's repository. Remote model calls are refused
unless the user changes the data policy and supplies a key via the environment.

## 2. Verification environment

| Item | Value |
| --- | --- |
| OS | Linux (container) |
| Python | 3.12.3 |
| Git | 2.43.0 |
| SQLite | 3.45.1 (FTS5 available) |
| Install | `python -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"` |
| Runtime dependency | `pydantic>=2.7` only |

## 3. Test suite

```
$ .venv/bin/python -m pytest -q
.....................................................................   [100%]
84 passed in 6.2s

$ .venv/bin/python -m compileall -q src tests && echo ok
ok
```

Coverage by area (test files):

| Area | Tests | Focus |
| --- | --- | --- |
| `test_storage.py` | 12 | migrations, checksum tamper, transaction durability/rollback, artifact integrity, FTS5 |
| `test_snapshots.py` | 13 | committed vs worktree content, patch/manifest evidence, checkout safety, reuse, stale refusal, hostile Git helpers, submodules, empty repo, clean-worktree collapse |
| `test_indexing.py` | 15 | symbols/spans, docstrings/decorators, edge resolution and confidence, parse failures, secret/binary/gitignore exclusions, manifest hashes, test/doc classification, idempotent reindex, failed-reindex preservation, PEP 695 type aliases |
| `test_search.py` | 16 | lexical identifier variants, broadened queries, exact/case, regex + invalid regex, FTS operator/Unicode safety, path filters, symbol ranking/detail, precise line numbers |
| `test_retrieval.py` | 10 | identifier/compound extraction, symbol+context retrieval, span provenance, budget enforcement, exclusions, no-evidence and weak-evidence honesty |
| `test_qa.py` | 11 | verified citations, rejection/withholding, schema failure, no-model digest, insufficient evidence, `LOCAL_ONLY` refusal, record listing, range validation |
| `test_cli.py` | 7 | full workflow, doctor, error exit codes, config round-trip and secret guard, schema export, version, explicit-snapshot disambiguation |

The suite uses real temporary Git repositories and a real SQLite file, and
injects the model provider at the factory boundary (never over the network).

## 4. End-to-end run (fresh workbench)

Commands were run against a two-commit fixture repository containing a dirty
module, an untracked module, a `.env`-style secret, a gitignored log and a
binary weight file.

```
$ maya --home /tmp/final-home init --project final
schema version 1 · project final · LOCAL_ONLY

$ maya repo add /tmp/final-repo
Attached: final-repo · branch master

$ maya snapshot create
identity f8cb7b0833fb · commit f8cb7b0833fb69b10f4b9ba07ab46268d32bbfaa
dirty yes · patch included no · status CREATED
warning: 2 uncommitted change(s) are NOT part of this snapshot

$ maya index
files indexed 10 · skipped 2 · lines 164 · bytes 4302
symbols 23 · edges 79 (18 resolved, 61 unresolved) · issues 2
duration 33 ms · manifest art_2277ced8c179a7b7886191bb29db59bb

$ maya inspect
python 6 files/141 lines · markdown 2/13 · ignore 1/3 · toml 1/7
coverage: FULL 6 · LEXICAL_ONLY 4 (10 files)
entry points: tinygpt.cli:main, python -m tinygpt
```

Answers:

```
$ maya ask "Where is the causal mask applied?" --no-model
status NO_MODEL
budget 1006/8000 estimated tokens
symbols: CausalAttention, CausalAttention.__init__, CausalAttention.forward,
         build_attention, test_causal_masking_blocks_future_positions, …
matching lines: src/tinygpt/attention.py:33, :34, :43, tests/...
citations: 6, all marked "not model-verified (retrieval evidence)"
```

Worktree snapshot and exact search for an uncommitted edit:

```
$ maya snapshot create --include-working-tree
identity f8cb7b0833fb+patch:36ac8dd02b84 · changed paths 2 · patch included yes

$ maya index
files indexed 11 · lines 166 · bytes 4326
symbols 17 · edges 53 (13 resolved, 40 unresolved) · issues 4
duration 29 ms
coverage: FULL 5 · LEXICAL_ONLY 4 · PARTIAL 2 (11 files)

$ maya search "dirty change" --mode exact
src/tinygpt/attention.py:44  exact  dirty change

$ maya issues
PARSE_ERROR 2 (attention.py:44, experimental.py:1 — deliberate fixture breakage)
EXCLUDED_GLOB 1 (weights.bin)
SKIPPED_SECRET 1 (.env)
```

Symbol counts drop in the worktree snapshot because the fixture deliberately
appends invalid Python; those files become `LEXICAL_ONLY` with `PARSE_ERROR`
issues, which is the intended, visible behaviour.

```
$ maya schemas export /tmp/final-schemas
Exported 17 schemas · bundle version 1.0 (18 files including the manifest)

$ maya db info
schema version 1 · journal wal · 516 KiB · row counts for 12 tables
```

## 4.1 Real mid-sized repository run (pallets/flask)

After the fixture run, the full flow was executed against a real repository:
`pallets/flask` at commit `49943b5a9441` (83 Python files, 226 files total,
34,635 lines, 1,157,567 bytes). Results:

```
$ maya index
files indexed      226
files skipped      10
lines indexed      34635
bytes indexed      1157567
symbols            1714
edges              5395 (1450 resolved, 3945 unresolved)
issues             10
duration           1.4 s
```

- Languages: Python 83 files / 18,392 lines; reStructuredText 79 / 14,648;
  HTML 20 / 362; text 9 / 97.
- Issues: 1 `SKIPPED_SECRET` (`tests/test_apps/.env`), 9 `EXCLUDED_GLOB`
  (png/svg/lock files under `docs/_static`).
- Resolution: 790 `HIGH`, 660 `MEDIUM`, 3,945 `LOW` (unresolved). The unresolved
  set is dominated by attribute access and calls to builtins/stdlib/external
  packages, which is expected for static analysis; no false `HIGH` claims were
  found in spot checks. The `src/`-layout module naming fix (below) raised
  resolved edges from 911 to 1,450.
- `maya inspect` reports entry points (`flask:app`, console scripts) and test
  paths correctly.
- `maya ask "Where is Flask.wsgi_app defined and what does it do?" --no-model`
  returns the `Flask.wsgi_app` span plus related request-dispatch symbols; the
  first run also pulled in unrelated documentation because the question's filler
  words (`where`, `is`, `defined`, `what`, `does`) were being searched as
  terms. Stopword filtering in the retrieval query fixed this; the question now
  searches `Flask.wsgi_app` only.
- `maya ask "zzz plutonium banana quantum" --no-model` now returns
  `INSUFFICIENT_EVIDENCE` instead of citing unrelated test lines, because a
  single accidental keyword match is no longer treated as evidence for a
  multi-concept question.
- Hostile probes: `NEAR(`, `a OR b NOT c`, and `naïve` no longer crash FTS5 or
  produce noise; `naïve` folds to `naive` via `unicode61` and is searched as a
  single quoted term. Invalid regular expressions in `--mode regex` produce a
  usage error; in auto mode they end the fallback chain.
- Worktree snapshots on Flask were not exercised (the clone is clean); the
  fixture and adversarial runs cover that path.

## 5. Defects found and fixed during verification

These are real defects found by running the product, not hypothetical risks:

1. **SQLite implicit transactions swallowed explicit commits.** With the stdlib
   default isolation level, an earlier `INSERT` could open a transaction that a
   later explicit `BEGIN` joined, so `COMMIT` never happened and worktree
   snapshots vanished across processes. Fixed with `isolation_level=None` plus
   explicit transactions; regression test covers autocommit + transaction
   durability across connections.
2. **Discovery skips were never persisted.** `.env` and `weights.bin` were
   skipped but `maya issues` was empty, hiding exactly what the product promises
   to surface. Fixed by inserting discovery issues inside the index transaction;
   tests assert `SKIPPED_SECRET` and `EXCLUDED_GLOB` rows exist.
3. **A failed reindex left the snapshot stuck in `INDEXING`.** The transaction
   rolled back and the previous index survived, but the status never returned to
   `INDEXED`, so the snapshot looked permanently in progress. Fixed with
   `restore_indexed` (status restored, error retained); test asserts both.
4. **The config secret guard blocked legitimate keys.** `retrieval.default_token_budget`
   was rejected because it contains "token". Fixed by scoping credential checks
   to the `model` section; regression test sets the budget and still rejects
   `model.api_key`.
5. **Invocation rows recorded the adapter kind, not the provider that answered.**
   `provider` stayed `openai_compatible` even when the adapter reported a
   different provider. Fixed so completion overwrites provider/model with the
   response's values; cost basis behaviour unchanged.
6. **`NO_MODEL` was logged as `QA_REJECTED`.** A valid evidence-only outcome
   looked like a refusal in the event log. Added `QA_EVIDENCE_ONLY` and mapped
   statuses explicitly.
7. **Invalid regex surfaced SQLite's wrapper error.** `regexp('(')` raised
   `user-defined function raised exception`. Patterns are now compiled before
   the query, producing a usage error with the real message; raw SQL misuse
   still yields `DatabaseError`.
8. **Git helper hardening initially broke worktree diffs.** Setting
   `-c diff.external=` to an empty value made Git try to execute the empty
   string. Replaced with `--no-ext-diff` / `--no-textconv` at the diff call and
   `-c core.fsmonitor=false` globally; a test with a hostile `.gitattributes`
   textconv and fsmonitor command asserts nothing executes.
9. **Subcommand help omissions.** `config show/defaults`, `model status` and
   `db migrate/info` lacked help strings, so `--help` listed only one action.
   Fixed.
10. **`--json` errors lacked a machine-readable code.** Added `code` to the JSON
    error payload (e.g. `USAGE_ERROR`, `NOT_FOUND`).
11. **FTS5 crashed on operator-like queries.** `NEAR(`, `a OR b NOT c` and
    similar inputs reached SQLite as raw FTS5 syntax and raised
    `fts5: syntax error` / `user-defined function raised exception`. Terms are
    now quoted with `"..."` (internal quotes doubled) in every match
    expression, and identifier tokenisation uses Unicode word characters instead
    of ASCII-only `\w`, so `naïve` no longer splits into `na`/`ve`.
12. **Auto-mode regex fallback crashed on non-regex input.** A query such as
    `foo(` failed with `re.error` before the lexical result was returned.
    Invalid patterns now end the fallback chain in auto mode and raise a usage
    error only when `--mode regex` is explicit.
13. **Citation rejection was “any invalid → reject”, but the code counted
    invalid citations loosely.** Tightened so a single unverifiable citation
    withholds the whole answer; range ordering (`end_line >= start_line`) is now
    validated at both the model schema and domain boundary.
14. **Clean worktrees were indexed as worktree snapshots.** A clean tree
    produced a `WORKTREE` snapshot with a null patch, which later looked dirty.
    Clean trees now collapse to a `COMMITTED` snapshot with no patch, while
    genuinely dirty trees still carry the patch and untracked-file manifest.
15. **`src/`-layout module names included the source root.** Modules were named
    `src.flask.ctx`, so absolute imports (`from flask.ctx import …`) resolved
    only at `MEDIUM` confidence via suffix matching and the names leaked into
    search results. The common `src/` prefix is now stripped, raising Flask's
    resolved edges from 911 to 1,450.
16. **Non-UTF-8 files were reported as `SKIPPED_BINARY`.** They are now
    `ENCODING_ERROR`, distinct from genuine NUL-byte binary detection.
17. **Accidental single-keyword matches counted as evidence.** A nonsense
    question whose terms each matched unrelated lines (e.g. `zzz plutonium
    banana quantum`) produced a `NO_MODEL` answer. Retrieval now requires a
    majority of the question's terms to appear as whole tokens in the retrieved
    spans when no symbol was recognised, and marks the bundle
    `evidence_sufficient=false`.
18. **Explicit `--snapshot` was unusable in multi-repository projects.** Every
    command still demanded `--repo` even though the snapshot already identifies
    its repository. `resolve_snapshot_context` now resolves the repository from
    the snapshot and only validates an optional `--repo` against it.

## 6. Measured performance (fixture, this environment)

| Operation | Result |
| --- | --- |
| Index committed snapshot (10 files, 164 lines, 4.3 KB) | 33 ms |
| Index worktree snapshot (11 files, 166 lines, 4.3 KB) | 29 ms |
| Index committed snapshot (pallets/flask, 226 files, 34,635 lines, 1.16 MB) | 1.4 s |
| Full test suite (84 tests, real Git + SQLite) | 6.2 s |
| QA retrieval + context pack, no model | sub-second (covered by suite) |

These numbers are for a tiny repository and are **not** extrapolated to large
ones. The 60-second onboarding target for a 10k-file repository remains
unmeasured until the Phase 2 evaluation corpus exists; the report does not claim
it.

## 7. Failure-path evidence

The suite asserts the behaviours that distinguish this from a demo:

- indexing a worktree after the tree moved ⇒ `SnapshotError`, snapshot `FAILED`,
  previous index intact;
- a model citation outside the context ⇒ `CITATIONS_REJECTED`, answer withheld,
  rejected text stored in the artifact;
- `LOCAL_ONLY` with a provider configured ⇒ `PolicyError`, invocation `REFUSED`,
  adapter never constructed;
- an indexing crash during edge resolution ⇒ transaction rollback, prior index
  queryable, `INDEX_FAILED` event;
- artifact bytes modified on disk ⇒ `ConflictError` on read;
- tampered migration checksum ⇒ `DatabaseError` and no migration applied;
- snapshot capture and indexing leave `HEAD` and `git status` unchanged.

## 8. Limitations

See `docs/implementation-gap-analysis.md` §5 for the full list. The ones a user
will notice first:

- Python-only AST (symbols include PEP 695 `type` aliases on Python 3.12+);
  other languages are lexical and labelled.
- Static resolution only; dynamic dispatch remains unresolved (`LOW`).
- Heuristic token estimates unless a provider tokenizer is configured.
- Best-effort secret detection by filename and signature.
- Local single-user state without encryption at rest.
- No automated retrieval/QA scoring harness yet (plan only).

## 9. Reproduce

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q

export MAYA_HOME=/tmp/maya-demo
.venv/bin/maya init --project demo
.venv/bin/maya repo add /path/to/repository
.venv/bin/maya snapshot create
.venv/bin/maya index
.venv/bin/maya inspect
.venv/bin/maya ask "Where is <concept> implemented?" --no-model
.venv/bin/maya schemas export ./schemas
```

No commit or push was performed as part of this report; the working tree
contains the implementation, tests and documentation.

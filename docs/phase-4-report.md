# Phase 4 report: verification and evidence

Phase 4 turns a finished agent run into a decision that cites execution evidence.
It is implemented end to end: schema, domain models, services, CLI, tests,
evidence bundle and documentation. This report states what was built, what was
measured, and what remains weak.

Implementation decisions and their rationale are recorded in
[ADR 0009](adr/0009-verification-and-evidence.md).

## 1. What runs

`maya verify run <run>` (or `TaskService.verify_run`) creates a new append-only
attempt and performs:

| Gate | Check | What it proves |
| --- | --- | --- |
| A | `scope.integrity` | the diff is inside the contract scope, excludes secrets/excluded paths, and the original checkout is unchanged |
| A | `approvals.integrity` | a consumed `WRITE_ACCESS` and `COMMAND_EXECUTION` approval match the current contract/plan hashes |
| A | `budget.integrity` | recorded steps, tool calls, tokens, cost and runtime did not exceed the contract |
| B | `snapshot.integrity` | the contract's snapshot and base commit are still the run's base |
| B | `diff.integrity` | the captured patch is non-empty, hashed and within the declared file count |
| C | `command.<hash>` | each declared command ran, with exit code and output artifact |
| D | `invariant.<name>` | generated ML invariant scripts ran through the command policy |
| C | `patch.reproducible` | the captured patch replays onto the base revision and produces the candidate files |
| C | `test.integrity` | AST comparison of test files/config between base and candidate |
| E | `review.independent` | a read-only model review grounded in the captured diff |
| F | `report.integrity` | the report's check ids, outcomes, criteria and patch hashes are internally consistent |
| G | evidence bundle | hashes, cross-references, no missing or unlisted files |

Command and invariant checks are declared on the contract:

```bash
maya task create --goal "..." --allow-path more_itertools/recipes.py \
  --acceptance "count_occurrences counts matching items" \
  --check 'python -m pytest tests/test_recipes.py -q' --check-criteria AC1
maya task start <task> --plan-file plan.json
# ... approved worker run with a captured diff ...
maya verify plan <run>
maya verify run <run>              # creates attempt 1
maya verify show <run>             # latest report, criteria, checks
maya verify report <run> --out report.md
maya verify bundle <run> --out evidence/
maya verify check-bundle evidence/
```

Every check row records `outcome`, `summary`, `details`, `duration_ms`, an
output artifact id, and findings. Every criterion is assessed as `VERIFIED`,
`PARTIAL`, `FAILED` or `UNVERIFIED`; the report's decision is derived from those
rows and the fixed rules in ADR 0009. Reports are hashed (`json_sha256`) and
exported as immutable JSON and Markdown artifacts.

## 2. Decision rules (implemented in `verification/report.py`)

1. Cancelled/failed/blocked run → `INCONCLUSIVE`/`FAILED`/`BLOCKED`.
2. A required, non-pre-existing `FAILED` check → `REJECTED`.
3. A required `BLOCKED` check → `BLOCKED`.
4. No passing required **functional** check (command or invariant) →
   `INCONCLUSIVE`. Passing deterministic gates do not substitute for a test.
5. No declared functional check at all → `INCONCLUSIVE`.
6. A failed criterion → `REJECTED`.
7. A reviewer BLOCKER → `NEEDS_HUMAN_DECISION`.
8. Unverified/partial criteria, warnings, pre-existing failures or reviewer
   concerns → `COMPLETED_WITH_LIMITATIONS`.
9. All criteria verified, no warnings → `ACCEPTED`.

`task finish --accept` refuses anything outside `ACCEPTED` /
`COMPLETED_WITH_LIMITATIONS` unless `--override` is passed with a reason. An
override is recorded as a limitation on the run and as
`VERIFICATION_OVERRIDDEN`; the report is not modified. Report limitations are
copied onto the finished run even when the decision is `ACCEPTED`.

## 3. Test integrity

`verification/integrity.py` parses the base and candidate test files with `ast`
(supplied content, not the live tree) and reports, with path and line:

- deleted test files and deleted test functions;
- renamed test functions (`TEST_RENAME_SUSPECTED`);
- newly added skip/xfail decorators or inline skips;
- assertion-count reductions in still-existing tests;
- tautological assertions (`assert True`, `assert x == x`);
- inflated numerical tolerances;
- changed or removed fixtures;
- weakened pytest configuration (`addopts -k/--ignore`, `collect_ignore`,
  `norecursedirs`, `__test__ = False`).

These are warnings by design: an empty result means "nothing detectable", never
"the tests cannot be gamed". Any warning downgrades an otherwise accepted run to
`COMPLETED_WITH_LIMITATIONS`.

## 4. ML invariants

Eight templates ship: `forward_smoke`, `shape_preserved`, `gradients_finite`,
`no_nan_output`, `causal_mask_invariance`, `deterministic_eval`,
`parameter_update`, and `dtype_device_consistency` (the phase's declared scope;
no broader ML semantics are implied). A contract names a template and typed
parameters:

```bash
maya task create ... --invariant 'forward_smoke:module=model,callable=Model,shape=1x4x32,param.d_model=32'
```

The script is rendered from validated JSON parameters, written under
`$MAYA_HOME/verification/<run>/attempt-N/home`, and executed like any other
command. Identity/layer ops fail loudly (`FAIL`) rather than silently returning
the input. Missing `torch`, a missing module/callable, or an unavailable CUDA
device prints an explicit `SKIP` marker with exit code 2; the verifier maps that
marker to `SKIPPED`, not `FAILED`. A script that prints no marker is
`INCONCLUSIVE` even if it exits 0.

## 5. Independent reviewer

`verification/reviewer.py` sends the contract criteria, bounded diff, check
results, integrity findings and agent limitations to a model with **no tools**.
Findings naming paths outside the diff are rejected, kept only as
`MODEL_REVIEW_UNGROUNDED` informational rows, and counted in
`rejected_findings`. Findings on unchanged lines are kept but flagged. The
reviewer's assessment cannot change any deterministic check; a BLOCKER escalates
to a human decision. Without a model the check is `SKIPPED` with the provider
error and the report records "independent review did not run". The reviewer
model is compared with the implementer's model invocations for the snapshot and
`same_model_as_implementer` is recorded.

## 6. Evidence bundle

The bundle directory contains `manifest.json`, `report.json`, `report.md`,
`patch.diff`, `checks.json`, `criteria.json` and the command-output artifacts the
checks cite. `verify_bundle` recomputes every SHA-256 and size, rejects unsafe
paths, missing required files, unlisted extra files, a manifest whose root hash
does not match its list, a report that names a different patch hash, and
`check_ids` that do not match `checks.json`. Bundles are never overwritten.

## 7. Verification of the implementation

- `python -m compileall -q src tests` passes.
- Full suite: **227 tests passed** (up from 198 before Phase 4), including:
  - `tests/test_verification.py` — real task workflow through to
    `REVIEWING`, declared checks, acceptance, append-only attempts, override
    gating, pre-existing failure handling, regression detection, patch drift,
    missing command approval, no-declared-check `INCONCLUSIVE`, missing-torch
    invariant skip, reviewer declaration;
  - `tests/test_verification_evidence.py` — decision matrix, test-integrity
    detections, all invariant templates compile, reviewer grounding rejection,
    reviewer BLOCKER behaviour, bundle tamper/extra/missing detection;
  - `tests/test_scale_guards.py` — parser work happens outside the write
    transaction, spill cleanup, idempotent diff races, network-filesystem
    refusal, memo claim scoping and guard;
  - `tests/test_runner_limits.py` — CPU rlimit and a live fork-bomb process
    limit.
- Concurrency: two independent `Services` connections on one home directory
  reserve one run, one job and one diff; the losing diff insert returns the
  recorded row instead of failing.

## 8. Real-repository run

Repository: [`more-itertools`](https://github.com/more-itertools/more-itertools)
at `c0ede7f` (7 Python files including tests, **16,833 Python lines**, 1.2 MB
working tree). A fresh isolated `$MAYA_HOME` was used; the user checkout was not
modified.

| Stage | Result |
| --- | --- |
| Index | 39 files (10 `FULL`, 29 `LEXICAL_ONLY`), 1,674 symbols, 6,531 edges (225 resolved), 5 issues, 695,952 bytes in **1,150 ms** |
| Task | `count_occurrences` recipe added to `more_itertools/recipes.py`, +5/−0, no scope violations |
| Check 1 (functional) | `python -c ...` — base **FAILED** (function absent), candidate **PASSED**, 106 ms; recorded as `improved`, not a regression |
| Check 2 (existing suite) | `python -m pytest tests/test_recipes.py -q` — base **PASSED**, candidate **PASSED**, ~13.0 s base / 14.0 s candidate |
| Built-ins | 9 passed, `test.integrity` `NOT_APPLICABLE`, `review.independent` `SKIPPED` (no model key) |
| Report | `ACCEPTED`, 1/1 criteria `VERIFIED`; limitation: independent review did not run |
| Bundle | exported, 156 KB, `verify_bundle` **ok** (no errors, no warnings) |
| Finish | accepted after reading the diff and checks |

**Disclosure:** no model credential was available in this environment, so the
implementer step was a hand-applied edit in the task worktree and the run was
moved to `REVIEWING` with a recorded human transition; the reviewer was skipped.
Everything downstream — diff capture, baseline worktree, both command checks,
built-ins, report, bundle verification and finish gating — executed for real.

## 9. What this run exposed, and what was fixed

1. **Invariant `SKIP` was reported as `FAILED`.** Exit code 2 was mapped like any
   non-zero command. Fixed by parsing the script's structured verdict marker
   (`PASS`/`FAIL`/`SKIP`); a missing marker is `INCONCLUSIVE`. Regression tests
   cover the no-torch path.
2. **Skipped required functional checks could end in
   `COMPLETED_WITH_LIMITATIONS`.** The decision counted passing built-ins as
   sufficient evidence. Fixed: a passing required *functional* check is now
   mandatory, otherwise `INCONCLUSIVE`.
3. **Report limitations were dropped when the decision was `ACCEPTED`.** The
   finished run could say `COMPLETED` while its report said review never ran.
   Fixed: report limitations always propagate and force
   `COMPLETED_WITH_LIMITATIONS`.
4. **Index timings were not observable.** `prepare_ms`/`write_ms` were on the
   report and snapshot metadata but not the `INDEX_COMPLETED` event. Added.
5. **No supported no-model path from approvals to a candidate patch.** There is
   no `task prepare` / external-implementer command; without a model the only
   route is a hand edit in the task worktree plus a manual transition. This is a
   real usability gap, not fixed in this phase (see below).
6. **Indexer wrote parsing inside the rebuild transaction.** Fixed by preparing
   and spilling batches outside the transaction, then replaying inserts in one
   short transaction. Timing and regression tests are in place.
7. **`DiffStore.create` / `ReviewStore.create` were check-then-insert.**
   Fixed: unique constraints are authoritative and a losing insert returns the
   recorded row.
8. **A database on a network filesystem was accepted silently.** SQLite WAL and
   POSIX locking are unsafe there. The database now refuses known shared
   filesystems unless `allow_shared_filesystem=True` is passed deliberately, and
   `BEGIN IMMEDIATE` retries transient lock contention within a bounded window.
9. **`max_processes` counted host processes, not the uid's.** The rlimit was
   far looser than configured. It now counts this uid's processes; a live
   fork-bomb test verifies the block and cleanup.
10. **Memos loaded every project claim for a document-scoped memo.** Claim
    filtering is now SQL-scoped, and a whole-project memo above 20,000 claims
    fails explicitly instead of exhausting memory.

## 10. Remaining weaknesses

- **Checks are only as good as their declaration.** A task with no functional
  check is honestly `INCONCLUSIVE`, but the product cannot infer the right test
  command. Proposed checks are suggestions, not authorization.
- **Baseline requires local executability.** The base revision runs on the same
  host with the same interpreter; a check needing dependencies installed only in
  the candidate workspace reports `BASELINE_UNAVAILABLE` (a limitation, not a
  regression claim).
- **Test integrity is structural.** It detects common weakening patterns, not
  semantic test equivalence, and cannot see dynamically generated tests.
- **ML invariants cover shape/dtype/gradient/mask/determinism basics.** They do
  not prove model quality, numerical equivalence under arbitrary kernels, or
  training convergence.
- **The reviewer needs a model and only sees the diff and check metadata.** It
  cannot read surrounding files or run anything; its review is advisory.
- **No evaluation corpus scoring.** The harness records per-run decisions and
  evidence, but there is still no labelled corpus or CI metric for "did the
  report agree with a human reviewer".
- **Single-host SQLite.** The database now fails fast on network filesystems,
  but there is still no multi-host or concurrent-writer service.
- **No no-model external implementer path.** As found in the real run, handing a
  prepared workspace to a human or another tool is not a first-class command.

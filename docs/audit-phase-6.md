# Adversarial audit — Phase 6

An independent attempt to falsify the product's claims. The audit did not reuse
the project's own test helpers to decide what "correct" means; it drove the real
services, the real CLI, real Git repositories and real subprocesses, then checked
the resulting state directly (database rows, artifacts, files on disk, bundle
manifests, exit codes).

Result: **82 probes across four campaigns; 2 real defects found and fixed; the
remaining 80 behaviours held.** One transient database error was observed once
and could not be reproduced in six controlled trials; it is recorded honestly
below rather than reported as a defect.

## How the audit was run

| Campaign | Probes | Focus |
| --- | --- | --- |
| 1 | 31 | pinned snapshots, secrets in indexes, hostile search, tool gateway, command policy, network denial, CLI/JSON, statistics |
| 2 | 20 | agent abuse, finish gate, contract tampering, test weakening, evidence bundles, leases, onboarding, concurrency, citation forgery, memory scope |
| 3 | 23 | research ingestion/claims/mapping/memo, experiment pre-registration guards, 700-file scale, migrations, SIGKILL durability, verification edges |
| 4 | 8 | credential-bearing remotes, secret patches, zero-change acceptance, budgets, parser bombs, bundle privacy |

Harnesses live in `artifacts/adversarial/harness{1..4}.py` and are reproducible:

```bash
PYTHONPATH=src .venv/bin/python artifacts/adversarial/harness1.py
# ... harness2.py harness3.py harness4.py
```

Each prints `[PASS]/[FAIL]/[ERROR]` per claim and a summary; failures exit 1.

## Defect 1 — a token in the Git remote was stored verbatim (fixed)

**Attack.** Create a repository whose remote is
`https://x-access-token:ghp_…@github.com/org/repo.git` — the shape CI and
credential helpers leave behind — attach it, and scan the workbench home.

**Observed before the fix.** The token appeared in `repositories.remote_url`,
in the `REPOSITORY_ATTACHED` event payload, and in `maya.db` and its WAL. The
index correctly skips credential-shaped *files*, but the remote URL itself was
never scanned. A local database, a backup, or an evidence export that quotes the
repository would carry a live credential.

**Fix.** `repositories/git.py::redact_remote_url` strips userinfo (keeping
scheme, host, port, path) before anything is stored. Attach warns that
credentials were removed and records
`details["remote_url_credentials_removed"] = True`. Regression test:
`tests/test_audit_findings.py::test_remote_url_credentials_are_not_stored`.

**Residual.** Rows written before this fix are not rewritten. Anyone who
attached a tokenised remote with an older build should rotate that token.

## Defect 2 — one malformed tool argument ended the whole run (fixed)

**Attack.** A scripted model emits `write_file {"path": "../evil.py"}`, then
`finish`. The path is rejected by the argument schema.

**Observed before the fix.** `ToolGateway.invoke` raised `ToolError` before its
own result-wrapping block; `ControlledAgent.run_loop` called `invoke` outside any
handler, so the exception escaped, the worker caught it at its boundary, and the
run settled `FAILED` with
`ToolError: invalid arguments for write_file: path must be normalised…`.
The model never got the chance to correct an ordinary typo (a negative
`start_line`, an oversized `limit`, a missing required field behave the same).
No authority was bypassed — nothing was written and no call row was created —
but a recoverable mistake was treated as fatal.

**Fix.** `run_loop` catches `ToolError`, counts it with the existing
invalid-action budget, records `agent_last_error`, and continues; three
consecutive invalid arguments still end the run with a typed failure.
Regression test:
`tests/test_audit_findings.py::test_invalid_tool_arguments_are_recovered_by_the_loop`.

**Verified after the fix.** The same episode now reaches `REVIEWING`; the
invalid write leaves no tool-call row and no file outside the workspace.

## What held under attack

Selected evidence, all re-run against the fixed build:

- **Pinning** — editing the working tree after indexing does not change search
  results; escaping symlinks are skipped; `.env` is skipped and its bytes appear
  in no artifact; re-indexing is idempotent.
- **Gateway** — symlink, traversal, absolute path, out-of-scope write, patch
  header traversal and credential reads are all refused; `bash` is refused;
  commands run in the workspace with a credential-free environment; output is
  capped (1,198 bytes for a 200 kB print); a 2 s timeout returns `TIMED_OUT` in
  2.0 s; `DENY_BY_DEFAULT` made a loopback connection fail.
- **Agent** — a no-op finish is `REJECTED` at verification; a weakened test is
  caught by `test.integrity` and rejected; an in-loop `PASSED` was not reused
  (a stateful check re-executed and failed, `REJECTED`); three invalid actions
  fail cleanly; repeating one read blocks after two calls.
- **Evidence** — fresh bundle verifies; editing `report.json`, deleting a file,
  adding a file, and rewriting a manifest hash are each detected; a task bundle
  contains no local absolute path; re-verification stores attempts `[1, 2]`.
- **Research** — binary/empty/oversized documents are refused; every extracted
  claim quote exists in its passage and matches its recorded hash; model
  extraction without a provider fails typed; a memo is built with an artifact.
- **Experiments** — two primaries, duplicate seeds, self-comparison and
  execution after revocation are refused; a comparison with no runs is
  `INCONCLUSIVE`; approve/revoke does not rewrite declared parameters; zero
  variance gives `p=None`.
- **QA** — two forged citations (invented quote, nonexistent path) were both
  marked invalid with reasons, the model answer was withheld, and the record
  status was `CITATIONS_REJECTED`.
- **Scale/storage** — 703 files indexed in ~2.4 s; 900-deep parentheses produce
  `PARSE_ERROR`, a binary `.py` produces `SKIPPED_BINARY`, the healthy file
  survives; a future migration is refused; SIGKILL during ten concurrent writers
  left `integrity_check = ok` in repeated trials.

## Investigated and dismissed (not defects)

- **Schema exports are byte-identical?** No: `schemas.json` carries
  `generated_at`, so directory hashes differ per run. Every other schema file is
  byte-identical and the rest of `schemas.json` matches. Consumers that hash an
  export must ignore that timestamp.
- **AWS key added to a diff was not flagged by the reviewer?** The write never
  reached the diff: `write_file` and `apply_patch` both refused
  credential-shaped content (`BLOCKED … credential`). The empty diff was the
  correct consequence.
- **`--json` errors are not on stdout.** They are a single JSON object on
  stderr with `error`, `code` and `hint`, and stdout stays clean, which matches
  the documented contract.
- **Search returned irrelevant hits for a token that does not exist.** In
  `auto` mode the query is deliberately broadened when the strict form is thin;
  `mode="exact"` returns nothing, and no hit contained the uncommitted marker.
- **"database disk image is malformed" after a SIGKILL.** Observed once inside
  campaign 3, immediately after a probe that copied a live database file and
  then a probe that killed writers sharing the main home. It did not reproduce in
  three isolated SIGKILL trials or three large-database (3.4 MB + 5.5 MB WAL)
  trials; the file reopened with `integrity_check = ok` every time. The probe was
  rewritten to use SQLite's backup API and a dedicated home, and passes. No
  product defect is claimed, but the single observation is recorded rather than
  hidden.

## Weaknesses the audit could not remove

1. **No real model provider was available.** Agent behaviour was driven by a
   scripted provider. The loop's recovery and the finish gate are deterministic
   and tested; real-model argument quality, repair success and prompt-injection
   resistance remain unmeasured.
2. **No live network research.** URL/DOI registration and PDF parsing were not
   exercised end to end; only local Markdown, binary and size-limit paths were.
3. **Experiment execution was not run.** Guards, pre-registration immutability
   and the statistics were tested; actually executing CPU_SMOKE seeds and
   comparing them was not, because this environment cannot supply a model key or
   long-running compute.
4. **Task-process crash recovery was not exercised.** Database durability under
   SIGKILL was; killing a worker mid-agent and resuming that run was not.
5. **Single platform.** Linux, Python 3.12, SQLite from the interpreter; no
   Windows/macOS, no hosted multi-user isolation.
6. **Not a penetration test.** No fuzzing, memory-safety analysis, dependency
   audit or network attack surface was in scope.

## Reproduce the two fixed defects

```bash
# 1. remote token (before the fix this printed ghp_…)
python - <<'PY'
from mayaworkbench.app.container import Services
# attach a repo whose origin is https://x-access-token:ghp_…@github.com/o/r.git
# then: print(repository.remote_url) and grep the home for the token
PY

# 2. malformed argument (before the fix the run settled FAILED with ToolError)
#    run a scripted task whose first action is
#    write_file {"path": "../evil.py", "content": "x"}
```

Both now fail closed as described above, with regression tests in
`tests/test_audit_findings.py`.

# Phase 3 report — controlled engineering agent

Status: implemented for the scope in
[docs/00-product-freeze.md](00-product-freeze.md) and
[ADR 0008](adr/0008-controlled-engineering-agent.md). Phase 4 verification and
Phase 5 experiments remain deferred and are not stubbed.

## 1. What was built

A `tasks` package (workflow, approvals, planning, agent loop, diff review) and a
`workspace` package (path policy, patch engine, worktree manager, command runner,
typed tool gateway), plus migration `0003`, storage, CLI, a separate worker
process, exported schemas and tests.

### 1.1 Task contracts and workflow

- `Task` + immutable, hash-addressed `TaskContract`: goal, pinned snapshot and
  full base commit, allowed/excluded paths, acceptance criteria, unknowns,
  budgets, required approvals and contract version.
- Explicit state machine: `CREATED`, `CONTRACTED`, `PLANNING`,
  `AWAITING_APPROVAL`, `IMPLEMENTING`, `PAUSED`, `REVIEWING`, and terminal
  `COMPLETED`, `COMPLETED_WITH_LIMITATIONS`, `FAILED`, `BLOCKED`, `CANCELLED`,
  `NEEDS_HUMAN_DECISION`.
- Only `WorkflowService` mutates state. Every transition is compare-and-set and
  append-only evented. Terminal runs cannot be reopened.
- A task cannot start a second active run. Each run is one attempt.

### 1.2 Plans and approvals

- `PlanService` asks a Technical Planner role for a structured plan, or accepts
  a user-supplied PlanPayload JSON file (`--plan-file`) for offline operation.
  Plan paths are validated against the contract before storage.
- Approvals are durable rows with action, resource scope, exact spec SHA-256,
  nonce, expiry, decision and one-time consumption. The write approval binds the
  contract content, plan hash, snapshot/base commit and path scope. The command
  approval binds the complete command policy.
- The gateway re-checks the spec hash before every write or command. A changed
  plan, contract, scope or policy requires a new approval. Expired, denied,
  already-used and mismatched approvals are refused.

### 1.3 Isolated worktrees

- Each run gets a unique detached `git worktree` under `$MAYA_HOME/workspaces`.
- The workspace is reconstructed from the pinned snapshot: commit, working-tree
  patch and untracked files restored from indexed content artifacts. Every
  indexed file is hashed and compared with the snapshot before the agent starts.
- The original checkout's HEAD/status are recorded at creation and re-checked
  after the run. A change is surfaced as an `ORIGINAL_CHECKOUT_CHANGED` error and
  blocks acceptance.
- Path traversal, symlinks, `.git`, excluded paths, credential-shaped paths and
  external paths are refused by the path policy.

### 1.4 Typed tool gateway

Tools: `read_file`, `list_files`, `search_text`, `apply_patch`, `write_file`,
`delete_file`, `git_status`, `git_diff`, `run_command`. Every invocation is
recorded before side effects, audited with redacted arguments and an output
artifact. Tool output is untrusted data and cannot change policy.

- Patches are parsed and applied with **no fuzz**: exact context, exact line
  counts, all-or-nothing writes with rollback, no renames/mode changes/binary
  patches.
- Command execution uses argv arrays, no shell, an executable allowlist, minimal
  environment (no provider keys or repository scripts), fixed cwd, timeout with
  process-group termination, output caps/truncation, dependency-install refusal
  and absolute-path refusal. Network namespace isolation is used when the host
  supports it and the actual enforcement mode is recorded in every result.
- File deletion is disabled unless the contract explicitly enables it.

### 1.5 Separate worker and bounded agent

- `maya worker --once --run-id ...` is a separate process. Jobs have leases,
  heartbeats, attempts and crash reclaim. The CLI only enqueues and launches it.
- One model step proposes one typed action. Application code validates it,
  executes it through the gateway and records the observation.
- Budgets: wall clock, model tokens, model cost, steps, tool calls, per-command
  timeout/output and repair attempts. Zero monetary budget means no paid usage.
- Pause is checked at safe points; cancel is checked during commands and kills
  the process group. A paused run resumes with its persisted tool history.
- A repeated failing action and repeated repair failures stop the loop instead
  of spinning.

### 1.6 Diff capture and review

- The candidate patch is captured with `git add -A` and `git diff --cached
  --binary`, including untracked files, and stored as a content-addressed
  artifact with SHA-256.
- The structural review reports scope violations, excluded/credential paths,
  introduced credential material, test file modification/deletion, added
  skip/xfail markers, dependency changes, binary changes, large patches, an
  empty patch and original-checkout drift. These are explicit findings with
  severities, never a pass verdict.
- `maya task finish --accept` is a user decision, not a verification decision.
  It refuses blocking findings and records the Phase 4 verification gap in the
  run's limitations.

### 1.7 CLI

`maya task create|list|show|start|approve|deny|approvals|run|status|pause|resume|cancel|diff|review|finish|cleanup|export`
and `maya worker`.

## 2. Enforced invariants

- A model response cannot change workflow state, grant an approval, change
  scope or execute a tool outside the gateway.
- Writes require a consumed approval whose hash matches the current contract and
  plan; commands require a consumed policy approval.
- The agent never writes to the user's checkout. The workspace is a detached
  worktree and the original HEAD/status are verified.
- `read_file` requires exact snapshot content at worktree creation; the agent
  cannot invent source from a prompt.
- Every command records argv, exit status, duration, env allowlist, network
  enforcement, output artifact and truncation.
- The patch is reproducible from the workspace, and every review claim links to
  a finding or measured diff fact.

## 3. Deliberate limitations

| Limitation | Consequence |
| --- | --- |
| A worktree is not a security sandbox | repository code runs as the user; it can read files the user can read. This is disclosed and original-checkout drift is detected, not prevented. |
| Network isolation is best-effort | `unshare -n` is used when available; otherwise the result records `network_enforcement=unavailable`. |
| Command approval covers a policy, not each exact argv | the approved resource scope is the executable allowlist, limits, cwd and network policy; every command is audited. A changed policy needs a new approval. |
| Phase 3 has no deterministic verification gates | acceptance is a human decision after structural review. `COMPLETED*` means the user accepted; it does not mean tests passed. |
| No apply-to-checkout command | the patch is exported; applying it to the user's checkout is deliberately left to the user / Phase 4. |
| Model required for an agent run | without a configured model, `task start` fails at planning; `--plan-file` avoids the planner call but the Implementer still needs a model. |
| SQLite single writer | the worker and CLI share a WAL database; long write transactions are avoided, but this is not a multi-tenant store. |
| Diff review is structural | test-integrity analysis does not prove semantic equivalence; Phase 4 adds independent review and checks. |

## 4. Verification

- `python -m compileall -q src tests` passes.
- The full pytest suite passes (see the final section for the exact count).
- A real separate worker process is exercised by `tests/test_task_cli.py`
  against a local OpenAI-compatible mock: plan → approvals → worktree → reads,
  patch, command, finish → diff → review → accept, with the original checkout
  unchanged.
- Tests cover scope escape, symlink escape, secret writes, stale/fuzzy patches,
  approval expiry/replay/mismatch, terminal transitions, pause/resume, cancel,
  job leases, command allowlist/timeout/output caps, budget exhaustion, original
  checkout drift, diff review, export and CLI errors.

## 5. Deviations recorded honestly

- `PAUSED` is added to §10.4's suggested state list because Phase 3 explicitly
  requires pause/resume without creating a new run.
- `delete_file` is authorized by the contract-level `allow_file_deletion` flag
  rather than a separate per-file approval. It is disabled by default and still
  passes path, secret and review checks.
- `task finish --accept` is a user review decision. The specification's full
  task decision vocabulary belongs to Phase 4, so this phase uses
  `COMPLETED*` and states the verification gap instead of implying acceptance
  gates.
- The worker is a local process under the user's account, not a hostile
  multi-tenant isolation boundary. This matches §12.5 and is repeated in the
  threat model.

## 6. Post-audit remediation (18 November 2026)

The adversarial audit (MWB-001 … MWB-014) found that the CLI did not expose this
phase and that several execution guarantees were recorded but not enforced. The
following fixes are implemented and covered by regression tests.

**Environment:** Python 3.12.3, Git 2.43.0, SQLite 3.45.1.
**Command:** `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --tb=no -p no:cacheprovider`
**Result:** `198 passed in 17.56s` (whole suite).

- **MWB-001** — `maya task …` and `maya worker` are registered by
  `cli/task_commands.py`; `tests/test_task_cli.py` exercises create → start →
  approve → run (separate worker process) → diff → review → finish, and
  `tests/test_research_cli.py` asserts every Phase 2/3 command area is present.
- **MWB-002** — under `DENY_BY_DEFAULT` the runner verifies a namespace with no
  default route and **refuses to start** the command when isolation is
  unavailable. `ALLOW_WITH_APPROVAL` is the explicit opt-in and still requires a
  consumed `COMMAND_EXECUTION` approval.
- **MWB-003** — the runner kills the whole process group when the leader exits
  (not only on timeout/cancel) and records `cleanup_required`/`cleanup_succeeded`.
- **MWB-009** — stdout/stderr are drained into bounded collectors; output beyond
  the cap is truncated without killing a valid command or filling temporary
  storage.
- **MWB-010** — `resource.setrlimit` applies CPU time, address space, file size,
  open files and a per-uid process ceiling before `exec`. If the host cannot
  enforce limits, the command is refused rather than run unbounded. Covered by
  `tests/test_runner_limits.py`.
- **MWB-006** — a single `BudgetController` reserves input plus output tokens for
  planner and implementer calls, caps the output allowance to the remaining
  budget, and refuses calls whose price is unknown unless the contract sets
  `allow_unknown_cost`. Unknown cost is never added as zero.
- **MWB-007** — workspace creation records a baseline of ignored paths. New
  ignored files created during a run are force-added to the candidate patch with
  a warning; removed ignored paths are reported.
- **MWB-014** — `TaskContract.research_context` pins document versions, claim IDs
  and the claim-matrix artifact (with its SHA-256). The planner receives the
  claim set. Migration `0004` adds the column.

**Environment limitation recorded honestly:** this build host denies
`unshare -n`, so command-execution tests use an explicit
`network_access="ALLOW_WITH_APPROVAL"` contract. That is a deliberate contract
choice, not a weakened assertion: the fail-closed `DENY_BY_DEFAULT` path has its
own regression test.

## 7. Scale and concurrency hardening (18 November 2026)

Measured, not asserted. Indexing this repository itself through the installed
CLI on Python 3.12.3 / SQLite 3.45.1:

| Metric | Value |
| --- | --- |
| Files / lines indexed | 131 files, 40,611 lines, 1,571,442 bytes |
| Symbols / edges | 1,799 symbols, 10,196 edges (3,159 resolved) |
| Index duration | 1,711 ms |
| Database size after index | 22.7 MB (WAL) |
| Re-index (same snapshot) | 2.2 s wall clock |

Guarantees added in this pass, each with a regression test in
`tests/test_concurrency.py`:

- **One active run per task.** The active-run check and the run insert share a
  `BEGIN IMMEDIATE` transaction, so two CLI processes starting the same task
  cannot both create a run. The write lock is held only for the reservation,
  never across model planning.
- **One active worker job per run.** Enqueue is transactional, and `claim`
  refuses a run whose other job still holds a live lease. A duplicate queued row
  left by an older build can no longer cause two workers to execute one run.
- **No infinite crash loop.** A job that crashes its worker `MAX_ATTEMPTS = 5`
  times is reaped as `FAILED` and its run is transitioned to `FAILED`, instead of
  being reclaimed forever and leaving the run stuck in `IMPLEMENTING`.
- **State change and audit event commit together.** `WorkflowService.transition`
  runs the compare-and-set and the `workflow_events` append in one transaction;
  a failure to write the event rolls the state back.
- **A stale worker cannot settle a run.** Before writing any run state the
  worker re-checks that it still owns the job lease; if another worker took
  over after a stall, settlement raises `LeaseLost` and writes nothing.
- **Thread-safe database access.** `Database` guards its single connection with
  a re-entrant lock and opens it with `check_same_thread=False`; a thread pool
  can share one process-level `Database` without SQLite `ProgrammingError`.
- **Research memos describe evidence gaps.** A project with registered documents
  but zero extracted claims now produces a memo that states the gap, instead of
  raising "no claims or research documents".

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

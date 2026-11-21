# ADR 0011 — A deterministic finish gate, typed repair feedback and grounded orientation

- Status: Accepted
- Date: 2026-11-20
- Applies to: `tasks/agent.py`, `tasks/inloop.py`, `tasks/orientation.py`,
  `workspace/tools.py`, `worker.py`, `verification/*`

## Context

Phase 3 produced one bounded action per model step, with a mandatory `finish`
action whose summary was recorded as a claim, not a verdict. Phase 4 then ran
declared checks *after* the run ended. That ordering is honest but weak: the
model could stop believing it was done, the checks could fail, and the only
feedback was a report the model never saw. In practice that means an implementer
that never reads its own test failures.

Two failure modes had to be avoided while closing that gap:

1. **The model grading itself.** If `finish` were accepted on the basis of the
   model's `checks_run` field, the product would be back to self-assessment.
2. **Repairing the wrong thing.** A check that fails because the *check* is
   broken (no tests collected, bad option, import-time internal error) is not
   evidence that the code is wrong. Feeding that back as "fix your code" invites
   speculative edits and can make a correct patch worse.

The model also started each run blind: an inventory of up to 400 paths, no idea
which mattered, so its first steps were `list_files` and keyword guesses.

## Decision

### 1. `finish` is a request; application code decides

When the contract declares verification checks and command execution is
authorized by a consumed approval, the loop runs those checks against the
candidate workspace before accepting a finish request.

- **All checks pass or skip** → the run proceeds to `REVIEWING`, with an explicit
  limitation that `maya verify run` re-executes them as evidence.
- **At least one check fails for a code-level reason and repair budget remains**
  → the loop appends a `REPAIR_REQUIRED` payload (check key, gate, exit code,
  reason, failing test ids, bounded output excerpt) and continues in the same
  run under the same step/tool/token/time budgets. `max_repair_attempts`
  bounds the rounds (initial + N repairs).
- **Only infrastructure failures** → no repair prompt. The limitation names each
  check and says it produced no verdict.
- **Failure with no repair budget left** → the run still goes to `REVIEWING`,
  with the failure in limitations. `maya verify run` will fail it; the loop
  never turns a failure into a pass.

Checks are executed by `DeclaredCheckRunner`, which matches keys against the
immutable contract, takes argv only from the contract, and uses the same
`CommandRunner` (allowlist, no shell, rlimits, network policy, timeout) as
verification. The model cannot add, edit, skip or waive a check.

In-loop results are labelled `in_loop` in run details, artifacts and events, and
are never written as `VerificationCheck` rows. The authoritative evidence stays
with Phase 4, which re-executes everything after patch capture.

### 2. Checker failures are classified separately

pytest's documented exit codes distinguish a test failure (1) from a broken
checker (3 internal error, 4 usage error, 5 no tests collected, 6 warning
ceiling) and an interrupted run (2). Only exit 1 is treated as a repairable code
failure; 3–6 stop the repair loop and are reported as infrastructure failures.
For non-pytest commands, a non-zero exit is a failure but an absent exit code
(blocked, cancelled, no result) is infrastructure.

This is the "verdict contract" idea: every failed check is attributed either to
the artifact (retry with feedback) or to the apparatus (stop and say so).
Treating the second as the first is how a repair loop makes things worse.

### 3. Orientation is retrieved, bounded and explicitly stale

The first two model steps receive a `repository_orientation` payload built from
the pinned snapshot's index: top retrieval spans for the task goal, symbols
defined in the allowed paths, relevant test files, and index issues that make a
file unreliable evidence. It is capped (~5 kB) and dropped from the prompt after
two steps, where the durable ledger becomes the better context.

The payload says the orientation describes the base revision and that the live
worktree must be read before editing. Orientation is best effort: an unindexed
snapshot or a retrieval error becomes a labelled field, never a silent guess and
never a gate.

### 4. Working memory is derived, not remembered

`workspace_ledger` reconstructs files read (with ranges), files changed, files
changed-but-not-re-read, commands with exit codes and declared checks executed —
all from the audited `ToolCall` rows. Because it is derived, it survives pause/
resume, prompt truncation and process restarts, and it cannot drift from the
audit log.

### 5. Progress guards are separate from failure guards

A repeated read-only action cannot change the workspace. After one duplicate the
loop returns a notice; at four it stops as `BLOCKED` with a `repeated action`
error. Writes and commands are not treated this way: retrying an edit after new
information is legitimate, so they remain governed by the existing repair budget
and three-identical-failures guard.

### 6. New typed tools, no new authority

- `run_check {key}` runs one contract-declared check, still requiring the
  consumed COMMAND_EXECUTION approval and the command allowlist.
- `find_symbol {query, limit}` searches the pinned index for orientation; it
  filters excluded and credential-shaped paths so it cannot reveal what the
  other tools hide.

Neither tool can choose argv, cross the path policy, or write.

### 7. Robustness boundaries are explicit

- File reads decode UTF-8, UTF-8/UTF-16 with a BOM, and otherwise return a
  visibly lossy decode (`encoding: utf-8-replaced`) with a count of replaced
  characters. UTF-32 is refused rather than decoded as UTF-16 garbage.
- Writes preserve an existing file's dominant EOL (CRLF/CR) and a UTF-8 BOM, so
  a model rewrite does not produce a whole-file diff or corrupt the encoding.
- Patch mismatches report the first differing line with expected/found text and
  name whitespace, line-ending and CRLF causes; patches that would introduce a
  NUL byte are refused.
- Paths reject C0/C1 controls, bidi overrides and line separators, and enforce a
  255-byte component limit, in addition to the existing containment, scope,
  symlink and secret rules.
- The indexer honours UTF-8/UTF-16 BOMs instead of reporting a Windows-edited
  Python file as `PARSE_ERROR`; a UTF-16 text file is no longer `SKIPPED_BINARY`.
- The CLI treats a closed stdout pipe as success and replaces undecodable
  characters instead of aborting mid-table.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Accept `finish` and only verify afterwards | Keeps the model blind to its own failures; the most common failure mode stays unfixed |
| Let the model run arbitrary checks to "see if it works" | Expands authority; declared checks already express what the user wants proven |
| Retry every non-zero exit | Retries a broken checker forever and invites test tampering |
| Store repair state in a new table | `run.details` plus append-only events and artifacts already carry it; a new table would duplicate authority |
| Re-plan after a failing check | Changes the approved plan/scope; repair must stay inside the approved contract |
| Give the model the whole repo map | Token cost on every step, and it still lacks the task-specific ranking |

## Consequences

- A run can now consume model tokens it would not have spent before: each
  failing finish triggers a repair step. Bounded by `max_repair_attempts`, step/
  token/cost budgets and the wall clock (per-check timeouts are clamped to the
  remaining round budget).
- In-loop checks execute repository code earlier than before. They are the same
  commands the user declared for verification and run under the same approval
  and command policy.
- pytest-specific exit-code semantics are encoded; other runners are treated
  generically. A wrong classification can only cost a repair round or stop one
  early — it cannot mark a failure as a pass.
- `task status` now shows the in-loop rounds, and two event types
  (`AGENT_CHECK_COMPLETED`, `AGENT_REPAIR_REQUESTED`) are part of the log.
- The loop protocol version is recorded as `AGENT_LOOP_VERSION = 1.1.0`; the tool
  gateway contract is `1.1`.

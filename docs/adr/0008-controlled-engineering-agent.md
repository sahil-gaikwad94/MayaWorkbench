# ADR 0008 — controlled engineering agent, approvals and isolated workspaces

Status: accepted (Phase 3)

## Context

The product needs a model to make code changes without letting model output
become authority. Phase 1 already has pinned snapshots, content-addressed
artifacts, events and a provider-neutral model gateway. Phase 2 added claim and
evidence models. Phase 3 must add a bounded engineering workflow that never
writes to the user's checkout, records every tool call, and can be paused,
cancelled and inspected.

## Decisions

1. **Task contract is immutable and hash-addressed.** Scope, budgets, criteria
   and required approvals are fixed before planning. No model response can edit
   them.
2. **Only the workflow service changes state.** Transitions use an explicit
   graph and a compare-and-set update in the same transaction as an event.
   Terminal states are final; retries are new runs.
3. **Approvals are durable records, not booleans.** They bind an exact action
   spec hash, nonce, expiry, scope and one-time consumption. The write approval
   includes the plan hash; the command approval includes the command policy.
4. **A separate worker process executes the agent.** The CLI creates records and
   launches `mayaworkbench.worker`. Jobs have leases, heartbeats and crash
   reclaim. A crashed worker does not silently keep authority.
5. **Worktrees are the write boundary, not a security boundary.** Each run gets
   a unique detached worktree reconstructed from the snapshot. The original
   HEAD/status are verified at creation and after the run. The limitation that
   repository code can still escape a worktree is disclosed in results and the
   threat model.
6. **One typed tool gateway.** No free-form shell. Tools validate typed
   arguments, enforce path/symlink/secret policy, require approvals and record
   audit rows/artifacts. Patches apply with no fuzz and all-or-nothing writes.
7. **One action per model step.** The model proposes a `thought`, a registered
   tool and arguments. Application code validates and executes. A model cannot
   call the workflow, approval or event APIs.
8. **Budgets are enforced by application code.** Steps, tool calls, wall clock,
   tokens, cost, command timeout and output, repair attempts and command policy
   are checked in the loop, not in the prompt.
9. **Phase 3 review is structural, not verification.** The diff review reports
   scope, secrets, test modifications, dependency/binary changes, size and
   original-checkout drift. Acceptance remains a human decision and the report
   says verification is Phase 4.

## Consequences

- The agent can be stopped safely: pause sets a flag and the loop stops at the
  next safe point; cancel is observed during commands and terminates the process
  group.
- Every write and command is auditable, but the design is intentionally
  conservative: commands are class-approved and deletion is opt-in.
- A malicious repository can still affect the user's machine through executed
  code. The product does not claim otherwise; hosted execution needs the
  isolation work described in §12.6.
- Phase 4 can consume the patch, command artifacts, review findings and status
  records without reinterpreting what happened.

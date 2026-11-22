# Spec — parallel read-only discovery (WP-ORCH-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — opt-in bounded parallel payloads; artifacts/events/state published sequentially in plan order.
**Vision source:** Audit B Upgrade 3; v2.1 §7.3, §10.2.

## Problem

The role orchestrator walks all deterministic analysis roles sequentially. The
research and workspace analysts are independent read-only jobs, so their latency
is currently summed rather than overlapped. Naive parallelisation would be
unsafe: SQLite writes, artifact ids and the durable handoff ledger must remain
sequential and ordered.

## Product outcome

Opt-in parallel execution of independent read-only discovery roles with
sequential, ordered artifact publication and unchanged state-transition
authority. Enable per run via `run.details.role_plan_parallel = true`; default
off for backwards compatibility.

## Design constraints

- Parallelism applies ONLY to deterministic read-only roles with no unmet
  dependencies and no implementer/reviewer/release role in the group.
- Worker threads compute payloads from immutable inputs only. No thread writes
  to SQLite, artifacts, events or run state. The main thread publishes results
  in original step order, preserving idempotency and ledger order.
- Bounded worker count; per-task failures become typed `RoleArtifact` records
  with StepState.FAILED, never silent skips.
- A resumed run must not re-execute or re-publish completed steps.

## Acceptance criteria

1. With the flag on, a group of two independent read-only steps overlaps (test
   with a barrier) and publishes artifacts in plan order.
2. With the flag off, behaviour is unchanged.
3. A payload failure produces a FAILED artifact with a reason and does not stop
   unrelated steps from being reported.
4. Resume after partial completion executes only pending steps.
5. Full suite green; status docs updated.

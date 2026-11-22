# Spec — evaluation integrity and method-aware validation (WP-R2I-2)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — frozen-hash harness gate, four extra method probes, formal-constraint plugin boundary.
**Vision source:** Audit B Upgrade 4; Audit A §6.3, §10.4; v2.1 §4.1, §4.6, §9.

## Problem

A coding agent can pass a task by weakening the yardstick: skipping a test,
lowering a tolerance, changing the metric function, or swapping the dataset
split. Existing test-integrity checks catch common test edits, but the
**evaluation harness itself** — metric code, split manifests, seed loops,
thresholds — is not frozen. A "green" result can therefore be a measurement of
a different, easier problem. Separately, method validation currently covers six
property probes; several defining ML properties (numerical stability under
reduced precision, gradient flow with expected exceptions, split/preprocessing
parity) are not covered.

## Product outcome

A hard, deterministic `EVAL_TAMPERING_BLOCKED` gate for unapproved changes to
evaluation-critical files, an explicit frozen-harness manifest recorded in
evidence, and additional method-specific probes. No model output can waive the
gate.

## Scope (in)

- `verification/evaluation_integrity.py`: declare evaluation-critical paths via
  the task contract (new optional `CheckSpec.details.evaluation_paths` +
  contract-level details extension if already available; otherwise a new
  dedicated field on `TaskContract` with a migration only if genuinely needed),
  hash their bytes at pin time, and compare against the candidate tree.
  Classify changes as `UNCHANGED`, `APPROVED_TREATMENT`, `TAMPERING`; require an
  explicit, scope-bound, hash-matched approval for any change.
- Wire as a new required-only-when-declared check key
  `evaluation.integrity`, with `CheckOutcome.BLOCKED` on tampering and a
  verification decision of `BLOCKED`/`REJECTED` (never a pass). Preserve
  append-only attempts.
- Extend `verification/math_invariants.py` with probes: reduced-precision
  stability (fp16/bf16 tolerance-aware), gradient flow with declared expected
  exceptions, split/preprocessing parity, and seed determinism. Each probe keeps
  the existing PASS/FAIL/SKIP discipline and emits structured details; none
  claims a mathematical proof.
- `ConstraintVerifier` plugin protocol with a no-op-by-default registry: a
  formal solver adapter can be registered, but absence is `NOT_CHECKED`, never
  a pass.
- CLI registration for the new check keys and probes; docs; tests.

## Explicitly out

- Bundling Z3/SymPy as hard dependencies or claiming formal proofs.
- Per-language semantic parsing, UI redesign, live model evaluation.

## Design constraints

- Freeze by content hash, not by path string alone; resolve symlinks and refuse
  paths outside the pinned snapshot.
- An approved treatment must name the exact path and hash; a generic "approval
  to edit evaluation code" is not sufficient.
- The new probes must SKIP honestly when the prerequisite (torch, target
  callable, declared split) is absent.
- Preserve backwards compatibility: existing contracts without declared
  evaluation paths are unaffected.

## Acceptance criteria

1. Changing a metric helper without approval yields `evaluation.integrity` BLOCKED
   and a non-passing decision.
2. A declared treatment with matching path+hash allows the change and records it
   as `APPROVED_TREATMENT` (not as unchanged).
3. A symlink escaping the snapshot is refused before hashing.
4. Each new probe passes on a correct tiny implementation, fails on a deliberately
   broken one, and SKIPs without its prerequisite.
5. No registered `ConstraintVerifier` yields `NOT_CHECKED`, never PASSED.
6. Full suite green; status docs updated.

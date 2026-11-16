# ADR 0005 — CLI-first surface, no empty scaffolding

Status: accepted (Phase 1)

## Context

The specification describes a multi-phase product ending in task contracts,
isolated worktrees, patch verification and an evaluation harness. It also
demands that no capability be claimed without implementation. There is pressure
to "prepare" later phases by creating tables, commands and modules up front.

## Decision

1. **The CLI is the Phase 1 product surface**, with `--json` as the integration
   contract. A future UI maps to the same service calls (see `docs/ui-flow.md`).
2. **No empty scaffolding.** Phase 3/4 tables (`tasks`, `approvals`,
   `worktrees`, `runs`, `claims`) are not created; commands that would only
   print "not implemented" do not exist. The gap analysis records the deferral
   and its prerequisites.
3. **Contracts are shaped to allow later phases** — events, artifacts and QA
   records carry correlation IDs and snapshot identity — without pretending the
   later behaviour exists.
4. **Every command must do real work or not exist.** Help text, errors and JSON
   output are part of the contract and tested end-to-end.

## Consequences

- Positives: the shipped surface has no dead paths; tests cover real behaviour;
  no migration debt for tables whose design would change once real tasks exist;
  the spec's "no symbol-name-only claim" rule extends to features.
- Negatives: later phases will need new migrations and commands; some users may
  expect a roadmap-shaped UI that lists future features.
- Revisit if: a phase is descoped permanently (then remove it from the docs) or
  when Phase 3 begins (design tables from real task-contract usage, not
  speculation).

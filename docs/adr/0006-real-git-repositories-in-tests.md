# ADR 0006 — Real Git repositories and real SQLite in tests

Status: accepted (Phase 1)

## Context

The riskiest behaviour lives at the boundary with Git and SQLite: object
reading, dirty-tree detection, patch hashing, transaction durability, FTS
behaviour and migration checksums. Mocking those boundaries would test the
mocks and miss exactly the bugs that matter (one such bug — SQLite implicitly
opening transactions and swallowing an explicit commit — was only found by
running two processes against a real database).

## Decision

- Integration tests create **real temporary Git repositories** with multiple
  commits, a dirty worktree, an untracked file, `.gitignore`, a secret file, a
  binary, a broken Python module and a submodule entry.
- Tests run the **real CLI entry point** (`main(argv)`) in-process and parse
  `--json` output, covering the full workflow.
- Storage tests use a **real SQLite file** (not `:memory:`) so WAL, file
  durability and cross-connection visibility are exercised.
- Model tests use a **provider double injected at the factory boundary**, not a
  patched HTTP stack, so policy, accounting and citation validation are real.
- No network access is required or permitted in the suite.

## Consequences

- Positives: failures correspond to real failure modes; the suite catches
  environment bugs (Git argv/env, transaction handling, FTS availability);
  tests double as executable documentation of the workflow.
- Negatives: suite runtime grows with subprocess Git calls (currently ~5 s for
  69 tests); tests depend on a Git executable and a SQLite build with FTS5
  (checked explicitly).
- Revisit if: suite runtime becomes a bottleneck — optimise with session-scoped
  fixtures before replacing real boundaries with mocks.

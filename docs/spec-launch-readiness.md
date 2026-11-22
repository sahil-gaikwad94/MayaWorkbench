# Spec — launch-readiness evidence and real integration gates (WP-LAUNCH-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — release evidence generator, CLI and both opt-in
live harnesses implemented; documentation in `docs/release-gates.md`.
**Vision source:** Audit A §4.5, §10.4, §11, MW-11; Audit B §2.2, Upgrade 1 and 6.

## Problem

The project can say "unit tests pass" and "the capability report looks right",
but a release still requires a human to stitch together commit SHA, dependency
versions, exact test command/counts, runtime capability evidence, adversarial
probe results and which integration gates actually ran. Today those facts live
in several documents and in conversation. A release summary can accidentally
claim more than was verified — the exact failure mode both audits flag.

## Product outcome

One command produces a machine-readable **release evidence record** from real
inputs, and two **real integration harnesses** that run only when their
infrastructure exists and otherwise report an explicit skip with a reason.
Nothing here claims a capability that was not exercised.

## Scope (in)

- `maya release evidence [--pytest-report PATH] [--adversarial-dir PATH] [--out DIR] [--json]`.
  The record contains: commit SHA and dirty flag; Python/pytest/pydantic/platform
  versions; a hash of the dependency manifest; the exact test command and
  collected/passed/failed/skipped counts parsed from a supplied pytest JSON/JUnit
  report; the canonical `ExecutionCapabilityReport`; adversarial harness counts
  when supplied; git status; status-doc freshness booleans; and an explicit
  `unverified` list. Missing required inputs cause a non-zero exit and a typed
  error — never a guessed value.
- `tests/integration/test_container_escape_live.py`, gated by `@pytest.mark.oci`
  and a real rootless OCI capability probe: populate overlay from a pinned
  fixture, assert a host canary and secret canary are unreadable inside the
  container, assert no home/SSH/socket/cloud-credential mounts, network none,
  pids limit applied, and process-tree cleanup. Skips with a concrete reason when
  no verified runtime is available.
- `tests/integration/test_model_live.py`, gated by `MAYA_LIVE_MODEL=1` plus an
  injected key and a hard spend cap: structured output validity, native tool-call
  parsing, and refusal behaviour against the configured adapter. No network call
  unless both gates are explicitly enabled.
- `docs/release-gates.md` listing exact commands, required artifacts, and which
  claims each gate does and does not license.
- Unit tests for the evidence generator (redaction, determinism, fail-closed on
  missing inputs).

## Explicitly out

- Running pytest, Podman or a paid provider from inside `maya release evidence`.
- CI configuration files for an external provider (document the commands instead).
- New execution or verification semantics.

## Design constraints

- The evidence generator reads, never mutates, the repository. It never executes
  repository code.
- It must not invent counts: without a pytest report, `tests` is `NOT_PROVIDED`,
  not `{passed: 0}`.
- Runtime capability comes from the one canonical report, not a new probe.
- Secret values and full environment are never written into the evidence file.
- Integration tests must be part of normal collection but skip fast and loudly
  (reason string) when infrastructure is absent.

## Acceptance criteria

1. `maya release evidence` with no report input exits non-zero with a typed error.
2. With a real pytest report, parsed counts equal the report's recorded counts.
3. The evidence file contains no `MAYA_*` secret values and no HOME path from the
   generator process.
4. Re-running with unchanged inputs yields the same evidence hash.
5. The live OCI test passes against a real rootless runtime; with none installed
   it reports `SKIPPED (reason: ...)` and never a pass.
6. The live model test makes zero network calls unless both gates are set and a
   spend cap is recorded.
7. Full suite green; docs and status table updated.

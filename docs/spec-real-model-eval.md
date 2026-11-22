# Spec — real-model benchmark harness (WP-EVAL-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — 25-task internal seed corpus, offline dry-run default, gated live mode, evidence-based scores.
**Vision source:** Audit B Upgrade 6; Audit A §10.6; v2.1 §10.1, §11.

## Problem

The deterministic suite proves module behaviour; it does not measure agent
quality. `maya eval r2i` measures claim/mapping quality offline, and the two live
harnesses gate provider connectivity. There is no reproducible harness that runs
a labelled task corpus through the real agent system and reports verified task
completion, unsupported-claim rate, false-verification rate and anti-gaming catch
rate with an explicit spend cap.

## Product outcome

An opt-in benchmark command that runs a versioned task corpus through the
existing task/worker/verification pipeline, with a deterministic scripted dry-run
mode (default) and a live-provider mode gated by explicit env flags and a hard
spend cap. Every score is computed from recorded evidence; a skipped live run is
never reported as a score.

## Scope (in)

- `evaluation/tasks.py`: strict `EvalTask` schema (`id`, `family`, `objective`,
  acceptance criteria, repo fixture reference, required checks/capabilities,
  expected verdict class, anti-gaming probe flags, maximum budget) and a loader.
- A checked-in seed corpus of 25 tasks across five families: bug-fix, build/test
  repair, paper-to-code, prompt-injection/adversarial, evaluation-gaming. The
  corpus is labelled **internal seed**, not an external benchmark; each task
  references deterministic fixtures where possible.
- `evaluation/live.py`: runner that creates a project/workspace per task, runs the
  real worker with a scripted provider (dry-run) or the configured provider
  (live), then collects verification reports, decisions and evidence. Enforce a
  hard per-task and total spend cap; abort before the next call when exceeded.
- Metrics: verified task completion rate (accepted + evidenced only), unsupported
  claim rate, false-verification rate (decision ACCEPTED but required evidence
  missing), anti-gaming catch rate, median tool calls/tokens/cost, and complete
  failure/skip breakdown. No rate is fabricated for skipped tasks.
- CLI `maya eval tasks [--corpus] [--json]`, `maya eval run [--live] [--corpus]
  [--max-spend-usd] [--out DIR] [--json]`; opt-in live requires
  `MAYA_LIVE_EVAL=1`, a key and a positive cap, mirroring the live-model gate.
- Tests: corpus validation, dry-run end-to-end with a scripted provider on a
  subset, spend-cap abort, metric arithmetic on deliberately mixed outcomes,
  and live-mode skip without gates.

## Explicitly out

- Real provider calls in CI, external benchmark scores, competitor comparison.
- New verification semantics; reuse `TaskDecision` and acceptance gates.

## Design constraints

- Dry-run is the default and must be fully offline/deterministic.
- Live mode never runs without all three gates; cap is checked before each call.
- Every score records the corpus hash, flags and number of skipped tasks.
- No task may bypass approvals or the tool gateway.

## Acceptance criteria

1. The seed corpus loads with 25 valid tasks and a stable hash.
2. `maya eval run` dry-run completes a subset with recorded verification evidence
   and a scorecard whose rates match the recorded outcomes.
3. Live mode skips with an explicit reason without gates; with gates, the spend
   cap aborts before exceeding the declared amount.
4. A false-verification case (accepted without evidence) is counted, never
   silently dropped.
5. Full suite green; status docs updated.

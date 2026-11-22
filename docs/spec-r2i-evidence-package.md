# Spec — R2I evidence package (work package R2I-E1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — implemented through domain, service, CLI,
API, artifacts, tests and status docs. The seed corpus is an internal labelled
dataset, not an external benchmark.
**Vision source:** `MayaWorkbench_MasterProductandEngineeringSpec.md` v2.1 §§2, 9,
11; Audit A §6.3/§6.4/§10.2; Audit B Upgrade 4.

## Problem

Maya can find a method in code, map claims to symbols, run checks and record
experiments, but the user still has to stitch together four different questions by
hand:

1. Did we find code that looks like the method?
2. Did we actually implement the method (not just find a name)?
3. Did we validate the method's defining properties?
4. Did we reproduce the reported result?

Today a green test command or a model-written summary can blur these into one
"done" claim. That is the exact failure mode the v2.1 spec forbids (§4.1, §4.6).

## Product outcome

A backend-computed, inspectable **reproducibility report** attached to a run, plus
an **offline evaluation harness** that measures whether claim extraction and
claim-to-code mapping are good enough to be trusted as inputs to that report.

The model may propose summaries. It may never assign the tier.

## Scope (in)

- `ValidationObligation`: typed, deterministic obligations derived from typed
  claims (shape/dtype/device, masking, numerical reference, gradient, invariants,
  split/preprocessing parity, metric, baseline parity, seeds).
- `ReproducibilityReport`: four independent tiers with explicit reasons and
  evidence references: `METHOD_CODE_FOUND`, `METHOD_IMPLEMENTED`,
  `METHOD_VALIDATED`, `RESULT_REPRODUCED`. Every tier may be `YES`, `PARTIAL`,
  `NO`, or `NOT_CHECKED`; `NOT_CHECKED` is never silent success.
- Deterministic computation from existing persisted facts: claim mappings,
  verification reports/checks, experiment comparisons, evidence bundle manifest.
  No new model call, no new trust authority.
- `evaluation/r2i.py`: gold-case schema and metric computation (claim precision,
  recall, F1 by kind; source-span citation validity; mapping precision/recall;
  correct uncertainty/`NOT_CHECKED` use). Deterministic, offline, no network.
- A checked-in **seed** gold corpus built from the repository's own paper/repo
  fixtures, with the explicit label that it is a seed, not an external benchmark.
- CLI: `maya research reproduction <run_id>` and `maya eval r2i --corpus <path>`,
  both with `--json`.
- API read route for the report; tests; docs; status table update.

## Explicitly out (this package)

- Downloading real papers or building a 25-paper external benchmark.
- Live model-provider evaluation (no key/cost authority here).
- New verification semantics or changes to `TaskDecision`.
- Remote/GPU execution, UI redesign, new migrations unless a persisted record is
  genuinely required (prefer deriving from existing records).

## Design constraints

- Derive, do not duplicate: the report is a pure function of existing records plus
  claim obligations. No parallel truth store.
- Fail closed: missing verification or experiment evidence yields `NOT_CHECKED`,
  never `YES`. A check that could not run is not a pass.
- Confidence/uncertainty from mapping (`UNCERTAIN`, `NOT_CHECKED`, incomplete index)
  must survive into the report; `ABSENT` with incomplete coverage is downgraded
  upstream and the report respects that.
- Every obligation and every tier cites the exact record id / artifact hash that
  produced it.
- Metrics never invent ground truth: the gold corpus is explicit input data.

## Acceptance criteria

1. A run with claim mappings but no verification yields `METHOD_CODE_FOUND` at
   most, with `METHOD_VALIDATED = NOT_CHECKED` and `RESULT_REPRODUCED = NOT_CHECKED`.
2. A run with passing declared checks and no experiment parity yields
   `METHOD_VALIDATED = YES` (where obligations are covered) and
   `RESULT_REPRODUCED = NOT_CHECKED`.
3. A run with a pre-registered experiment comparison showing baseline/candidate
   parity plus passing checks can yield `RESULT_REPRODUCED = YES` only when every
   required obligation has evidence; otherwise `PARTIAL` with named gaps.
4. A deliberately weakened test or tampered evaluation harness cannot raise a tier
   (existing integrity gates remain authoritative inputs).
5. Evaluator scores a deliberately imperfect prediction set with the expected
   precision/recall/uncertainty numbers; no metric is hard-coded.
6. Full suite green; compileall green; status docs updated; no leaked artifacts.

## Implementation notes (2026-11-22)

- Report assembly lives in `research/reproduction.py` (pure) and
  `research/reproduction_service.py` (store access, artifact + event). The report
  is a derived view persisted as a `REPRODUCIBILITY_REPORT` artifact; no new
  SQL table or migration was introduced.
- `research/obligations.py` derives typed obligations deterministically from
  claim kinds/validation methods and declared check keys.
- `evaluation/r2i.py` scores real exported claim/matrix artifacts against a
  checked-in seed corpus (`evaluation/gold/causal_attention_seed.json`). Metrics
  are computed, never hard-coded; zero denominators are reported honestly.
- CLI: `maya research reproduction <run>` and `maya eval r2i`.
- API: `GET /research/reproduction/{run_id}`.

## Durable design decision

A gold corpus is an input dataset, not application state: it is versioned JSON
shipped as package data, not a domain record with a store and migration. This
keeps the evaluator offline, reproducible and independent of a deployment DB.

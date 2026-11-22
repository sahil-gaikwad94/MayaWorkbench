# Spec — task workbench trust surfaces (WP-UI-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — four read-only run endpoints, seven browser panels, DOM-free tests and a real browser smoke check.
**Vision source:** v2.1 §4.9, §4.10, §5.2; Audit A MW-05; Audit B Upgrade 5.

## Problem

The local control plane exposes real backend data over JSON, but the browser
page is still mostly a set of raw-JSON boxes. The product's trust surfaces —
what will happen, what was approved, what the plan was, what the agent did, what
evidence exists, and what the experiment showed — are not laid out as a
supervisable task workflow. Audits rate this the weakest product surface.

## Product outcome

A single-page local workbench where a user can supervise one task end to end:
workspace and pin freshness, objective/constraints/runtime, plan and approvals,
execution timeline, captured diff, verification/evidence graph, and experiment
comparison. Every value comes from the backend; the UI never computes a verdict.

## Scope (in)

Backend read endpoints (no new write authority):
- `GET /runs/{id}/timeline` — ordered items from workflow events, tool calls and
  model invocations with `{at, kind, label, detail, ref, seq}`.
- `GET /runs/{id}/plan` — the approved plan, contract constraints, capability
  grants, resource scope and trust profile.
- `GET /runs/{id}/evidence` — the evidence graph (criteria → checks → artifacts,
  verification report, claim mappings, reproduction report, experiment links).
- `GET /experiments/{id}/comparison` — latest comparison with metrics/CI/effect
  size and fairness findings.

UI panels:
- Workspace overview (repositories, snapshot SHA, index/runtime freshness).
- Task overview (objective, acceptance criteria, state, budget, trust profile).
- Plan & approvals (DAG steps, capability scope, approval state, side-effect
  summary, working Approve/Reject actions wired to existing POST routes).
- Execution timeline (event/tool-call stream with durations and outputs).
- Review (unified diff from `/runs/{id}/diff`, file-scope checks, test results).
- Evidence (claim → source → check → artifact links with verified/partial/
  unverified labels and reproduction tier).
- Experiments (baseline vs candidate metrics, CI, effect size, parity findings).

## Explicitly out

- Authentication, multi-user, hosted deployment.
- New execution/verification semantics or state transitions.
- New model calls from the UI.

## Design constraints

- All data via existing services; handlers stay thin (no business rules in API).
- `textContent` only in the browser; no `innerHTML` with server data.
- Keep the NDJSON `fetch()` stream (MW-04 fix) intact.
- Endpoints are read-only and idempotent; no new artifacts/events per GET.
- Approvals use the existing `POST /tasks/{id}/approve` contract with an
  `approval_id` read from the task/run view.
- Degrade honestly: missing plan/evidence/comparison yields a typed empty state,
  never a fabricated success.

## Acceptance criteria

1. Each new endpoint returns real data for a fixture run and typed 404/empty
   states for a run without that data.
2. A browser test loads a completed task and sees its plan, timeline, diff,
   evidence tiers and experiment comparison rendered from backend payloads.
3. Approve action from the UI posts the exact approval id and updates state.
4. No console errors and no HTML injection from server-provided strings.
5. Full suite green; docs/status updated; no leaked artifacts.

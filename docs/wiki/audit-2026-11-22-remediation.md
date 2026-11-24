# 2026-11-22 audit remediation (working page)

Historical record for the work package driven by the two 2026-11-21/22 audits:
`MayaWorkbench_Rigorous_Audit_and_Upgrade_Report-1.md` (Audit A) and
`Mayaworkbench Rigorous Audit Repair And Upgrade Report.md` (Audit B).

- Start commit: `47c78e3` (`main`). Baseline: `python -m compileall -q src` OK;
  `python -m pytest -q` exit 0, 529 collected, two skips.
- Environment at start: Python 3.12.3, pytest 9.2.0, pydantic 2.14.4, Linux.

## Plan (ordered by the audits' own P0→P2 priority)

- WP-A Execution truth: cgroups readback/cleanup fix + runner wiring (B FIX-01);
  `READ_ONLY_ANALYSIS` profile (B FIX-02/03); canonical capability report
  (A MW-03); unified `RuntimeWorkspace` for file tools + commands + checks +
  verification (A MW-01/02); torch-assuming test fixture.
- WP-B Agent loop: `BROKEN_CHECKER` check classification and clean `REVIEWING`
  transition on provider exhaustion (B FIX-02 / A harness D7); wire
  `ExperienceMemoryService` + `AdvancedMemoryService` into Services/orientation
  and post-failure negative-result recording (B FIX-05).
- WP-C Correctness/UX: lockfile-aware environment resolver (A MW-06); NDJSON
  stream client instead of EventSource (A MW-04); CLI flags + `/ui` route
  (B FIX-07).
- WP-D Honesty/docs: capability manifests and truthful labels (A MW-07);
  single-source capability/status registry feeding README/status/test-matrix
  (A MW-12).
- WP-E Larger upgrades: native tool calling/streaming/providers (B FIX-04);
  Tree-sitter + real embeddings behind capability detection (B FIX-06 / A
  MW-08); trust-surface UI (A MW-05); research/connector work (A MW-09/10).

## Durable decisions (2026-11-22)

- Rootless isolation is fail-closed and unavailable by default: controller names are
  presence only, and availability requires a real OCI smoke test plus a writable cgroup.
  Do not reintroduce a silent fallback to host execution.
- `READ_ONLY_ANALYSIS` is a code-enforced tool mask, not a trust claim; it needs no OS sandbox.
- One `RuntimeWorkspace` per task is the only file view. An isolated overlay is populated
  from the pinned snapshot and retained until verification has compared candidate and
  baseline; no component may construct its own hidden runner.
- A finite cgroup limit requires an exact readback; `max` is never accepted as success.
- A broken checker is apparatus failure, not a code failure: finish transitions to REVIEWING
  with `BROKEN_CHECKER`/`ERROR` rather than burning a model turn on repair.
- Environment plans are pure argv plans; they never install on the host and never invent a
  `requirements.txt` for a lockfile-only project.

## Verification evidence (2026-11-22)

- At work start (commit `47c78e3`): `compileall` OK; `pytest -q` exit 0, 529 collected,
  two skips; adversarial harnesses 31+20+23+8 = 82/82 pass.
- After the execution/workflow/API/docs package: `pytest -q` exit 0, **568 collected,
  566 passed, 2 skipped**; `compileall` OK; adversarial harnesses still 82/82.
  The D7 broken-checker probe now settles as `FAILED`/`REJECTED` by the authoritative
  verifier instead of crashing into a provider-exhaustion `FAILED`.
- Later the same day the model platform gained native tool calls, streamed SSE,
  real Anthropic/Gemini adapters selected from config (no island modules), pluggable
  embeddings with RRF, optional Tree-sitter adapters, and a real MCP stdio transport
  with risk tiers and hash-bound grants. No live provider key or OCI host was used;
  all new transports are covered by fake-server/fake-opener tests.

## 2026-11-22 exploration findings (as of commit 47c78e3)

- Execution truth: `CgroupV2Scope.create()` accepts a `"max"` readback for a finite
  limit; `cleanup()` uses `shutil.rmtree` on cgroupfs; production never passes a
  `cgroup_scope` to `CommandRunner`. Rootless `provision()` creates an **empty**
  `/work`; source is mounted readonly at `/src`; host file tools edit
  `workspace.path`; `collect_artifacts()` is never called by the worker.
  `backends.capabilities()` claims cpu/memory/pids `enforced` from controller-name
  strings only, while `capabilities._cgroup_probe()` says "wired_into_runner: false".
  Docker's "rootless unverified" warning is ignored in availability computation.
- API/UI: server wire type is `application/x-ndjson` with bare JSON lines; UI uses
  `EventSource` (needs `text/event-stream` + `data: ` framing) → `onmessage` never
  fires. Approvals panel is a dead stub; "Diff/evidence" dumps a verification
  report, never the bundle route. No `/ui` route. `/events/stream` bounded to
  1–30 s with `end` line.
- Environment planner: `build_plan()` ignores `info.lockfiles`; pyproject-only
  emits `pip install -r requirements.txt`; node always `npm ci`; Java/C++/CI
  ecosystems detected but never planned. CLI `env detect/plan/doctor` take only
  `--path`; `workspace create` requires `--slug` (no `--name`).
- Models/connectors: no `tools`/`tool_calls`/`stream`/cache fields in
  `ModelRequest`/`ModelResponse`/`ModelCapabilities`; `streaming=False` hardcoded.
  (Fixed on 2026-11-23: all three adapters implement buffered and incremental
  streaming; see ADR 0017.)
  MCP adapter has no transport and no production caller. Advanced connector
  registry is CLI-only. Non-Python languages are regex/LOW/HEURISTIC; embeddings
  are hashing, reranking lexical, both off by default. Two same-named embedding
  classes across `capabilities.py` and `retrieval/semantic.py`.

## 2026-11-22 R2I evidence package (same day)

- Decision: a reproducibility report is a **derived view** of recorded claims,
  mappings, verification and experiment evidence — not a new truth store. It is
  persisted as a content-addressed artifact plus an append-only event; no new SQL
  table or migration was added.
- Decision: a gold corpus is a versioned input dataset (JSON package data), not a
  domain record. The evaluator is offline and scores real exported claim/matrix
  artifacts; the bundled corpus is explicitly labelled an internal seed, never an
  external benchmark.
- Four tiers are backend-assigned and conservative: absence of evidence is
  `NOT_CHECKED`, never success; imported experiment runs, fairness violations and
  missing obligations cap `RESULT_REPRODUCED` below `YES`.
- Spec: `docs/spec-r2i-evidence-package.md`. Wiring: `Services.reproduction`,
  `maya research reproduction`, `maya eval r2i`, `GET /research/reproduction/{run}`.
- **Next package under design (2026-11-22):** `docs/spec-workbench-ui.md` — the
  browser control plane still renders mostly raw JSON; the remaining audit gap is
  a real supervisable task surface (plan/approvals, timeline, evidence graph,
  diff, experiment comparison). Decision: add read-only backend endpoints and
  render them without any new backend write authority or model call. Shipped the
  same day: four GET endpoints + seven new panels, verified with DOM-free tests
  and a real browser check (no JS errors; honest 404 states).
- **Same-day follow-on packages (2026-11-22):** research evidence quality
  (version-bound claim citations, typed cross-source disagreements with append-only
  resolution, paper status/correction/retraction alerts, migration 0011) and signed
  portable run bundles (sign/verify/import with named key, verify-time HMAC checks).
  Neither grants a new verdict authority: retracted sources and disagreements feed
  readiness/reproduction as downgrade inputs; a signature identifies a key, not a
  scientific result.
- **Adversarial review of the four-tier logic (2026-11-22):** three fresh-context
  review cycles were run under the doubt-driven discipline. Cycle 1 found 24 issues
  (2 blocking), cycle 2 found 11 (6 major), cycle 3 found 6 (2 blocking). All were
  fixed with regression tests except where a fix was a bounded design decision:
  run-existence cannot be proved by a pure function, so invalid run-id shapes and
  unbound task-scoped comparisons cap `RESULT_REPRODUCED` at PARTIAL, and the live
  integration harness is the authority for run locality. No fourth review cycle was
  run (three-cycle bound). Cross-model second opinion remains available on request.

# Build history (dated)

- **2026-11-21 (pre-build audit baseline):** `python -m pytest -q` collected 328 tests with
  2 reproducible failures: the PDF pypdf fallback test and `test_background_child_...`
  (the latter via `RLIMIT_NPROC` `EAGAIN` in this environment). Audit doc:
  `MayaWorkbench-Part2_Audit_and_Upgrade_Plan.md`.
- **2026-11-21:** MW-P0-01 fixed — typed `EncryptedPdfError`, controlled builtin fallback,
  6 acceptance tests. `src/mayaworkbench/research/pdf.py`.
- **2026-11-21:** MW-P0-02/03 fixed — per-limit `getrlimit` readback, derived aggregate,
  `RLIMIT_NPROC` removed, explicit `ExecutionTrust` profiles that block unavailable runtimes.
  `src/mayaworkbench/workspace/runner.py`, `workspace/capabilities.py`, `maya runner doctor`.
- **2026-11-21:** WP1/2/3/4 packages completed through data model + service + CLI + tests:
  `ExecutionBackend` + rootless OCI backend, typed capability grants, persistent sessions,
  Engineering Workspace records. Full suite at that point: 373 passed, 0 failed.
- **2026-11-21:** P2 attestation added — optional HMAC signature over manifest root with
  `UNATTESTED`/`SIGNED_BY_USER` levels; `maya verify attest|check-attestation`.
- **2026-11-21:** WP5 completed — heuristic JS/TS/Go/Rust symbol adapters with LOW confidence
  and `HEURISTIC` provenance, repo map `heuristic_languages`, CLI symbol search/detail, and
  durable content-hash incremental indexing with `files_reused` evidence. Full suite:
  **380 passed, 0 failed** (baseline for the next work packages).
- **2026-11-21:** `AGENTS.md` rules file created as durable context for future sessions.
- **2026-11-21:** WP7/8/9/10/11 integrated (model platform + GitHub read-only connector,
  role orchestrator, mutation verification + security report, portable runner bundles,
  local loopback JSON control plane with NDJSON event streaming), plus experience memory
  and context-pack provenance. Verified full suite at integration: **437 passed, 0 failed**.
- **2026-11-21:** WP6 completed (typed claim kinds, explicitness, target resources,
  validation ids, `NOT_CHECKED` status, migration 0009, metadata discovery with host
  allowlist + immutable source versions, CLI round trip). Final full suite: **443 passed,
  0 failed**.
- **2026-11-21:** WP12 advanced connectors, raw LLM/agent capabilities, advanced
  tool families, persistent skills (migration 0010), and advanced memory governance
  completed end-to-end and integrated. Final verified full suite: **473 passed,
  0 failed**. Remaining scope is limited to intentionally deferred items in the
  truth table plus real-provider/host-infrastructure validation gates.
- **2026-11-21:** All eleven previously PARTIAL rows completed: north-star evaluation
  harness, C/C++/CUDA adapters, submodular selection, method-specific invariants, HMAC
  approvals, 18-state workflow, 18-tool gateway, cgroup-v2 pids limiter, user-owned
  runner bridge, workspace resource scopes. Verified full suite: **495 passed, 0 failed**.
- **2026-11-21:** Next set completed end-to-end: environment detection/plans, PTY + durable
  session replay, `SIGNED_BY_CI`/`.maya-proof.json`, MLflow/TensorBoard adapters, opt-in OCR,
  optional hashing embeddings/reranker/PPR with measured evaluation, and named v2.0 engine
  facades. Verified full suite: **517 passed, 0 failed**.
- **2026-11-21:** Final implementable rows completed: fail-closed remote microVM/GPU
  backend adapters, host-canary argv adversarial test, and a thin local control-plane UI
  served by the API (health, projects, tasks, approvals, events, evidence). Final verified
  full suite: **523 passed, 0 failed**. MCP interoperability adapter added next (protocol
  only, never a permission boundary); full suite: **526 passed, 0 failed**.
- **2026-11-21:** Rigorous fresh-eyes end-to-end audit: fixed hard-coded harness CLI paths,
  a false-green network-isolation probe (real security defect), the OCI-client regression,
  a PPR attribute bug, missing skills wiring, and an incomplete `api_request`. Verified:
  **82/82 adversarial probes**, 18/18 CLI steps on a 134-file repository, full task
  workflow to `COMPLETED_WITH_LIMITATIONS`, full suite **529 passed, 0 failed**.

Outstanding at this date: WP6 (typed claim graph / research discovery), WP7 (model capability
interfaces + routing + connectors), WP8 (role DAG), WP9 (mutation-based verification),
WP10 (runner bundles), WP11 (web/API control plane), WP12 (advanced connectors).
These are **not** claimed as shipped in `IMPLEMENTATION_STATUS.md`.

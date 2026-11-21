# Rigorous end-to-end audit (2026-11-21)

Fresh-eyes review, break-testing, and fixes applied after the feature work.

## Method

1. Full unit suite from a clean state.
2. Four adversarial harnesses (82 probes: pinning, gateway, network, CLI, storage,
   verification, worker, security).
3. Hunt for incomplete implementations and hardcoded assumptions.
4. Break-testing on a generated **134-file mid-sized multi-language repository**
   (`/tmp/opencode/mid-repo`: 60 Python modules, 20 test files, 25 TypeScript
   components, 10 Rust, 10 Go, C/C++/CUDA, configs).
5. Full CLI workflow and full task workflow on that repository.

## Defects found and fixed

| # | Finding | Fix |
|---|---|---|
| 1 | Adversarial harnesses hard-coded `.venv/bin/maya`, producing false ERROR results on a source checkout. | Added `artifacts/adversarial/_cli.py` resolver (installed `maya` → `python -m …` → local venv) and replaced every hard-coded path. Harness 1 then went 30→31 PASS. |
| 2 | **Security false-green:** `_NETWORK_PROBE` only checked for absence of a default route. On a host with no default route but reachable loopback, `DENY_BY_DEFAULT` reported "isolated" and ran connected commands (adversarial B11). | Replaced with a positive isolation proof: the child must fail to connect to a parent-owned loopback listener. Now fails closed on this host; B11 passes. Added regression test. |
| 3 | The OCI client used by `RootlessContainerBackend` applied `DENY_BY_DEFAULT` to itself. After fix #2, every container run was blocked even though the container enforces `--network none`. | Switched the trusted OCI client to `ALLOW_WITH_APPROVAL`; container isolation remains the boundary. |
| 4 | Optional PPR mode crashed: `RetrievalService` referenced `self.symbols` (nonexistent) instead of `self.search.symbols`. | Corrected the reference; verified PPR mode on the mid repository. |
| 5 | `Services` had no `skills` attribute; programmatic callers could not use the shipped skills service. | Wired `SkillsService` into the composition root with an alias. |
| 6 | `api_request` always failed closed with no injection path (incomplete implementation marked SHIPPED). | Added an injectable HTTP executor with SSRF-resistant host validation (all DNS answers checked for private/reserved), fixed HTTPS/443 only, no credential headers, bounded response, request/response SHA-256 provenance. Fail-closed when unconfigured. |
| 7 | Test helper imported the removed `_NETWORK_PROBE` constant. | Rewrote the helper to use the runner's positive probe. |

## Verification results

- Full suite: **529 passed, 0 failed** (see `docs/test-matrix.md`).
- Adversarial harnesses: 31/31, 20/20, 23/23, 8/8 — **82/82 PASS**.
- CLI flow on the 134-file repository: 18/18 steps PASS
  (init, repo, snapshot, index, inspect, lexical/symbol search, symbol show,
  grounded ask, env detect/plan, skills, workspace, memory, events, model, runner).
- Advanced features on the mid repository: 47 multi-language files with symbols,
  semantic reranking, PPR, context manifest (40 candidates), workspace/skills.
- Full task workflow: task created → planned → 2 approvals → worker → REVIEWING →
  verification `COMPLETED_WITH_LIMITATIONS` → finish accepted with the recorded
  limitation. A no-change task was correctly `REJECTED`; a command check was
  correctly `BLOCKED` under `DENY_BY_DEFAULT`.

## Honest scope

Real-model calls, a genuine OCI host, and hosted multi-user deployment remain
unverifiable in this environment and are not claimed. The security fix #2
intentionally blocks execution on hosts that cannot positively prove isolation.

# Test matrix and release test summary

## Declared baseline environment

| Item | Value |
|---|---|
| Date | 2026-11-22 (reconciled after the audit-remediation package) |
| Commit | `git rev-parse HEAD` at test time (work started at `47c78e3`) |
| Python | 3.12.3 (CPython, Linux aarch64) |
| pytest | 9.2.0 |
| pydantic | 2.14.4 |
| pypdf (optional PDF extra) | 5.9.0 |
| Git | available on PATH; real temporary repositories in tests |
| SQLite | interpreter build with WAL + FTS5 |
| Install command | `python -m pip install -e ".[dev]" "pypdf>=4,<6"` |
| Test command | `python -m pytest -q` |

Install the package (do not rely only on `PYTHONPATH=src`): the worker is a
separate OS process and must be able to import `mayaworkbench`. A missing
editable install makes `test_cli_full_flow_uses_a_separate_worker_process`
fail with `ModuleNotFoundError`, which is a setup error, not a product defect.

## Latest full-suite result

```
782 collected, 773 passed, 9 skipped, 0 failed, 0 errors
(python -m pytest -q --junitxml=..., exit 0)
```

The nine skips are explicit: six opt-in live harnesses (`tests/integration/`),
one Playwright-dependent browser test, and two pre-existing optional-dependency
cases. A skip never counts as a pass. A machine-readable release record built from this
JUnit report and the four adversarial harness results is produced by
`maya release evidence --pytest-report <junit.xml> --adversarial-dir <dir> --out <dir>`.

The two skips are the existing optional-dependency cases; a skipped check never
counts as a pass. `python -m compileall -q src` also passes.

Test collection grew from 328 to 635. The 2026-11-22 packages added runtime-workspace,
cgroup, lockfile-environment, API-diff, broken-checker, provider-wiring, native
tool-call/SSE, MCP transport, pluggable-embedding, optional Tree-sitter, four-tier
reproduction-report and offline R2I-evaluation regressions.
Earlier growth included: 6 PDF fallback/parser-policy tests, 8
runner limit/trust-profile tests, 4 execution-capability CLI tests, 8
execution-backend/isolation tests, 5 task-capability grant tests, 6
process-session tests, 4 Engineering Workspace tests, 4 attestation tests,
4 multi-language indexing tests, 3 incremental-indexing tests, 5 experience-memory
tests, 3 context-provenance tests, 8 API control-plane tests, 21 model-platform tests,
5 role-orchestrator tests, 9 mutation-verification tests, 6 runner-bundle tests, 6 typed-claim/discovery tests, 8 advanced-connector tests,
6 skills tests, 6 advanced-tool tests, 5 agent-capability tests, 5 advanced-memory tests, 4 HMAC approval tests,
4 cgroup limiter tests, 4 method-invariant tests, 4 remote-runner tests,
3 evaluation tests, 1 task-scope test, 3 environment tests, 8 session/PTY tests,
7 proof/attestation tests, 3 experiment-adapter tests, 4 OCR tests, 4 semantic
retrieval tests, 3 named-engine tests, 4 remote-backend tests, 1 UI test, 1 positive-network-isolation regression test,
2 api_request tests, and related regression tests. The two
failures observed in the pre-fix baseline are resolved:

- `test_pdf_content_stream_over_budget_is_skipped_with_a_warning` — now passes
  through the controlled pypdf→builtin fallback (MW-P0-01).
- `test_background_child_is_terminated_when_leader_exits` — now passes because
  the unreliable `RLIMIT_NPROC` is no longer applied (MW-P0-02).

`test_process_limit_stops_a_fork_bomb` was replaced by a truthful test: process
count is reported `unsupported`, the supervisor kills the whole process tree,
and no child outlives the command. A test that merely asserted a claimed
`"rlimit"` string cannot detect enforcement failures and was removed.

## Coverage by layer

| Layer | Command | What it proves | CI job |
|---|---|---|---|
| Fast deterministic unit | `pytest -q tests/test_storage.py tests/test_indexing.py tests/test_search.py tests/test_retrieval.py tests/test_claims.py tests/test_mapping.py tests/test_memory.py` | contracts, hashing, retrieval, claims, mapping, memory; no external services | `unit` (short budget) |
| Scale/robustness guards | `pytest -q tests/test_scale_guards.py tests/test_robustness.py` | bounded indexing/parsing, hostile encodings, interrupted output | `unit` |
| Execution trust & limits | `pytest -q tests/test_runner_limits.py tests/test_runner_capabilities.py tests/test_tool_gateway_runner.py tests/test_workspace_policy_patch.py tests/test_worktree.py` | per-limit evidence, trust-profile fail-closed, path policy, process-group cleanup | `execution` (Linux) |
| Integration | `pytest -q tests/test_snapshots.py tests/test_verification.py tests/test_verification_evidence.py tests/test_experiments.py tests/test_experiment_stats.py tests/test_experiment_comparison.py` | real Git worktrees, SQLite, verification, experiment statistics | `integration` |
| Worker lifecycle | `pytest -q tests/test_task_workflow.py tests/test_task_cli.py tests/test_concurrency.py` | leases, approvals, CAS, separate worker process, cancellation, stale settlement | `worker` (allow 120 s) |
| Security / adversarial | `python artifacts/adversarial/harness{1..4}.py` | 82 scripted probes; separately maintained, not folded into unit-test count | `adversarial` (no real provider) |
| Evaluation integrity | `pytest -q tests/test_evaluation_integrity.py tests/test_method_invariants.py tests/test_constraints.py` | frozen-harness hash gate, four extra method probes, formal-constraint plugin boundary | `unit` |
| R2I evidence | `pytest -q tests/test_reproduction.py tests/test_reproduction_integration.py tests/test_evaluation_r2i.py` | four-tier report (adversarially reviewed over three cycles), offline gold-corpus evaluator | `unit` + `integration` |
| Release evidence | `maya release evidence --pytest-report … --adversarial-dir … --out …` | machine-readable record from real inputs; no guessed counts; redaction guard | `release` |
| Workbench UI | `pytest -q tests/test_workbench_ui.py` | four read-only run endpoints plus structured browser panels; DOM-free assertions and a Playwright-gated smoke test | `unit` + `ui` |
| Research evidence quality | `pytest -q tests/test_research_evidence_quality.py` | version-bound citations, typed disagreements, paper status alerts, migration 0011 | `integration` |
| Signed run bundles | `pytest -q tests/test_runner_bundles.py tests/test_attestation.py` | sign/verify/import with named key, verify-time HMAC, swapped-envelope rejection | `unit` |
| Live-model benchmark | `pytest -q tests/test_eval_live.py` | 25-task internal seed corpus, offline dry-run, hard spend cap, evidence-based rates | `unit` |
| Parallel discovery | `pytest -q tests/test_role_orchestrator.py` | opt-in bounded parallel payloads with ordered sequential publication | `unit` |
| Real-model smoke | opt-in, protected job with low-privilege key and cost cap | real structured output and tool-call quality | `model-smoke` (manual approval) |
| Platform | Linux non-root first; Windows/macOS only if advertised | cross-platform behavior | `platform` (future) |

## Release artifact fields

A release summary must record: commit SHA, Python version, dependency lock,
exact test command, collection count, pass/fail/skip counts, and the CI run URL.
Never report "all tests pass" from a partial run. The internal adversarial
audit (`docs/audit-phase-6.md`) remains explicit that it used scripted models,
did not execute experiment runs, did not kill a worker mid-agent, and was not a
penetration test.

## Known unverified areas

- Real-model end-to-end task quality and prompt-injection resistance.
- Worker crash *during each workflow state* with resume/idempotency; DB
  durability under SIGKILL is tested, mid-agent termination is not.
- Non-Linux platforms and hosted multi-user deployment.
- cgroup-backed process/memory/CPU enforcement end-to-end in a task run: the worker
  now opts into a verified cgroup v2 pids scope and routes isolated profiles through
  a populated `RuntimeWorkspace` + `BackendCommandRunner`, but this host has no cgroup
  v2 delegation and no OCI runtime, so those paths are covered only by fake-root and
  fake-OCI unit tests.
- Real host-canary/secret isolation and limit read-back inside an actual rootless
  container. A bounded `MAYA_OCI_SMOKE=1` probe exists and the profile stays unavailable
  until it passes; it has not been run here.
- Native provider adapters (OpenAI SSE + tool calls, Anthropic tool_use, Gemini function
  calls), pluggable embeddings/RRF, optional Tree-sitter parsing and MCP stdio transport
  are implemented and covered with fake transports; no live provider, OCI runtime or
  third-party MCP server was exercised. See `IMPLEMENTATION_STATUS.md`.

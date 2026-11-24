# Audit remediation — 23 November 2026

**Source:** `MayaWorkbench_Audit_and_Engineering_Upgrade_Roadmap_2026-11-23.md`
**Package:** WP0 (execution trust and lifecycle closure) + WP1 (deterministic
test and release evidence) + the tractable P1 items.
**Release name:** Trustworthy Runtime + Verification.
**Decision record:** [ADR 0015](../adr/0015-trustworthy-runtime-and-verification-release.md).

Status vocabulary (from the audit, §11):

- `IMPLEMENTED` — source implementation exists.
- `UNIT_TESTED` — deterministic tests cover the component.
- `INTEGRATION_TESTED` — real local dependencies/transport are exercised.
- `LIVE_VERIFIED` — the real OCI/provider/connector gate passed.
- `EXTERNALLY_EVALUATED` — independent held-out or public benchmark evidence.

A component can hold several of these. Nothing in this page is `LIVE_VERIFIED`
for the container profile, and nothing is `EXTERNALLY_EVALUATED`.

---

## P0 — release/trust blockers

### MW-NEW-P0-01 — Secret exposure through `/src`

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- New `src/mayaworkbench/workspace/sanitize.py` is the single canonical filter:
  excluded directories, host credential stores, credential-shaped paths,
  credential-shaped content, symlinks and unreadable entries.
- `RootlessContainerBackend.provision()` now materializes **two** task-owned
  trees: an immutable sanitized snapshot (`0o400` files, `0o500` directories)
  and a writable overlay built from it. It asserts their manifests are identical
  before returning.
- `_oci_argv()` emits exactly one bind mount (`/work`). The `/src` mount of the
  original worktree is gone; the probe confirms `/src/.env` is `absent`.
- `RuntimeWorkspace.for_container()` compares the backend's sanitized manifest
  with the overlay manifest and refuses a partial workspace.
- Policy is versioned (`maya-sanitize-v3`) and hashed
  (`sanitize_policy_sha256`) into `runtime_binding`.

**Evidence:** `tests/test_execution_backends.py::
test_provision_sanitizes_the_source_and_never_exposes_secrets`,
`::test_sanitized_snapshot_is_immutable`, `::test_oci_argv_mounts_only_the_sanitized_overlay`;
`tests/test_runtime_workspace.py::test_populate_overlay_is_clean_and_content_addressed`.

**Remaining:** a live host canary read from the real kernel namespace is only
proven by the opt-in OCI harness, which has not run here.

### MW-NEW-P0-02 — Capability probe overstates verification

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- `RootlessContainerBackend.verify_capabilities()` launches a task-equivalent
  container (same `_oci_argv`, same policy) and reads back, from inside it:
  `pids.max`, `memory.max`, `cpu.max`, `RLIMIT_CPU/FSIZE/NOFILE`, egress denial,
  root-filesystem read-onlyness, `/work/.env`, `/src/.env` and a host canary.
- It also performs bounded active tests: a fork storm against `pids.max` and a
  CPU-time termination when the budget is small enough to reach.
- `capabilities()` derives every status from that evidence. `enforced` requires
  `observed == requested`; otherwise that limit alone is `not_applied`.
- The caller-asserted `verified=True` constructor argument was **removed**.
- `Cargo`-style per-limit evidence now carries `requested` and `observed`.
- The worker verifies the *contract's own* policy before provisioning, and stores
  the evidence in `runtime_binding.capability_evidence`.

**Evidence:** `tests/test_execution_backends.py::
test_live_probe_marks_every_mandatory_limit_enforced`,
`::test_probe_that_observes_a_different_value_is_not_enforced`,
`::test_backend_without_probe_evidence_claims_nothing`,
`::test_evidence_round_trips_through_json_for_durable_bindings`.

**Remaining:** a real rootless kernel readback (`LIVE_VERIFIED`) is still gated by
`MAYA_OCI_LIVE`/`MAYA_OCI_SMOKE` on a host with podman and cgroup v2 delegation.

### MW-NEW-P0-03 — In-loop checks did not receive the task runtime

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- `DeclaredCheckRunner` accepts `runtime` and passes it to
  `verification.execution.execute_check`.
- With an isolated trust profile and no runtime it returns `BLOCKED` with a typed
  reason; it never constructs a host runner.
- `worker._execute` passes the exact `CommandRunnerTaskRuntime` it gave the
  gateway.
- Invariant scripts go through `runtime.exec_script`; the container adapter
  writes the generated script into `.maya-scripts/` (excluded from the candidate
  diff) and executes it inside the container.

**Evidence:** `tests/test_isolation_contract.py::
test_in_loop_check_is_blocked_without_the_task_runtime`,
`::test_in_loop_check_uses_the_injected_runtime`.

### MW-NEW-P0-04 — Container sessions used host `Popen`

**Status:** `IMPLEMENTED`, `UNIT_TESTED` (container-owned path included);
`LIVE_VERIFIED` pending the OCI gate.

- `workspace/sessions.py` now defines a `SessionBackend` protocol with
  `LocalProcessSessionBackend` (host, `LOCAL_TRUSTED` only) and
  `UnavailableSessionBackend` (typed refusal, never a fallback).
- The worker injects `UnavailableSessionBackend` for `ROOTLESS_CONTAINER`.
- ToolGateway checks `manager.capability` and returns a `BLOCKED` tool result with
  the reason.
- The PTY framing contract was also fixed here (see P1-02).

**Evidence:** `tests/test_sessions.py::
test_isolated_runtime_refuses_sessions_instead_of_using_the_host`,
`::test_gateway_reports_unavailable_sessions_as_a_typed_refusal`.

**Container-owned sessions (added in the second pass).**

- `ContainerSessionBackend` (`workspace/container_sessions.py`) executes every
  session command with the OCI exec API inside a **retained** container started
  with the same `_hardening_flags` as a task container: network `none`,
  `--read-only`, `--cap-drop ALL`, `no-new-privileges`, the same pids/memory/cpu/
  ulimit policy and the same single sanitized `/work` mount.
- The host runs only the client; the user's command is never a host process.
- Each exec carries `MAYA_SESSION_ID=<id>`; a generated `/bin/sh` terminator
  inside the container signals only processes with that marker (TERM, then KILL
  after a bounded grace period, PID 1 skipped). The script lives in
  `.maya-sessions/`, excluded from manifests by sanitizer `v3`.
- `SessionManager` now delegates `build`/`spawn`/`terminate` to the backend, so
  the id exists before spawn and termination is per-session.
- An unstartable or dead retained container flips the capability to unavailable
  with the observed reason; PTY sessions are refused inside a container because a
  container-side TTY cannot be verified here.

**Evidence:** `tests/test_container_sessions.py` (7 tests, real argv assertions
against the fake OCI client).

**Remaining:** the live `podman exec` behaviour (signal delivery, `/proc` marker
scan, cleanup after a client kill) is only proven by the opt-in OCI harness,
which has not run here.

### MW-NEW-P0-05 — Baseline was not executed in an equivalent runtime

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- `verification/service.py::_baseline_runtime()` provisions a clean baseline in a
  second isolated runtime and verifies parity of image, mount policy, resource
  policy, sanitizer policy and the pinned source manifest.
- `_declared_checks` sends the baseline check through that runtime.
- An isolated candidate with no matched baseline runtime, or a parity mismatch,
  produces `BLOCKED` for the check; it can never be `PASSED`.
- Parity evidence is copied into the check details and the attempt details.

**Evidence:** `tests/test_isolation_contract.py::
test_baseline_runtime_reports_matched_parity`,
`::test_baseline_runtime_refuses_a_different_mount_policy`.

---

## P1 — core-engineering gaps

### MW-NEW-P1-01 — `cpu_seconds` conflated CPU quota with CPU time

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- `CommandPolicy.cpu_time_seconds` (cumulative CPU seconds, RLIMIT_CPU) and
  `CommandPolicy.cpu_quota` (cores, cgroup `cpu.max`) are separate fields.
- Limit names are `cpu_time` and `cpu_quota`; the OCI backend passes
  `--ulimit cpu=…` for the former and `--cpus …` for the latter, and reports
  each with its own readback.
- The host runner reports `cpu_quota: unsupported` rather than borrowing the
  rlimit result.

**Evidence:** `tests/test_runner_limits.py::test_cpu_limit_terminates_a_cpu_spin`,
`tests/test_execution_backends.py::test_oci_argv_mounts_only_the_sanitized_overlay`.

### MW-NEW-P1-02 — Session event framing was implicit

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- The module documents the contract: output is an arbitrary chunk stream,
  monotonically sequenced, replayable, with truncation as an event. There is no
  one-event-per-line promise.
- `SessionManager.stream_text()` is the supported accumulation helper.
- The flaky PTY and bounded-output tests now accumulate ordered chunks.

**Evidence:** `tests/test_sessions.py` (PTY and bounded-output cases).

### MW-NEW-P1-03 — Optional-dependency tests were not hermetic

**Status:** `IMPLEMENTED`, `UNIT_TESTED`.

- The “torch is absent” cases install a `sitecustomize.py` meta-path blocker via
  `PYTHONPATH`, so the import fails deterministically even when the interpreter
  has PyTorch installed globally.
- A temporary working directory is no longer mistaken for dependency absence.

**Evidence:** `tests/test_method_invariants.py` (three SKIP cases).

### MW-NEW-P1-04 — Test-matrix document was inconsistent

**Status:** `IMPLEMENTED` for the document; **CI wiring is not** (no CI runner in
this workspace).

- `docs/test-matrix.md` now cites a real run: 834 collected, 827 passed, 7 skipped, 0 failed, 85.5 s, with the
  JUnit report and a JSON summary committed under `docs/reports/`.
- The skip list is enumerated from the report (3 OCI live, 3 model live, 1
  Playwright). The contradictory “two skips” paragraph and the stale
  “328 → 635” history were replaced.
- `python -m pytest -q` completes in 80–91 s across repeated runs here, well
  inside a 10-minute budget; the audit's 120 s timeout was an environment
  constraint, not a product defect.

**Remaining:** a pinned CI job that fails on a missing/cancelled JUnit report.
The repository has no CI configuration in this workspace, so that cannot be
implemented and verified here.

### MW-NEW-P1-09 — Local API needed a strict security contract

**Status:** `IMPLEMENTED`, `UNIT_TESTED` for the request contract; **authentication
is deliberately out of scope**.

- `guard_local_request` enforces a loopback `Host` (DNS-rebinding defence), no
  cross-site `Sec-Fetch-Site`, and no non-loopback `Origin`/`Referer` on
  mutations. Non-browser local clients keep working.
- Responses carry `Cache-Control: no-store`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`.
- `create_server` refuses a non-loopback bind unless `MAYA_API_ALLOW_REMOTE=1`.
- Event/timeline projections already whitelist payload keys, so approval nonces
  and tool arguments do not reach the read stream.

**Evidence:** `tests/test_api_local_security.py` (7 tests).

**Remaining:** per-session CSRF nonces, rate limits, bounded event retention and
any multi-user permission model. A remote bind is still unauthenticated, which is
why it needs an explicit opt-in.

---

## Additional defect found by running the suite

`discover_cgroup_root()` let `PermissionError` escape when a delegated cgroup path
existed but was not readable (as in this sandbox:
`/sys/fs/cgroup/freebuff/<id>` is root-only). That crashed `runner doctor`,
`env doctor`, `maya env …` and every `LOCAL_TRUSTED` command, because the runner
treats cgroup discovery as an optional optimisation. Discovery now fails closed
(`OSError` → “no usable delegation”) and `process_count` stays `unsupported`
instead of the process crashing.

This was one of the seven pre-existing failures on a clean checkout; the other
six were consequences of the same crash.

---

## Second pass — WP2 sessions and P1-05 provisioning

### Container-owned sessions (closes the P0-04 remainder)

**Status:** `IMPLEMENTED`, `UNIT_TESTED`; `LIVE_VERIFIED` pending.

- `SessionBackend` gained `build`/`spawn`/`terminate`. The manager no longer
  creates a process itself, so a runtime can own the translation and the
  termination.
- `ContainerSessionBackend` starts one retained container per task
  (`run --detach`, shared `_hardening_flags`) and runs every session command with
  the OCI exec API inside it. The host executes only the client.
- Per-session termination: `MAYA_SESSION_ID=<id>` in the exec environment plus a
  generated `/bin/sh` scanner that TERMs then KILLs only matching processes and
  never PID 1. The script is excluded from manifests (`.maya-sessions`).
- `RootlessContainerBackend.stop` stops and removes the retained container before
  deleting the overlay, so no exec runs against a removed mount.
- Fail-closed paths: unstartable container, container no longer running, PTY
  requested. Each produces a typed `PolicyError` and, where applicable, flips the
  capability to `available=False` with the observed reason.

**Evidence:** `tests/test_container_sessions.py` — exact argv assertions for the
retained container, single `/work` mount, per-session marker, kill-script target,
runtime-stop cleanup, unstartable/dead container refusals and PTY refusal.

**Remaining:** live signal delivery and `/proc` marker scanning in a real
container, covered by the opt-in OCI gate only.

### Environment provisioning (P1-05)

**Status:** `PARTIAL` — spec, resolver, provisioner, CLI and evidence are
implemented; image builds, package/image cache, service lifecycle and the live
fixture are not.

- `EnvironmentSpec` records detected ecosystems, package managers, **hashes of
  the lockfiles that exist**, `lockfile_absent` for the ecosystems where none
  exists, ordered argv steps, the pinned image/digest, declared services, secret
  references (names only), hardware, detection confidence and the plan digest.
  `spec_sha256` is deterministic and changes when a lockfile byte changes.
- `EnvironmentProvisioner` executes the spec through an injected runtime and
  refuses `LOCAL_TRUSTED` in code. Network steps are `BLOCKED` without explicit
  approval; a failure stops the run and marks the rest `SKIPPED`; every step
  records argv, exit code, duration and a log hash; inventory is read back
  (`pip list --format=json`, `npm ls --json`, `cargo metadata`) and an unreadable
  inventory is `unavailable`, never an empty list.
- Services are declared and reported `not_started` with the concrete reason; the
  provisioner never claims a service is running.
- CLI: `maya env spec` (resolved spec) and `maya env provision`
  (`--execute --allow-network` required to install; refuses non-rootless).

**Evidence:** `tests/test_environment_provisioning.py` — determinism, lockfile
pinning, secret references, host refusal, network gate, ordered execution, failure
short-circuit, inventory truthfulness, declared services and the CLI surfaces.

**Remaining:** project image builds, a content-addressed package/image cache with
SBOM/licence metadata, service orchestration with local port publishing, and the
Python/Node end-to-end fixture. All require a rootless OCI host.

---

## Model/provider hardening (follow-up pass)

Status: `IMPLEMENTED`, `UNIT_TESTED`; `LIVE_VERIFIED` pending a provider key.

This pass fixed provider-contract defects that only appear against a real API:

- **OpenAI-compatible.** Strict JSON Schema is projected (closed objects, all
  properties required) before it is sent; the adapter degrades
  `json_schema` → `json_object` → prompt-only. It starts with `max_tokens` and
  switches to `max_completion_tokens` when a reasoning model rejects the former,
  drops `temperature`/`parallel_tool_calls`/`stream_options` only after an
  observed rejection, requests `stream_options.include_usage`, and maps
  `context_length_exceeded` and `insufficient_quota` to non-retryable typed errors.
- **Anthropic.** JSON output uses the GA `output_config.format` field (not the
  deprecated `output_format`/beta header). Cache breakpoints are applied to the
  requested message blocks (max four), consecutive `tool_result` blocks are
  merged into one user turn, and `stop_reason: refusal` is a typed non-answer.
- **Gemini.** Structured output sends `responseMimeType: application/json` plus a
  projected OpenAPI-subset `responseSchema`; `functionResponse.name` is resolved
  from the assistant's earlier `functionCall`; `promptFeedback.blockReason` and
  blocked finish reasons are typed `content_filter` errors; `thoughtsTokenCount`
  and `cachedContentTokenCount` are accounted for.
- **Streaming.** All three adapters implement the incremental `stream()`
  protocol and buffered `generate(stream=True)`. SSE framing, byte/event/line
  limits and bounded `Retry-After` live in `models/sse.py` and are shared.
- **Gateway.** It resolves the provider-specific key variable, allows a keyless
  loopback endpoint under `LOCAL_ONLY`, counts tool schemas in its input bound,
  records the attempts that actually happened instead of the configured ceiling,
  and stores only safe status/code details on failure.
- **Live harness.** It no longer requires `MAYA_MODEL_API_KEY` when the configured
  adapter reads a different variable, exercises the streaming transport when
  declared, and still makes at most two tiny calls under a hard spend cap.

Evidence: `tests/test_provider_adapters.py` (20 tests) plus updates to
`tests/test_model_platform.py`; `docs/adr/0017-model-provider-hardening.md`.
Remaining: a live call against each configured provider. Until then no provider
quality, tool-call reliability or streaming-latency claim is licensed.

## Not implemented in this package (and why)

| Audit item | Status | Concrete blocker |
|---|---|---|
| P1-05 Environment provisioning (`EnvironmentSpec/Resolver/Provisioner`) | `PARTIAL` | Spec, resolver, provisioner, CLI and evidence are implemented and tested (`tests/test_environment_provisioning.py`). Missing: image builds from project recipes, a content-addressed package/image cache with SBOM/licence metadata, service lifecycle with local port publishing, and the live Python/Node fixture — all blocked on a rootless OCI host. |
| P1-06 Real-model task evaluation | `NOT_STARTED` `LIVE_VERIFIED` pending | The harness and 25-task corpus exist, and the provider adapters are now wire-tested for both streaming and non-streaming structured output. A live run still needs provider credentials and a spend cap; no key exists here. |
| P1-07 Held-out expert-labelled R2I corpus | `NOT_STARTED` | Needs ≥2 human annotators and adjudication. Not producible by code. |
| P1-08 Multi-language gold-set validation | `NOT_STARTED` | Needs fixture repositories and manual labels per language. |
| WP2 Persistent container sessions / service orchestration | `PARTIAL` | Container-owned sessions are implemented and unit-tested; service orchestration (start DB/API, publish local ports, health checks) is not implemented and is reported `not_started` by the provisioner. |
| WP3 Live model/provider evaluation | `PARTIAL` | Adapter correctness, streaming, structured output, cache/tool semantics and typed failures are implemented and unit-tested with fake transports. `LIVE_VERIFIED` is still pending a provider key; no quality claim is made. |
| WP4 R2I evidence UI and held-out benchmark | `NOT_STARTED` | Same as P1-07. |
| WP5 Build/dependency graph, impact analysis | `NOT_STARTED` | Large parser/LSP programme; not attempted partially. |
| WP6 Connector expansion | `PARTIAL` | Existing connectors unchanged; no new connector was added without adversarial tests. |
| WP7 Polished workbench UI + usability study | `NOT_STARTED` | Needs 5–8 target engineers; the local UI is unchanged. |

The audit's recommendation — “do not expand the agent swarm or add integrations
until the runtime is trustworthy” — is respected: no role, connector or UI
surface was added in this package.

---

## Verification actually performed

| Check | Result |
|---|---|
| `python -m compileall -q src` | passes |
| `python -m pytest -q --junitxml=docs/reports/pytest-2026-11-23.xml` | 834 collected, 827 passed, 7 skipped, 0 failed, 0 errors, 85.5 s |
| `docs/reports/test-run-2026-11-23.json` | generated from the JUnit report; commit, Python, platform, counts, duration |
| Live OCI harness | **not run** (`MAYA_OCI_LIVE` unset, no rootless OCI runtime) |
| Live model harness | **not run** (`MAYA_LIVE_MODEL` unset, no key) |
| Adversarial harnesses | not re-run in this package; they are unchanged |

The 7 skips are exactly the opt-in live harnesses and one Playwright test. No
skip is reported as a pass.

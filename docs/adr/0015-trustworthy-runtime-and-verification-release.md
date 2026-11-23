# ADR 0015 — Trustworthy Runtime + Verification release

- **Status:** accepted
- **Date:** 2026-11-23
- **Supersedes:** the execution-isolation parts of ADR 0012 and the 2026-11-21
  "read-only `/src` mount" decision.
- **Context document:** `MayaWorkbench_Audit_and_Engineering_Upgrade_Roadmap_2026-11-23.md`

## Context

The 23 November audit found that the product's evidence promise was undermined by
its execution substrate. A task could be isolated in one part of the lifecycle and
leave the isolation boundary in another:

1. The user's worktree was still bind-mounted read-only at `/src` while the
   filtered overlay was mounted at `/work`. A path excluded from `/work` was
   readable at `/src`.
2. The capability probe started a container and inferred that every resource
   limit was enforced, without reading a single effective value back.
3. In-loop declared checks were constructed without the task runtime, so an
   isolated task's checks fell back to a host runner (which then refused, or
   worse, would have executed on the host).
4. Terminal sessions used host `subprocess.Popen` regardless of trust profile.
5. Baseline checks ran on the host while candidate checks ran in a container, so
   a baseline/candidate comparison was not a comparison.

Separately, `cpu_seconds` was documented as a cumulative CPU-time budget but
mapped to `--cpus` (a CPU-core quota) in the OCI backend.

## Decision

**Preserve the architecture. Close the lifecycle. Refuse rather than downgrade.**

### 1. One canonical sanitizer, no worktree mount

`workspace/sanitize.py` is the single filter. A task receives:

- an immutable sanitized snapshot (owner-read-only files, `0o500` directories);
- a writable overlay populated from that snapshot;
- exactly one bind mount: the overlay at `/work`.

The user's worktree is never mounted. The filter excludes `.git` internals, host
credential stores (`.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`), credential-shaped
paths, credential-shaped *content*, symlinks and unreadable entries. The policy is
versioned (`maya-sanitize-v3`) and hashed into the run's evidence.

Rationale: one implementation means the overlay, the reference snapshot and the
evidence manifest cannot drift. A second, loosely synchronised filter is how the
original defect appeared.

### 2. Capability is evidence, not configuration

`RootlessContainerBackend.verify_capabilities()` starts a task-equivalent
container with the same flags as a task and reads back, from inside it:

- `pids.max`, `memory.max`, `cpu.max`;
- `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`;
- egress denial, root-filesystem read-onlyness, canary/secret unreadability;
- an active fork-storm and a CPU-time termination test where cheap.

A limit is `enforced` only when `observed == requested`. The caller can no longer
assert `verified=True`; there is no such parameter. If one control is ignored, only
that control becomes `not_applied` and the profile stays unavailable.

### 3. One runtime injected into every side-effecting component

`DeclaredCheckRunner` receives the task runtime. With an isolated trust profile and
no runtime it records `BLOCKED`; it never builds a host runner. Sessions are created
by an injected `SessionBackend`; the rootless worker injects
`UnavailableSessionBackend`, so `open_session` returns a typed capability refusal.

### 4. Baseline parity is a precondition for comparison

`verification/service.py::_baseline_runtime` provisions a clean baseline in a
second isolated runtime and records the image, mount policy, resource policy,
sanitizer policy and pinned-manifest hashes of both sides. A mismatch, or an
unavailable baseline runtime, makes the declared check `BLOCKED`.

### 5. CPU time and CPU quota are separate controls

`CommandPolicy.cpu_time_seconds` (cumulative CPU seconds, RLIMIT_CPU) and
`CommandPolicy.cpu_quota` (cores, cgroup `cpu.max`) are distinct fields, distinct
limit names (`cpu_time`, `cpu_quota`) and distinct evidence. Neither is ever
reported as the other.

### 6. The local API has a request contract

Loopback-only `Host` check (DNS-rebinding defence), same-origin/cross-site refusal
for mutations, `no-store`/`no-frame`/`no-referrer` headers, and a non-loopback bind
refused unless `MAYA_API_ALLOW_REMOTE=1`. There is still no user authentication, so
a remote bind remains out of scope for this release.

## Consequences

- The isolated profile is **experimental** until the live OCI gate passes on a
  real rootless host. That gate is explicitly required by `docs/release-gates.md`
  and has not been run in this environment.
- Container-owned persistent terminals were not implemented in this package. They
  are implemented in [ADR 0016](0016-container-owned-sessions-and-environment-provisioning.md)
  (retained container, per-session marker, generated terminator); this ADR's
  fail-closed session contract still holds for any runtime that cannot own one.
- Sessions, in-loop checks and verification share one runtime, which makes
  "the tree the tools edited is the tree the checks measured" true by
  construction rather than by convention.
- The sanitized reference is immutable, so a task cannot mutate the tree it is
  being diffed against.
- A task that cannot be isolated is blocked, which is a deliberate availability
  cost.

## Product-freeze reconciliation

`docs/00-product-freeze.md` froze the Phase 0–5 scope as CLI-first with no local
UI, labelled corpora or evaluation scoring. Later work (v2.1) added a local
API/UI, an internal R2I seed corpus, an offline evaluation flow and opt-in live
harnesses. This ADR records that those additions are **accepted and in scope**,
while the following remain out of scope and are not claimed:

- multi-user authentication, tenancy or hosted operation;
- a public API bind without an explicit operator opt-in;
- distributed training, GPU scheduling or a remote runner mesh;
- a signed-proof or HMAC-approval claim beyond the separately named
  implementations in `IMPLEMENTATION_STATUS.md`;
- any container-isolation claim before the live OCI gate passes.

## Evidence

| Decision | Tests |
|---|---|
| Sanitized snapshot, single mount, immutability | `tests/test_execution_backends.py`, `tests/test_runtime_workspace.py` |
| Evidence-based limits, ignored flag is `not_applied` | `tests/test_execution_backends.py` |
| In-loop checks use the injected runtime / blocked without it | `tests/test_isolation_contract.py` |
| Sessions fail closed for isolated runtimes | `tests/test_sessions.py` |
| Baseline parity | `tests/test_isolation_contract.py` |
| CPU-time vs CPU-quota split | `tests/test_runner_limits.py`, `tests/test_execution_backends.py` |
| Local API request contract | `tests/test_api_local_security.py` |
| Hermetic optional-dependency tests | `tests/test_method_invariants.py` |
| Full suite | `docs/reports/test-run-2026-11-23.json` (later provider-hardening run: 834 collected, 827 passed, 7 skipped, 0 failed) |

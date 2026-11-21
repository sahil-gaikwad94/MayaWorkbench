# ADR 0012 — Execution trust profiles and per-limit enforcement evidence

- Status: Accepted
- Date: 2026-11-21
- Applies to: `workspace/runner.py`, `workspace/capabilities.py`, `workspace/backends.py`,
  `domain/enums.py`, `worker.py`

## Context

MW-P0-02 and MW-P0-03 of the Part 2 audit showed two failures in one mechanism:

1. `RLIMIT_NPROC` is a per-UID ceiling that Linux does not enforce for privileged
   processes, so a fork bomb created 200 children with a configured limit of 8.
2. The same per-UID headroom calculation could reject a *legitimate* child fork with
   `EAGAIN`, breaking `test_background_child_is_terminated_when_leader_exits`.
3. `limits_enforcement` was the static string `"rlimit"` on every launched command,
   whether or not any `setrlimit` call succeeded.

A Git worktree protects the source branch but not the host filesystem, so the product
story also needed to stop implying process isolation.

## Decision

- Remove `RLIMIT_NPROC` entirely. Report `process_count` as `unsupported` until an
  isolated backend with a verified cgroup/container primitive exists.
- Apply CPU, address-space, file-size and open-file limits inside the forked child and
  read them back with `getrlimit`; write the observed result to a private evidence file
  that the parent reads. `limits_enforcement` is now derived from per-limit evidence
  (`all_enforced` / `partially_enforced` / `not_enforced` / `not_applied`).
- Introduce `ExecutionTrust`: only `LOCAL_TRUSTED` executes on the host, explicitly
  labelled "NOT a sandbox". `READ_ONLY_ANALYSIS` refuses commands; isolated profiles
  are blocked rather than downgraded to host privileges.
- `maya runner doctor|profiles` exposes the observed host capabilities in human and JSON
  form; `RootlessContainerBackend` is fail-closed on missing OCI runtime or cgroup
  controllers and never mounts home, Docker socket, SSH agent or ambient secrets.

## Consequences

- The fork-bomb regression test was rewritten to assert the truthful contract
  (unsupported PID limit, supervisor kill, no surviving descendants), not a claimed
  status string.
- Two previously failing tests now pass; the full suite is green.
- A real host-canary E2E inside a container remains unverified on hosts without cgroup v2
  and an OCI runtime. This is recorded in `IMPLEMENTATION_STATUS.md`.

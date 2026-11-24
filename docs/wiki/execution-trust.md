# Execution trust decisions (dated)

- **2026-11-21:** `RLIMIT_NPROC` was removed from `workspace/runner.py`. Reason: on this
  Linux host it is a per-UID ceiling not enforced for privileged processes (audit observed
  200 children with a limit of 8), and its headroom calculation could fail a legitimate
  `fork()` with `EAGAIN` (this caused `test_background_child_is_terminated_when_leader_exits`
  to fail). Evidence: `tests/test_runner_limits.py`.
- **2026-11-21:** Resource limits are now reported per limit with the mechanism observed by
  a `getrlimit` readback inside the child; `limits_enforcement` is derived
  (`all_enforced`/`partially_enforced`/`not_enforced`/`not_applied`). Process count is
  reported `unsupported` until a cgroup/container backend is verified.
- **2026-11-21:** `ExecutionTrust` profiles: only `LOCAL_TRUSTED` executes on the host;
  `READ_ONLY_ANALYSIS` refuses commands; `ROOTLESS_CONTAINER`, `REMOTE_MICROVM` and
  `GPU_RUNNER` are blocked rather than downgraded. `maya runner profiles` shows this.
- **2026-11-21:** `RootlessContainerBackend` mounts source read-only at `/src` and a distinct
  overlay writable at `/work`, drops all capabilities, sets no-new-privileges, network `none`,
  cgroup `pids.max`/memory/cpu, and never mounts home, Docker socket, SSH agent or ambient
  secrets. Verified with a fake OCI client that asserts the exact argv
  (`tests/test_execution_backends.py`). A real-container E2E host-canary test remains
  outstanding because this environment has no cgroup v2 controllers or OCI runtime.
- **2026-11-22 (supersedes the "cgroup limits are future" note):** the worker opts into a
  cgroup v2 pids scope when a *writable, read-back-verified* delegation is discovered from
  `/proc/self/cgroup`; a `max` readback is rejected as a false green, cleanup drains live pids
  and uses `rmdir` on cgroupfs, and an unverifiable scope leaves `process_count: unsupported`.
  Evidence: `tests/test_cgroup_limits.py`.
- **2026-11-22:** controller names no longer produce `enforced` claims. The one canonical
  `ExecutionCapabilityReport` reports presence separately from applied/read-back limits, and
  `ROOTLESS_CONTAINER` is available only after a real rootless OCI runtime, cgroup delegation
  and a bounded smoke test pass. `READ_ONLY_ANALYSIS` became available with a code-level
  read-only tool mask. Evidence: `tests/test_runner_capabilities.py`,
  `tests/test_execution_backends.py`.
- **2026-11-22:** a single `RuntimeWorkspace` now owns the task file view. For an isolated
  profile its overlay is populated from the pinned snapshot, mounted at container `/work`, and
  used by file tools, commands, in-loop checks and artifact/diff capture. The retained overlay
  is reused by verification for candidate checks; baseline checks stay in a separate clean
  tree. Real kernel escape tests still require an OCI host. Evidence:
  `tests/test_runtime_workspace.py`.
- **2026-11-23 (supersedes the read-only `/src` mount decision):** the original user
  worktree is NEVER bind-mounted into an isolated task. A task-owned, immutable,
  secret-free sanitized snapshot (0444 files, 0555 directories) is produced by the same
  canonical filter as the writable overlay and mounted at `/src`; the policy fingerprint
  binds the manifest to the filter version. Read-only was not secret isolation: a task
  could previously read `/src/.env` and `.git/config` from the original worktree. Evidence:
  `tests/test_runtime_workspace.py`, `tests/test_execution_backends.py`, and the live
  canary probes in `tests/integration/test_container_escape_live.py`.
- **2026-11-23:** capability evidence comes from a live in-container probe built on the
  production backend. It reads effective `pids.max`, `memory.max` and `cpu.max`, tests
  network denial with a host listener, tests secret denial on every mount, checks mount
  visibility and verifies teardown. A passing smoke marker or a controller name cannot
  make a limit `enforced`; an unverified limit is `not_applied`. Evidence:
  `tests/test_runner_capabilities.py`, `tests/test_execution_backends.py`.
- **2026-11-23:** one `TaskExecutionRuntime` reaches in-loop command and invariant checks.
  An isolated profile with no injected runtime returns typed `BLOCKED` rather than building
  a host runner. Terminal sessions use a `ProcessSessionBackend`: host `Popen` only for
  explicitly trusted profiles; an OCI task gets a keepalive container and `podman exec`.
  PTY is refused with a typed capability error in the isolated profile. Evidence:
  `tests/test_agent_intelligence.py`, `tests/test_session_backends.py`.
- **2026-11-23:** baseline and candidate checks use equivalent isolated runtimes. A second
  container is provisioned with identical image, policy, limit evidence and mount-policy
  hash; only the source tree differs. A missing or mismatched baseline runtime produces
  `BASELINE_PARITY_MISSING` and caps the check at `INCONCLUSIVE` — never a host fallback and
  never a silent skip. Evidence: `tests/test_isolated_baseline.py`.
- **2026-11-23:** CPU *time* (RLIMIT_CPU), CPU *quota* (`--cpus`) and wall time are separate
  policy and evidence fields. `cpu_seconds` is a legacy alias only. Evidence:
  `tests/test_runner_limits.py`.

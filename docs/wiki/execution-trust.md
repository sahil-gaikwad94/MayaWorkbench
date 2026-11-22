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

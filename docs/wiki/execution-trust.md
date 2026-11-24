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
- **2026-11-21 (superseded 2026-11-23):** `RootlessContainerBackend` mounted the source
  read-only at `/src` and a distinct overlay writable at `/work`. Read-only is not secret
  isolation: the user's worktree was still reachable at `/src`, which is the MW-NEW-P0-01
  defect. That mount is **removed** in the 2026-11-23 entry below.
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
- **2026-11-22 (partly superseded 2026-11-23):** a single `RuntimeWorkspace` owns the task
  file view, and its retained overlay is reused by verification for candidate checks. The
  in-loop check wiring and the baseline half of that claim were not implemented at this
  point; both are fixed below.
- **2026-11-23 — one canonical sanitizer (MW-NEW-P0-01).** `workspace/sanitize.py` is now the
  only filter. A task gets an immutable sanitized snapshot (`0o400`/`0o500`) and a writable
  overlay built from it; the user's worktree is never mounted. Excluded: `.git` internals,
  host credential stores, credential-shaped paths, credential-shaped *content*, symlinks, and
  unreadable entries. The policy is versioned (`maya-sanitize-v3`) and hashed into evidence.
  Evidence: `tests/test_execution_backends.py` (`.env`, `.npmrc`, a secret-shaped `notes.txt`,
  a symlink to a host secret, an immutable snapshot), `tests/test_runtime_workspace.py`.
- **2026-11-23 — capability claims require a live in-container probe (MW-NEW-P0-02).**
  `verify_capabilities()` launches a task-equivalent container with the same flags a task uses
  and reads `pids.max`, `memory.max`, `cpu.max` and the effective `RLIMIT_CPU`/`FSIZE`/`NOFILE`
  back **from inside it**, plus egress denial, root-filesystem read-onlyness, canary
  unreadability and an active fork-storm/CPU-time test. A limit is `enforced` only when the
  observed value equals the requested value; otherwise only that limit is
  `not_applied`/`unsupported` and the profile stays unavailable. The constructor no longer
  accepts a caller-asserted `verified=True`. Evidence: `tests/test_execution_backends.py`
  (including a probe that silently reports a different `memory.max`).
- **2026-11-23 — in-loop checks use the injected runtime (MW-NEW-P0-03).**
  `DeclaredCheckRunner` takes the task runtime and passes it to `execute_check`; with an
  isolated trust profile and no runtime it records `BLOCKED` instead of building a host
  runner. Evidence: `tests/test_isolation_contract.py`.
- **2026-11-23 — container-owned sessions (MW-NEW-P0-04).** Sessions are created by an
  injected `SessionBackend` that owns `build`/`spawn`/`terminate`. `LOCAL_TRUSTED` keeps the
  host-process backend. The rootless worker injects `ContainerSessionBackend`, which starts
  one retained container with the *same* hardening flags and the same single sanitized
  `/work` mount as a task container and runs every session command through the OCI exec API;
  the host executes only the client. Each exec carries `MAYA_SESSION_ID=<id>` and a generated
  in-container script TERMs/KILLs only processes with that marker (PID 1 skipped). PTY
  sessions, an unstartable container and a dead container are typed refusals, never host
  fallbacks. Evidence: `tests/test_container_sessions.py`, `tests/test_sessions.py`.
- **2026-11-23 — baseline/candidate parity (MW-NEW-P0-05).** An isolated candidate is compared
  with a clean baseline provisioned under the same image, mount policy, resource policy and
  sanitizer policy. Parity is recorded per attempt and a mismatch, or an unavailable baseline
  runtime, makes the declared check `BLOCKED` — never a pass. Evidence:
  `tests/test_isolation_contract.py`, `tests/test_isolation_contract.py::test_baseline_runtime_*`.
- **2026-11-23 — CPU-time and CPU-quota are separate controls (MW-NEW-P1-01).**
  `CommandPolicy.cpu_time_seconds` (cumulative CPU seconds, RLIMIT_CPU) and `cpu_quota`
  (cores, cgroup `cpu.max`) are distinct fields and distinct limit names; the OCI backend no
  longer maps a CPU-time budget onto `--cpus`. Evidence: `tests/test_runner_limits.py`,
  `tests/test_execution_backends.py`.
- **2026-11-23 — the local API has a tested request contract (MW-NEW-P1-09).** Loopback-only
  Host check (DNS-rebinding defence), same-origin/cross-site refusal for mutations, listable
  security headers, and a non-loopback bind refused unless `MAYA_API_ALLOW_REMOTE=1`. Evidence:
  `tests/test_api_local_security.py`.
- **2026-11-23 — environment provisioning is isolated and gated (MW-NEW-P1-05).**
  `workspace/environments.py` resolves an immutable `EnvironmentSpec` (lockfile hashes,
  `lockfile_absent`, managers, argv steps, image digest, services, secret references) with a
  deterministic digest. `workspace/provisioning.py` executes it only through an injected
  isolated runtime: `LOCAL_TRUSTED` is refused in code, download steps are `BLOCKED` without
  explicit network approval, failures stop the run, and inventory is read back from inside
  the runtime or reported `unavailable`. Services are declared and reported `not_started`.
  Evidence: `tests/test_environment_provisioning.py`.
- **Still outstanding (2026-11-23):** the live OCI gate has not been run on a rootless host in
  this environment, so **no container-isolation, session or provisioning claim is licensed**
  yet. Project image builds, a package/image cache, service orchestration with port publishing
  and the live Python/Node fixture are not implemented. A remote API bind remains
  unauthenticated, which is why it requires an explicit opt-in.

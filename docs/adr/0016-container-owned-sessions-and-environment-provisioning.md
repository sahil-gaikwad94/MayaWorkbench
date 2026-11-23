# ADR 0016 — Container-owned sessions and environment provisioning

- **Status:** accepted
- **Date:** 2026-11-23
- **Builds on:** [ADR 0015](0015-trustworthy-runtime-and-verification-release.md)
- **Audit items:** MW-NEW-P0-04 (remainder), MW-NEW-P1-05, WP2 (session deliverable)

## Context

ADR 0015 closed the isolation boundary for files, commands and checks, but left
two capabilities unavailable behind a typed refusal:

1. **Sessions.** A rootless task could not open a terminal, because
   `SessionManager` drove a host `subprocess.Popen`. Reporting the capability
   unavailable was honest, but it meant an isolated task could not run a dev
   server, a watch loop or an interactive test runner — the most common reason a
   working engineer opens a terminal.
2. **Environments.** `maya env plan` produced argv plans. A plan is not an
   environment: nothing installed dependencies, nothing recorded what was
   installed, and nothing could prove baseline and candidate shared it.

Both are core to the product's "engineering workbench" claim, and both can be
implemented without weakening the trust boundary.

## Decision

### 1. A session is a process inside the task container

`SessionBackend` now owns three things instead of one: `build` (translate the
requested argv for this runtime), `spawn` (start the client) and `terminate`
(stop the process). The manager no longer knows how a process is created.

`ContainerSessionBackend` executes every session command with the OCI exec API
inside a **retained** container for the task:

- The retained container is `run --detach` with exactly the same hardening flags
  as a task container (`_hardening_flags` is shared code, not a copy): network
  `none`, `--read-only` root, `--cap-drop ALL`, `no-new-privileges`, the same
  pids/memory/cpu/ulimit policy, and the *same single* sanitized `/work` mount.
- The host runs only the OCI client. The user's command never appears as a host
  process, so a container task cannot leak back to the host through a terminal.
- Each session's exec carries `MAYA_SESSION_ID=<id>`. Termination runs a generated
  `/bin/sh` script inside the container that signals only processes whose
  environment carries that marker, escalating to `SIGKILL` after a bounded grace
  period. PID 1 is explicitly skipped. The script lives in `.maya-sessions/`,
  which the sanitizer excludes from manifests and diffs.
- The id is generated before spawn, which is what makes "stop this session"
  precise rather than "stop everything".
- PTY sessions are refused inside the container. A container-side TTY cannot be
  verified from this environment, and silently accepting `use_pty` would let a
  caller believe it has one.
- If the retained container cannot start, the capability flips to unavailable
  with the observed reason. There is no host fallback, in either direction.

The retained container is stopped and removed by `RootlessContainerBackend.stop`,
before the overlay is deleted, so an exec never races a removed bind mount.

### 2. An environment is an immutable spec executed inside the runtime

`EnvironmentSpec` (in `workspace/environments.py`) records, with separate
provenance:

- detected ecosystems, package managers and **hashes of the lockfiles that
  exist**;
- `lockfile_absent`, naming ecosystems where reproducibility is weaker, instead
  of pretending a lockfile exists;
- the ordered `ProvisionStep` list (argv arrays, never shell strings);
- the pinned image and optional digest;
- declared services with a port and health-check argv;
- environment variables **by secret reference only** (`secret:NAME`);
- hardware requirements, empty when nothing was declared;
- the detection confidence, descriptor list and plan digest.

The spec digest is deterministic. Changing one byte of a lockfile changes it.

`EnvironmentProvisioner` (in `workspace/provisioning.py`) executes that spec
through an injected `ProvisioningRuntime`:

1. **`LOCAL_TRUSTED` is refused in code.** A host install is not a provisioned
   environment.
2. **Network is explicit.** A step that downloads packages is `BLOCKED` unless the
   caller approved it; the run stops there rather than half-installing.
3. **Every step is evidence.** Purpose, argv, exit code, duration and a hash of its
   log are recorded; the log is streamed to a caller-supplied sink.
4. **Inventory is read back, never assumed.** `pip list --format=json`,
   `npm ls --json` and `cargo metadata` produce a bounded package list. A failed
   inventory is `unavailable` with the command and exit code — not an empty list.
   Ecosystems without a bounded command report `not_supported`.
5. **Services are declared, not faked.** The run reports
   `services.status = "not_started"` with the concrete reason (no retained
   runtime with local port publishing in this release). Declaring the dependency
   is still useful for parity evidence; claiming it started would not be.
6. **Cleanup is owned.** The provisioner writes only inside the isolated runtime
   and reports `host_state_created: false`; the runtime owner removes the tree.

CLI: `maya env spec` prints the resolved spec; `maya env provision` is a no-op
report unless `--execute` is given, refuses any profile but `ROOTLESS_CONTAINER`,
fails closed when the live probe is unavailable, and requires `--allow-network`
for downloads.

### 3. Sanitizer policy bumped to `maya-sanitize-v3`

`.maya-sessions` joins `.maya-scripts` as execution apparatus excluded from source
manifests. The version bump is intentional: a manifest produced under v2 must not
be silently compared with one produced under v3.

## Consequences

- An isolated task can now run a persistent process, and the terminal inherits the
  same boundary as its file tools and checks.
- Sessions and commands share one container image, one mount and one resource
  policy, so "what the terminal sees" equals "what the checks see".
- Environments become reproducible artifacts (`spec_sha256`, `run_sha256`) instead
  of a documented intention.
- **Still not verified live:** no rootless OCI host exists in this environment, so
  the session container, the in-container terminator and real installs are
  covered by argv/evidence tests only. `docs/release-gates.md` lists what the live
  gate must add before any isolation or provisioning claim is licensed.
- **Still not implemented:** image builds from project recipes, a content-addressed
  package/image cache with SBOM/licence metadata, service lifecycle with local
  port publishing, and the Python/Node end-to-end fixture. Each is listed as
  `NOT_STARTED`/`PARTIAL` in `IMPLEMENTATION_STATUS.md` with its blocker.

## Evidence

| Decision | Tests |
|---|---|
| One hardened retained container, same flags as a task container | `tests/test_container_sessions.py::test_session_container_uses_the_task_hardening_flags` |
| Session commands execute inside the container | `tests/test_container_sessions.py::test_session_process_runs_inside_the_container` |
| Termination targets one session only | `tests/test_container_sessions.py::test_termination_targets_one_session_and_leaves_no_apparatus_in_the_diff` |
| Runtime stop removes the session container | `tests/test_container_sessions.py::test_stopping_the_runtime_removes_the_session_container` |
| Unstartable container / dead container fail closed | `tests/test_container_sessions.py::test_unstartable_session_container_is_a_typed_refusal`, `::test_dead_session_container_is_not_reused` |
| PTY refused in a container | `tests/test_container_sessions.py::test_pty_sessions_are_refused_inside_a_container` |
| Spec determinism, lockfile pinning, secret references | `tests/test_environment_provisioning.py` |
| Host provisioning refused; network gated; failures stop; inventory truthful | `tests/test_environment_provisioning.py` |
| Full suite | `docs/reports/test-run-2026-11-23.json` (later provider-hardening run: 834 collected, 827 passed, 7 skipped, 0 failed) |

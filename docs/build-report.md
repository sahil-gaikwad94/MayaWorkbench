# Build report — Part 2 fixes & upgrades (Sprint A/B slices)

- PDF fallback: typed EncryptedPdfError + controlled builtin fallback; 6 acceptance tests.
- Runner: per-limit getrlimit readback evidence, derived limits_enforcement, RLIMIT_NPROC removed, ExecutionTrust profiles that fail closed; 12 limit tests.
- Capability report: workspace/capabilities.py + `maya runner doctor|profiles`; 4 tests.
- WP1: ExecutionBackend, TrustedLocalBackend, RootlessContainerBackend (read-only source, writable overlay, cgroup limits, no home/socket/secrets, network none) with fake-OCI argv assertions; 8 tests.
- WP2: ToolCapability grants in TaskConstraints, closed allowlist in ToolGateway, --capability/--trust CLI; 5 tests.
- WP3: persistent sessions (stream/replay/input/idle/redaction/cleanup) + 5 gateway tools; 6 tests.
- WP4: Engineering Workspace data model + service + `maya workspace` CLI; 4 tests.
- Worker routes ROOTLESS_CONTAINER through the isolated backend; fails closed otherwise.
- P2: optional HMAC-SHA256 bundle attestation with attestation levels; 4 tests.

Verified: python -m compileall -q src OK; python -m pytest -q => 373 passed, 0 failed.
Truth table: IMPLEMENTATION_STATUS.md. Test matrix: docs/test-matrix.md.

## 2026-11-22 audit remediation (post-47c78e3)

- One canonical `ExecutionCapabilityReport`: controller presence is no longer an
  enforcement claim; rootless OCI needs a real smoke test; `READ_ONLY_ANALYSIS`
  is a code-enforced read-only tool mask.
- cgroup v2: exact finite `pids.max` readback (`max` rejected), live-pid drain and
  `rmdir` cleanup, worker opt-in with fail-closed auto-discovery.
- One `RuntimeWorkspace` per task: isolated overlays populated from the pinned
  snapshot, shared by file tools, commands, in-loop checks, artifact/diff capture
  and verification; retained candidate overlay, separate clean baseline.
- Agent loop: broken checkers are apparatus failures (`BROKEN_CHECKER`), finish
  after provider failure settles as REVIEWING with an explicit in-loop error.
- Lockfile-aware environment plans; NDJSON `fetch()` UI client with working
  approvals, task controls, real stored-diff view and `/ui` alias.
- Experience memory recalled during orientation and failed attempts recorded.
- Docs reconciled from one status registry; see `docs/wiki/audit-2026-11-22-remediation.md`.

Verification numbers for this package are recorded in `docs/test-matrix.md`.

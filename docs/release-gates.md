# Release gates

**Date:** 2026-11-22
**Status:** normative for releases; the two live harnesses in this document are opt-in.

This page is the authority for what a release may claim. Every gate below has an
exact command, the environment it needs, the claim a **pass** licenses, and the
meaning of a **skip**.

> **A skipped gate is NOT a pass.** A skip means the harness did not exercise the
> capability. It must be recorded as `SKIPPED (reason: ...)` in the release
> evidence and listed in `unverified`. Never convert it to "all green".
>
> **No live run has occurred in the environment where these gates were added.**
> Both live harnesses are collected by `pytest` but skip with explicit reasons
> here: there is no verified rootless OCI host and no live model key/spend cap.
> The deterministic and adversarial gates below are the only ones with local
> evidence in this environment.

## Gate summary

| Gate | Command | Required environment | Pass licenses | Skip means |
|---|---|---|---|---|
| Deterministic suite | `python -m compileall -q src` then `python -m pytest -q` | none | Code compiles; the deterministic suite is green at the recorded count | Not applicable — this gate must pass, never skip |
| Adversarial harnesses | `PYTHONPATH=src python artifacts/adversarial/harness{1..4}.py` | none | The 82 scripted probes pass; scripted-model behaviour only | Not applicable — a `[FAIL]`/`[ERROR]` is a release failure |
| OCI live | `MAYA_OCI_LIVE=1 MAYA_OCI_SMOKE=1 python -m pytest -q -m oci tests/integration/test_container_escape_live.py -rs` | rootless podman on PATH, cgroup-v2 delegation, `MAYA_OCI_LIVE=1`, `MAYA_OCI_SMOKE=1` | The real rootless container boundary was exercised: overlay, argv flags, host-read denial, pids readback, cleanup | No verified rootless runtime was present; **no isolation claim is licensed** |
| Model live | `MAYA_LIVE_MODEL=1 MAYA_MODEL_API_KEY=… MAYA_LIVE_SPEND_CAP_USD=0.10 python -m pytest -q -m live_model tests/integration/test_model_live.py -rs` | explicit opt-in key, hard spend cap, remote-model policy | The configured adapter produced schema-valid structured output and well-formed tool calls (if it declares `tool_calling`) | No key/cap/policy/capability; **no real-provider quality claim is licensed** |
| Release evidence | `maya release evidence --pytest-report PATH --adversarial-dir PATH --out DIR --json` | real pytest report and adversarial results; `git` metadata | A machine-readable record assembled from real inputs only | Not a skip gate: missing required inputs must exit non-zero with a typed error |

## 1. Deterministic suite (mandatory)

```bash
cd /root/MayaWorkbench
python -m compileall -q src
python -m pytest -q
```

Record the exact **collected / passed / failed / skipped** counts from the run.
A partial run does not license "all tests pass". This gate is deterministic and
must pass. Its result is the `tests` section of the release evidence.

## 2. Adversarial harnesses (mandatory, scripted only)

```bash
cd /root/MayaWorkbench
PYTHONPATH=src python artifacts/adversarial/harness1.py
PYTHONPATH=src python artifacts/adversarial/harness2.py
PYTHONPATH=src python artifacts/adversarial/harness3.py
PYTHONPATH=src python artifacts/adversarial/harness4.py
```

The four harnesses contain 31 + 20 + 23 + 8 = **82** probes. Each prints
`[PASS]` / `[FAIL]` / `[ERROR]` per claim and exits 1 on any failure. They use
scripted model doubles: a pass licenses the scripted-abuse claims only, **not**
real-provider prompt-injection resistance. Their counts are stored separately
from the unit-test count.

## 3. OCI live gate (opt-in, real rootless runtime)

```bash
cd /root/MayaWorkbench
# First prove the host capability from the canonical report:
python -m mayaworkbench.cli.main runner doctor --json

# Then run the harness (podman must be rootless and on PATH):
MAYA_OCI_LIVE=1 MAYA_OCI_SMOKE=1 \
  python -m pytest -q -m oci tests/integration/test_container_escape_live.py -rs
```

Required environment:

* `MAYA_OCI_LIVE=1` — deliberate opt-in. Without it the tests skip with a reason.
* `MAYA_OCI_SMOKE=1` — makes the canonical capability probe build the **actual
  production backend** with identical flags and run a bounded in-container probe.
  `probe_execution_capability_report()` must report
  `execution_backends["rootless-container"]["available"] is True`. A bare OCI
  binary, a smoke marker or cgroup controller names are never enough.
* A **rootless** podman on `PATH` plus a writable cgroup-v2 delegation with an
  exact finite limit readback. Docker requires `MAYA_ALLOW_DOCKER=1` and is not
  treated as verified by the canonical probe; prefer podman.

What a pass licenses (all mandatory, reported per control in
`oci_verification.smoke_test`):

* `/work` is populated from the pinned source while `.env`, credential paths,
  credential-shaped content and symlinks are excluded; `/src` is the **sanitized
  task-owned snapshot**, never the original worktree (`mount_policy_hash` binds
  the manifest to the filter version).
* Real OCI argv is observed: `--network none`, `--read-only`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--pids-limit`, `--memory`, `--cpus` (a CPU
  *core quota*), no home/SSH/socket/cloud-credential mounts, no host secret in
  the environment. CPU *time* is a separate RLIMIT_CPU dimension.
* **Effective** cgroup values are read from inside the task container and compared
  to the request: `pids.max`, `memory.max` and `cpu.max` must each show `enforced`
  with the observed value in the evidence. A control that is ignored is reported
  `not_applied`/`unsupported`, not masked by a passing one.
* Network denial is proven actively: the container cannot reach a host listener
  on loopback (not merely `--network none` in argv).
* Secret/canary denial is proven from every mount: `/src/.env`, `/src/.git/config`,
  `/src/.npmrc` and credential paths are unreadable; host canary, host home,
  Docker socket, `/etc/shadow` and `HOME/.ssh` are unreadable; mount visibility is
  confirmed from `/proc/self/mountinfo` (`/src` read-only, `/work` writable).
* Rootless terminal sessions run in a task-owned container, report `pwd=/work`,
  cannot read host canaries, and are fully cleaned up (no descendant survives).
* Baseline and candidate checks run in separate isolated runtimes with matching
  image, policy, limit evidence and mount-policy hash; parity is recorded.
* `stop()` removed the container and overlay with no surviving child. All
  subprocess timeouts are <= 60 s.

What a skip means: no verified rootless runtime was available. **No container
isolation, cgroup-enforcement or host-escape claim is licensed.** The gate must
appear in `unverified` with its concrete reason string.

## 4. Model live gate (opt-in, real provider, hard spend cap)

```bash
cd /root/MayaWorkbench
MAYA_LIVE_MODEL=1 \
MAYA_MODEL_API_KEY='<low-privilege-key>' \
MAYA_LIVE_SPEND_CAP_USD=0.10 \
MAYA_DATA_POLICY=BYOK_REMOTE_MODEL \
python -m pytest -q -m live_model tests/integration/test_model_live.py -rs
```

Required environment:

* `MAYA_LIVE_MODEL=1` — deliberate opt-in; no network call is made without it.
* `MAYA_MODEL_API_KEY` — supplied by the operator. The harness never logs or
  writes the key. Anthropic/Gemini adapters resolve their own provider-specific
  key variable from configuration; export that variable as well when using them.
* `MAYA_LIVE_SPEND_CAP_USD` — parseable, finite and > 0. **If the live flag is on
  and this is missing or invalid, the guard test FAILS (not skips).**
* `MAYA_DATA_POLICY=BYOK_REMOTE_MODEL` and the usual `MAYA_MODEL_PROVIDER`,
  `MAYA_MODEL_NAME`, `MAYA_MODEL_BASE_URL` settings for the configured adapter.
  Settings are read-only in the harness; no config file is mutated.

The harness makes at most two calls with `max_output_tokens <= 256`: (1) a tiny
JSON schema response that must validate, and (2) an echo tool definition whose
returned tool calls (if any) must name a declared tool and carry parseable JSON
arguments. If the configured adapter declares `tool_calling=False`, only the
tool test skips with a reason. Every assertion message records the hard spend cap.

What a skip means: no key, no cap, no remote-model policy, or no declared tool
capability. **No real-provider output-quality or tool-call claim is licensed.**

## 5. Release evidence

```bash
cd /root/MayaWorkbench
python -m pytest -q --junitxml=artifacts/release/pytest-junit.xml
maya release evidence \
  --pytest-report artifacts/release/pytest-junit.xml \
  --adversarial-dir artifacts/adversarial \
  --out artifacts/release \
  --json
```

`maya release evidence` is specified in
`docs/spec-launch-readiness.md` (WP-LAUNCH-1). It assembles the record from real
inputs only: it reads the repository, never executes repository code, never
invents counts, and exits non-zero with a typed error when a required input is
missing. It must be present for this gate to be satisfied; if the command is
absent from the build, the gate is **not** satisfied and must be listed as
unverified. Do not substitute hand-written numbers for this command.

### Release artifact fields

The evidence record must contain:

* `git`: commit SHA and dirty flag, plus `git status --short`.
* `environment`: Python, pytest, pydantic and platform versions; hash of the
  dependency manifest (`pyproject.toml`).
* `tests`: the exact test command and `collected` / `passed` / `failed` /
  `skipped` counts parsed from the supplied pytest JSON/JUnit report. With no
  report the value is `NOT_PROVIDED`, never `{passed: 0}`.
* `capabilities`: the canonical `ExecutionCapabilityReport` (same object as
  `maya runner doctor --json`).
* `adversarial`: per-harness PASS/FAIL/ERROR counts when supplied.
* `status_docs`: freshness booleans for `IMPLEMENTATION_STATUS.md` and
  `docs/test-matrix.md`.
* `unverified`: an explicit list of everything not exercised — including every
  skipped gate, with its reason string.

Secret values and the full host environment are never written into the record;
no `MAYA_*` key value and no generator-process `HOME` path may appear in it.
Re-running with unchanged inputs must yield the same evidence hash.

## Release decision rule

A release may claim only what a **passing** gate licenses. Any skip is copied
verbatim into `unverified`. It is never acceptable to describe a skipped OCI or
live-model gate as "green", "verified" or "all tests pass".

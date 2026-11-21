# MayaWorkbench — Part 2: Rigorous Product Audit, Fixes & Engineering-Workbench Upgrade Plan

**Audit date:** 21 November 2026  
**Audited implementation:** `MayaWorkbench-main (1).zip`  
**Vision documents:** `MayaWorkbench_BriefV1.md`, `Mayaworkbench V2 Master Blueprint.md`  
**Additional in-repository specification:** `MayaWorkbench_MasterProductandEngineeringSpec.md` (v2.1)  
**Audit type:** Static architecture review, source inspection, CLI smoke check, Python compilation, selected automated tests, and adversarial boundary review  
**Status:** Detailed engineering audit; not a penetration test or independent benchmark certification

---

## 0. Executive verdict

MayaWorkbench has a legitimate technical foundation. The ZIP contains a local-first Python application with a meaningful repository intelligence pipeline, research-document ingestion, claim extraction and mapping, a durable task workflow, bounded agent tools, isolated Git worktrees, command execution controls, verification records, experiment manifests/statistics, and provenance-aware project memory.

That is materially more substantial than a generic chatbot wrapper. The strongest product decision remains correct: **make the system responsible for an evidence-backed engineering outcome, not merely for generating code or producing confident explanations.** Preserve that core.

However, the current implementation is best described as a **single-user, local-first, Python-centric research/repository engineering alpha**. It is not yet the broad, connected engineering environment implied by the larger vision. It cannot presently operate as a full engineering computer with a general terminal, environment manager, multi-language semantic index, browser/research discovery, external tool connectors, remote GPU runner, or web control plane. Some of those features are sensibly deferred in the in-ZIP v2.1 specification; they should not be described as shipped.

There is also a significant trust boundary: the current local command runner is a restricted developer convenience, **not a hostile-code sandbox**. A Git worktree protects the original Git checkout, but it does not isolate a Python process from the user's filesystem or privileges. The current root-run process-count test also demonstrates that one advertised resource limit is not reliably enforced in that environment.

### Overall decision

| Area | Audit judgment | Why |
|---|---|---|
| Product direction | Strong | Research-to-Implementation (R2I), reproducibility, verification and evidence form a coherent wedge. |
| Current alpha foundation | Substantial | Real CLI, domain services, persistence, repository index, workflow and experiment code exist. |
| Repository intelligence | Useful but narrow | Python AST; other languages are primarily lexical; no demonstrated PPR or embedding retrieval in this ZIP. |
| Research-to-code | Partial | Ingestion, claim extraction/DAG and mapping exist, but the ambitious v2.0 paper-to-symbol engine is not present under its claimed name or full specification. |
| Agent capability | Early | A bounded local implementer with a compact tool set, not yet a general engineering agent. |
| Verification and evidence | One of the strongest areas | Deterministic checks, evidence records, manifests and statistical routines exist; structural checks are not semantic proof. |
| Experiment environment | Local alpha | Run provenance and statistics exist; no remote GPU/TPU runner mesh is shipped. |
| Execution security | Not ready for untrusted code | Local user privilege remains the effective boundary; the PID limit failure exposes an enforcement gap. |
| Connected AI and integrations | Very limited | Generic OpenAI-compatible gateway exists; no broad connector/MCP integration layer is shipped. |
| User-facing workbench | CLI-first | No production web application/API/control plane is present in the ZIP. |
| Full product readiness | Not yet | Strong alpha base; incomplete broad environment, security, model-integration and E2E validation. |

**Recommendation:** Do not throw away the current architecture or replace it with a large agent swarm. Strengthen the execution and tool substrate, generalise the project into an Engineering Workspace, and then add multi-language intelligence, connected tools and specialised agent workflows. The correct sequence is **trustworthy execution → richer tools → richer context → connected intelligence → bounded autonomy**.

---

## 1. What was actually audited

### 1.1 Supplied sources

1. **V1 Product Brief & Technical Blueprint** — defines the R2I wedge, product modes, task contract, agent roles, repository intelligence, experiment discipline, approvals and evidence requirements.
2. **V2.0 Master Blueprint** — adds the intended paper-to-code claim DAG, anti-gaming checks, PPR/context packing, cryptographic approvals, statistical audit, remote runner bridge and higher-level feature horizons.
3. **Current product ZIP** — implementation source, tests, documentation, adversarial artifacts and an additional in-repository v2.1 master product/engineering specification.

The ZIP's included v2.1 specification is important: it is more conservative about sequencing than the separate v2.0 blueprint. It explicitly places multi-language Tree-sitter, external research search, broader integrations, MLflow/TensorBoard, MCP, remote runners and more autonomous workflows in later stages. This audit therefore treats v2.1 as the best description of the intended staged delivery plan, while still evaluating the v2.0 claims as claims that need to be reconciled.

### 1.2 Inspection and checks performed

- Extracted and inspected the uploaded ZIP as a source snapshot. It contains approximately 207 ZIP entries and about 2.55 MB uncompressed.
- Inspected the Python package under `src/mayaworkbench`, the tests, `README.md`, phase reports, threat model, and the in-ZIP `MayaWorkbench_MasterProductandEngineeringSpec.md`.
- Ran `python -m compileall -q src`: **passed**.
- Ran `PYTHONPATH=src python -m mayaworkbench.cli.main --help`: **passed**; the CLI exposes the command families listed later in this report.
- Collected the test suite: **328 tests** are present.
- Ran selected test groups for scale guards, snapshots, storage and task budgets: **37 passed**.
- Ran the runner-limit tests excluding the fork-bomb case: **3 passed, 1 deselected**.
- Attempted the broader suite. It did not complete successfully: two reproducible failures were isolated, and later runs hit the execution window before full completion.
- Reviewed the documented Phase 6 audit. It states that the earlier internal adversarial work used scripted/mock model behaviour, did not use a real model provider, did not execute experiment runs, did not exercise task-process crash recovery, and was not a penetration test.

### 1.3 What was not established

This was not a complete real-model end-to-end validation. No external model API key or connected provider credentials were available for this audit. I did not certify real-time external literature retrieval, a Kaggle/Colab remote run, cross-platform behaviour, hosted multi-user security, deployment safety, or an independent performance benchmark. The full 328-test suite was not observed completing green.

These limitations matter. A passing unit test is evidence for a narrow behaviour; it is not evidence that a complete multi-step agent will solve a complex engineering task correctly.

---

## 2. Current product reality — shipped versus specified

### 2.1 Capabilities present in the ZIP

| Capability | Current implementation evidence | Current boundary |
|---|---|---|
| Local workbench bootstrap | `maya init`, `doctor`, local home/configuration | Single-user local operation |
| Project/repository management | `project`, `repo`, `snapshot` commands and persistence | One local control plane; no hosted project collaboration |
| Pinned repository snapshots | Snapshot services and immutable commit references | Correctness still depends on source checkout and Git state handling |
| Repository indexing | File discovery, Python parser, language/file metadata, symbol and import records | Deep AST support is Python-centric; other languages are lexical/marker-based |
| Repository map | Package manager/build marker detection, tests/config/docs/entrypoint summaries | Detection is not build execution or semantic understanding |
| Retrieval | Lexical BM25, exact/regex and symbol search; context assembly | No demonstrated vector embedding pipeline, reranker or PPR ranking in this ZIP |
| Grounded repository Q&A | `ask` and stored QA records; no-model deterministic digest path | No model key means no true model-based reasoning; answers remain bounded by retrieved context |
| Research documents | Document registration, PDF/text ingestion, claim extraction | No OCR for scanned documents; paper discovery is limited compared with a research search system |
| Claim mapping | Claim DAG and mapping services with evidence/source checks | Does not equal the full V2.0 hierarchical `PaperToCodeMatrixEngine` described in the separate blueprint |
| Task contracts/workflow | Task services, durable events, state transitions, approvals and worker | The task agent is a scoped local implementer, not a cross-system engineering agent |
| Worktree handling | Dedicated Git worktree for task changes | Git isolation is not a filesystem or process sandbox |
| Tool execution | `read_file`, `list_files`, `search_text`, `find_symbol`, `apply_patch`, `write_file`, `delete_file`, `git_status`, `git_diff`, `run_command`, `run_check`, `finish` | No general terminal, PTY, broad package/build toolchain or connected tool gateway |
| Command runner | argv execution, `shell=False`, executable allowlist, timeout, output cap, network-isolation attempt and OS limits | Local trusted-code boundary; process limit issue; no container/microVM boundary |
| Verification | Declared checks, test/command outcomes, integrity checks, diff review and evidence report | Static/structural integrity does not prove the implementation matches the paper or intent |
| Experiment records | Run manifests, metrics, provenance and comparison/statistical utilities | Local CPU/small-validation scope; no remote GPU/TPU runner mesh |
| Project memory | Provenance-aware records, user confirmation, commit-aware staleness | Curated durable knowledge, not yet a complete experience-learning/negative-results engine |
| Model gateway | Provider-neutral base/gateway and OpenAI-compatible adapter | Not a mature multi-provider router; no broad embedding/vision/reranking adapters |
| CLI | Local command interface with JSON output | No web UI/control plane is shipped in the ZIP |

### 2.2 High-impact V2.0 blueprint claims not found as shipped artifacts

The separate V2.0 document says the core algorithms have already been implemented and verified in `maya_workbench_engine.py` with a corresponding `test_maya_workbench_engine.py`. Those exact files and class names were not found in the supplied ZIP.

| V2.0 claim | Finding in supplied ZIP | Accurate wording for the product today |
|---|---|---|
| `maya_workbench_engine.py` and its dedicated test suite | Not present | Related functions are distributed across package modules and their tests. |
| `PaperToCodeMatrixEngine` | No exact class found | Claim extraction/DAG and claim-to-code mapping are present under other services. |
| `ExperimentLabAuditor` | No exact class found | Statistical and comparison utilities exist in `experiments/`; the exact named auditor is not present. |
| `RepoIntelligenceEngine` with damped PPR | No exact class or PPR implementation found | Repository indexing, symbol relationships and lexical retrieval exist; do not claim PPR ranking. |
| Submodular token-knapsack context packer | Not verified as the described algorithm | Context assembly and bounded retrieval exist; the claimed submodular algorithm is not evidenced. |
| HMAC-SHA256 ApprovalToken | No HMAC approval-token implementation found | There is an approval-record/scope mechanism, but it should not be described as the V2.0 HMAC-signed token scheme. |
| 18-state deterministic orchestrator | Related workflow state machine exists, but not the exact V2.0 named engine/specification | Describe the actual implemented states/transitions from the domain code, not the V2.0 architecture claim. |
| Remote Kaggle/Colab runner bridge | Not present | Experiment manifests and exportable data are not the same as a dispatch/runner bridge. |
| Signed `.maya-proof.json` | Not present as a signed proof protocol | Evidence bundles exist; the threat model states that local hashes are not a trusted external signature. |
| Veritas/Vexx symbolic equation-to-tensor bridge | Not present | Do not imply symbolic dimensional proof or a live external Veritas bridge. |
| Full AST anti-gaming proof | Structural integrity checks exist, but cannot reliably determine all semantic weakening | Label findings as structural heuristics; require independent mutation/invariant testing. |

**Action required:** Update the separate V2.0 blueprint status language. Change “Architecture-Verified & Core Engine Implemented” to something such as “Target Architecture and Algorithm Specification; implementation coverage varies by module.” Add a matrix linking each proposed component to an actual file, class, test and acceptance test. This is a credibility issue: the product should never announce an algorithm as implemented just because its pseudocode exists in a brief.

---

## 3. Product direction: keep the wedge, expand the work surface

The strongest idea in the V1 brief remains the right entry point: **Research-to-Implementation**. A user gives Maya a method, codebase and outcome; Maya investigates, maps, plans, implements, verifies and reports. That is specific, valuable and measurable.

The wider ambition should not turn MayaWorkbench into an unrestricted “AI employee” or a generic browser automation bundle. Instead, expand the unit of context from **repository** to **engineering workspace**.

### 3.1 Recommended product definition

> **MayaWorkbench is an evidence-first AI engineering environment. It understands a project's code, configuration, data, tools, runtime and experiments; plans work across them; executes bounded actions in isolated environments; and returns reviewable changes with reproducible evidence.**

A workspace may contain one or more repositories, documentation, datasets, notebooks, service definitions, environment specifications, databases, infrastructure configuration, experiment runs and approved external connectors.

This allows Maya to move beyond “answer questions about a repo” to tasks such as:

- Diagnose a failing service across application code, configuration, tests and logs.
- Upgrade a Python package while checking transitive dependencies, API compatibility and integration tests.
- Build and profile a TypeScript frontend with a Python API and a local database.
- Investigate a training regression across model code, dataset fingerprints, hyperparameters, GPU logs and experiment history.
- Reproduce a paper in an existing ML project, including environment setup and controlled baseline/candidate runs.
- Debug a Docker Compose or CI failure and prepare a reproducible fix.
- Assess a Terraform/Kubernetes change without applying it to a live environment.
- Investigate a database migration against schema, fixtures, application usage and rollback constraints.

### 3.2 What should remain out of scope for now

Do not start by building a general-purpose browser robot, an agent marketplace, a huge multi-agent swarm, a public untrusted-code execution service, autonomous production deployment or a distributed training platform. These will create security and maintenance obligations before the core task completion loop earns trust.

Use staged breadth: support more engineering environments behind the same narrow, auditable task lifecycle.

---

## 4. Critical findings and required fixes

Severity definitions:

- **P0 — Correctness/trust blocker:** fix before promoting the alpha as reliable or expanding execution authority.
- **P1 — Major capability gap:** required for the next meaningful product release.
- **P2 — Strategic expansion:** valuable after the execution/tool substrate is dependable.
- **P3 — Later differentiator:** high-effort capability that should be earned through evidence.

### MW-P0-01 — PDF extraction fails instead of falling back when optional `pypdf` is installed

**Severity:** P0 correctness defect  
**Observed test:** `tests/test_research_documents.py::test_pdf_content_stream_over_budget_is_skipped_with_a_warning`  
**Observed environment:** Python 3.13.5, pytest 9.0.2, optional `pypdf` installed

`research/pdf.py::extract_pdf_text()` uses `pypdf` whenever the package is importable. If `PdfReader` rejects the file (`PdfReadError: startxref not found` in the reproduced fixture), the code wraps the error as `ExtractionError` and exits. It does not try the bundled extractor, even though the module documentation says the built-in parser is a fallback when the optional extra is unavailable.

This creates an environment-dependent failure: a minimal/malformed document that the built-in bounded parser can partially handle may fail only on a machine where `pypdf` happens to be installed.

**Fix:** add a controlled fallback policy, preserving the original parser failure as a warning. Do not silently accept encrypted documents or turn an unreadable/scanned document into fabricated text.

Suggested control flow:

```python
def extract_pdf_text(data: bytes) -> PdfExtraction:
    validate_pdf_header(data)
    pypdf = _load_pypdf()
    if pypdf is None:
        return _extract_builtin(data)

    try:
        result = _extract_with_pypdf(pypdf, data)
        # If pypdf parsed the file but extracted empty text, do not automatically
        # call that a parser failure. Preserve its page-level warnings.
        return result
    except EncryptedPdfError:
        raise
    except ExtractionError as primary_error:
        try:
            fallback = _extract_builtin(data)
        except ExtractionError as fallback_error:
            raise ExtractionError(
                f"pypdf failed ({primary_error}); built-in fallback also failed "
                f"({fallback_error})"
            ) from fallback_error
        fallback.warnings.insert(
            0, f"pypdf parser failed; built-in fallback used: {primary_error}"
        )
        return fallback
```

Use a distinct encrypted-document error or typed failure reason rather than broad exception-message matching. Catch failures that occur not only during `PdfReader(...)` but also while enumerating a malformed page tree. Keep the fallback parser's decompression and byte ceilings intact.

**Acceptance tests:**

1. Same fixture passes with `pypdf` absent and present.
2. If `pypdf` rejects a supported Flate-stream PDF, built-in extraction is attempted and the report names the fallback.
3. If both parsers reject the document, the error reports both reasons without inventing text.
4. Encrypted PDF without usable credentials remains blocked; fallback must not bypass encryption policy.
5. Decompression-bomb byte ceilings remain effective on both paths.
6. A scanned/image-only PDF returns a no-text warning, not hallucinated OCR output.

### MW-P0-02 — Process-count cap is not reliably enforced; runner overstates limits

**Severity:** P0 security/trust defect  
**Observed test:** `tests/test_runner_limits.py::test_process_limit_stops_a_fork_bomb`

The root-run Linux test created 200 child processes despite a policy limit of 8. `RLIMIT_NPROC` is not enforced for privileged/root execution on Linux, and the current runner reports `limits_enforcement="rlimit"` generically. The code also silently tolerates `setrlimit` errors. Therefore, the result can claim a limit was applied when the kernel did not enforce the intended boundary.

There is a deeper issue: `RLIMIT_NPROC` is tied to the real UID and does not provide a robust per-task process-tree budget. A per-process CPU or address-space limit is not equivalent to an aggregate task budget either.

**Fix:** introduce per-limit enforcement evidence, and use a process-tree primitive for process counts. Preferred order:

1. Linux cgroup v2 per-run scope (`pids.max`, `memory.max`, `cpu.max`, optionally `io.max`) with a verified cgroup path and cleanup.
2. A rootless OCI runtime with cgroup limits where available.
3. A dedicated low-privilege user namespace/worker identity with explicit constraints, where supported.
4. If process-tree enforcement cannot be established, return `unsupported` and refuse the untrusted execution profile. Do not report full enforcement based only on attempting `RLIMIT_NPROC`.

Return a structured result such as:

```json
{
  "limits": {
    "wall_time": {"status": "enforced", "mechanism": "supervisor"},
    "cpu": {"status": "enforced", "mechanism": "cgroup-v2"},
    "memory": {"status": "enforced", "mechanism": "cgroup-v2"},
    "process_count": {"status": "unsupported", "mechanism": null},
    "file_size": {"status": "enforced", "mechanism": "rlimit-fsize"}
  }
}
```

The exact values must come from runtime probes, not static claims. Change the aggregate `limits_enforcement` field to a summary derived from the individual results (`all_enforced`, `partially_enforced`, `not_enforced`).

**Acceptance tests:**

- Fork bomb is stopped at or below the configured cgroup PID limit when cgroups are available.
- Root and non-root execution are both tested.
- If the host cannot provide process-tree limits, an untrusted profile is blocked before process creation.
- The result reports per-limit status accurately.
- Tests do not pass merely because the runner claims a limit was applied.
- A process that creates descendants and exits cannot leave those descendants running after cancellation or completion.

### MW-P0-03 — Worktree isolation is being confused with execution isolation by the product story

**Severity:** P0 trust boundary  
**Evidence:** `workspace/runner.py` explicitly states that it is a local developer boundary, not a hostile multi-tenant sandbox; `docs/threat-model.md` documents residual host access.

A Git worktree protects the source branch from direct edits. `PathPolicy` can protect typed file operations. But arbitrary Python code executed through an allowed `python -c` or project script still runs with the local user's OS privileges. It may access files the user can access. Removing provider API keys from the subprocess environment is good, but it does not prevent code from reading credentials stored in accessible files. Network namespace isolation is helpful when it is established and verified; it does not turn the process into a secure sandbox.

**Fix:** introduce explicit execution trust profiles, and never use the word “sandboxed” for the current profile.

| Profile | Purpose | Boundary | Allowed actions |
|---|---|---|---|
| `READ_ONLY_ANALYSIS` | Indexing, search, repository map | Read-only workspace snapshot | No code execution or writes |
| `LOCAL_TRUSTED` | User intentionally runs their own trusted project | Host user privileges, narrow argv tools | Explicit warning; no hostile repository claim |
| `ROOTLESS_CONTAINER` | Execute project code with isolation | Rootless OCI container, isolated mounts, cgroups, restricted capabilities | Build/test/install in disposable env |
| `REMOTE_MICROVM` | Higher-risk or untrusted repository execution | Per-task microVM/isolated worker | Stronger boundary, no host filesystem mount |
| `GPU_RUNNER` | Approved training/large validation | User-owned remote GPU/TPU environment | Approved manifests and bounded telemetry |

Only promote an untrusted-task mode when the isolation is verified. Mount the source snapshot read-only and a distinct workspace overlay read-write. Never mount the user's home directory, host Docker socket, SSH agent, cloud credentials or general secret directories into the agent runtime. Use scoped secret injection only when the task contract requires it and the user approves it.

**Acceptance:** a deliberately malicious fixture cannot read a host canary file, access a mounted credential, escape its writable directory, or continue consuming resources after the task ends. If a host cannot supply the required boundary, Maya must block execution or explicitly downgrade to `LOCAL_TRUSTED` with a clear warning and user decision.

### MW-P0-04 — Test completion and worker recovery evidence are incomplete

**Severity:** P0 release-readiness issue

The available run did not establish a green 328-test suite. The documented Phase 6 audit also says task-process crash recovery was not exercised and no real model provider was available. A CLI test exercising a separate worker and mock OpenAI-compatible server did not complete within the 45-second audit execution window; that is **not** enough to label it a product bug, but it means that path remains unverified here.

**Fix:** create a reproducible CI matrix and explicitly mark test coverage by layer.

- Fast deterministic unit tests: under a short budget, no external services.
- Integration tests: SQLite, worktree, runner, fake model gateway, fake connector.
- Worker lifecycle tests: crash during each workflow state, restart, resume, idempotency and duplicate event handling.
- Real-model opt-in smoke tests: run only in a protected CI job with a dedicated low-privilege test provider key and a cost cap.
- Platform tests: Linux non-root first; Windows/macOS compatibility if those platforms are advertised.
- Security tests: separately maintained adversarial suite; not conflated with unit-test count.

Add a release artifact containing commit SHA, Python version, dependency lock, test command, collection count, pass/fail/skip counts and the CI run URL. Never display “all tests pass” from a partial run.

### MW-P1-01 — Repository intelligence is too Python-centric for an engineering workbench

**Severity:** P1 major capability gap

The indexer extracts Python AST symbols. Other languages are represented mainly through lexical indexing and marker detection. That is useful for repository search but insufficient for cross-language call chains, build graph analysis, type references, multi-service applications, Rust crates, TypeScript frontends or C++/CUDA projects.

**Fix:** introduce a parser adapter layer.

Recommended order based on likely engineering workflows:

1. Python — retain current parser; add richer import resolution and test/config associations.
2. TypeScript/JavaScript/JSON — frontend and Node ecosystems.
3. Rust — crate/module/symbol and Cargo relationships.
4. Go — package/import/call references.
5. C/C++/CUDA — headers, compilation database, build targets and symbol references.
6. Java/Kotlin/C# and SQL/HCL/YAML/Dockerfile as demand requires.

Use Tree-sitter for syntax structure and incremental parsing, not as a claim of complete semantic understanding. Add adapters for language servers, compiler databases and build-system metadata where stronger resolution is required. Every relation must carry a confidence/source type (`AST_IMPORT`, `TYPE_REFERENCE`, `CALL_SITE`, `BUILD_DEPENDENCY`, `CONFIG_REFERENCE`, `HEURISTIC`). Dynamic dispatch, reflection, generated code and runtime dependency injection must remain explicitly uncertain.

**Acceptance:** multi-language fixture repositories with a backend, frontend, shared schema and infrastructure files; questions and change impact analysis must find relevant symbols/configs and return source locations. Measure index correctness against a manually reviewed gold set.

### MW-P1-02 — Current execution toolset cannot build or operate real projects

**Severity:** P1 major capability gap

The runner defaults to `python`, `python3`, `pytest`, `ruff` and `mypy`. Dependency installation is refused. There is no general shell session or PTY, no background process manager, and no Node/Rust/Go/JVM/build tools. This intentionally limits risk, but also means Maya cannot build a typical web application, compile native code, run a local API/database stack or reproduce many ML environments.

**Fix:** do not simply add `bash`, `npm`, `cargo`, `docker` and arbitrary executable names to the current host runner. First add the isolated environment execution profile from MW-P0-03. Then support project-specific environment descriptors and allowlisted tools inside the isolated runtime.

Support:

- Environment detection from `pyproject.toml`, `uv.lock`, `requirements*.txt`, `package.json`/lockfiles, `Cargo.toml`/`Cargo.lock`, `go.mod`, Maven/Gradle files, CMake/Make/Ninja, Dockerfiles and Compose files.
- Reproducible environment creation from lockfiles or pinned images.
- Build/test/lint/type-check commands as structured tasks.
- Explicit dependency install approval, performed inside disposable environment, not on the host.
- Persistent process sessions for dev servers, databases, test watchers and notebooks.
- Session output streaming, stop/restart, health checks and process-tree cleanup.

### MW-P1-03 — Model gateway is an adapter, not yet a connected AI platform

**Severity:** P1 major capability gap

The code contains a provider-neutral model gateway and an OpenAI-compatible adapter, but there is no demonstrated mature provider routing layer, embedding adapter, reranker, vision model interface or external tool connector framework. The current documented agent audit used scripted/mock model behaviour.

**Fix:** separate five provider capabilities behind interfaces:

- `ChatModel`: text generation, streaming and structured output.
- `EmbeddingModel`: document/query vectors.
- `Reranker`: candidate ranking.
- `VisionModel`: screenshots, diagrams, charts and visual inspection.
- `ToolConnector`: external systems and MCP adapters.

Add model routing based on task type, uncertainty, context size, latency, provider health, token budget, data policy and actual tool-call reliability. The model is not the authority layer; it proposes actions, while backend policy decides whether an action is permitted.

### MW-P1-04 — Research ingestion is not a complete research discovery workflow

**Severity:** P1 major capability gap

The current code can register and extract documents and fetch a supplied URL/DOI with security checks. That is not the same as finding and comparing literature. There is no demonstrated literature discovery index with query expansion, source ranking, citation graph, recency filtering and cross-source claim reconciliation. Scanned PDFs are not OCR'd.

**Fix:** stage research capabilities:

1. Search reputable literature metadata (for example, Crossref, OpenAlex, Semantic Scholar or arXiv) and official documentation.
2. Save immutable source metadata, retrieval timestamp, URL/DOI, paper version and hash.
3. Extract sections, equations, tables, algorithms and page/paragraph citations.
4. Distinguish paper statements, author claims, reproduced results, independent results and Maya inference.
5. Build claim dependencies and link each claim to source spans, repository symbols and validation checks.
6. Detect conflicting versions and retracted/corrected papers where source metadata supports it.
7. Add OCR only as an explicit optional capability with page images, confidence and OCR provenance. Never silently blend OCR guesses into exact quotes.

### MW-P1-05 — Task agent tools do not cover an engineering computer

**Severity:** P1 major capability gap

Current tools are repo/file/Git/command/check/finalization focused. The user asks for an agent that can operate a real complex engineering environment. It needs typed, observable operations across build tools, processes, APIs, datasets, logs, containers, notebooks and connected systems.

**Fix:** expand through a central tool gateway (see Section 8), not by teaching the model that it can execute arbitrary shell commands without policy.

### MW-P1-06 — No visible user-facing workbench or API control plane

**Severity:** P1 product experience gap

The ZIP ships a CLI. The v2.1 spec describes a thin FastAPI/API and UI direction, but those are not shipped in the uploaded implementation. CLI is excellent for the first alpha, but a multi-step task with approvals, sessions, diffs, evidence, experiments and connectors ultimately needs a web workspace.

**Fix:** build a thin UI around stable application services and event contracts. Do not move business logic into the frontend. Start with a project overview, task timeline, plan/approval screen, live terminal/session view, diff/review, evidence report and experiment view.

### MW-P2-01 — Memory is not yet a negative-result and experience-learning system

**Severity:** P2 differentiator gap

The current memory is provenance-aware and confirmation-based, which is a good trust choice. But the broad vision implies reuse of failed approaches and prior experiments. Add a negative-result store with task goal, hypothesis, environment, base commit, patch hash, failure signature, experiment outcome, confidence, expiry/staleness and retrieval triggers. Similarity should suggest a prior attempt, not automatically prohibit a new approach.

### MW-P2-02 — No bounded multi-runner compute mesh

**Severity:** P2 capability gap

The v2.0 bridge to Kaggle/Colab is not present. Start with portable run bundles (commit, patch, environment lock, manifest, command, seeds, dataset fingerprint and expected outputs) and a user-driven import/export flow. Add remote runner adapters only where APIs and quotas are stable and permitted. Treat free-tier availability as volatile; do not promise a permanent quota or zero cost.

### MW-P2-03 — Evidence bundle is not cryptographically attested

**Severity:** P2 trust differentiator

A hash in a local bundle detects accidental modification only if the hash source itself is trusted. The threat model acknowledges that a local attacker can alter both the database/artifacts and hashes. Add an optional signature envelope backed by a user-held signing key or trusted CI identity. Distinguish `HASHED`, `SIGNED_BY_USER`, `SIGNED_BY_CI`, and `UNATTESTED`; a signature proves which key signed the bundle, not that the scientific result is true.

---

## 5. The upgraded architecture: Engineering Workspace + Evidence-First Agent Runtime

### 5.1 High-level architecture

Keep a modular monolith for the control plane. Separate the execution worker early, because its privilege, lifecycle and failure modes differ from the API and orchestration services.

```text
                        MAYAWORKBENCH CONTROL PLANE
┌──────────────────────────────────────────────────────────────────────┐
│ CLI / Web UI / API                                                    │
│       │                                                              │
│ Workspace & Project Service ── Task/Workflow Orchestrator              │
│       │                         │                                     │
│ Repository & Research Context   │ Task DAG / Approvals / Budgets       │
│       │                         │                                     │
│ Context Engine                  ├── Agent Role Runtime                 │
│  - parsers / symbols             │    - planner / researcher           │
│  - lexical + vector retrieval    │    - architect / implementer        │
│  - source/provenance graph       │    - test / security / reviewer     │
│       │                         │                                     │
│ Model Gateway                   Tool Gateway                           │
│  - chat / embeddings             - local workspace tools                │
│  - rerank / vision               - terminal/process tools                │
│  - routing / budgets             - browser/API/connector tools            │
│       │                         - experiment/compute tools              │
│       └────────────┬──────────────┘                                     │
│                    │                                                  │
│ Persistence: SQLite/Postgres | Event Log | Memory | Evidence Registry │
└────────────────────┬─────────────────────────────────────────────────┘
                     │ signed/validated job specification
                     ▼
┌──────────────────────────────────────────────────────────────────────┐
│ EXECUTION PLANE (separate worker/service)                              │
│ Runtime profile selector → isolated workspace → command/process       │
│ manager → resource enforcement → artifacts/logs/telemetry              │
│                                                                          │
│ Local rootless container / remote microVM / user-owned GPU runner       │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.2 Workspace is the new top-level object

Avoid making the repository table carry every future resource type. Introduce:

- `Workspace`: durable scope for a research or engineering effort.
- `ResourceAttachment`: typed connection to a repository, document set, dataset, notebook, service, database schema, infrastructure environment, experiment tracker or connector.
- `EnvironmentSpec`: declarative language/runtime/toolchain and dependency-lock description.
- `ExecutionProfile`: where and under what security/resource policy work executes.
- `Task`: user goal with constraints, acceptance criteria, allowed resource scope and required approvals.
- `AgentRun`: orchestrator execution with role invocations and a trace.
- `ToolInvocation`: typed operation, permission decision, input/output hashes, duration and result.
- `Artifact`: diff, report, log, notebook, plot, model checkpoint, test result or reproducibility bundle.
- `EvidenceItem`: verifiable support for a specific claim.

One workspace may link multiple repositories and non-code resources. Keep commit-pinned repository snapshots exactly as they are: they remain critical provenance records.

### 5.3 Execution profiles and runtime abstraction

Create an `ExecutionBackend` interface rather than baking local subprocess semantics into every workflow.

```python
class ExecutionBackend(Protocol):
    def capabilities(self) -> ExecutionCapabilities: ...
    def provision(self, spec: EnvironmentSpec, policy: ExecutionPolicy) -> RuntimeRef: ...
    def exec(self, runtime: RuntimeRef, argv: list[str], timeout: int) -> ExecutionResult: ...
    def open_session(self, runtime: RuntimeRef, command: list[str]) -> SessionRef: ...
    def stream_session(self, session: SessionRef) -> Iterator[ProcessEvent]: ...
    def stop(self, runtime: RuntimeRef, reason: str) -> CleanupResult: ...
    def collect_artifacts(self, runtime: RuntimeRef) -> list[ArtifactRef]: ...
```

Initial backends:

1. `TrustedLocalBackend` — keep the current bounded local runner but label it accurately.
2. `RootlessContainerBackend` — disposable OCI workspace, read-only base snapshot, writable overlay, no host home, no Docker socket, cgroup limits and optional restricted network.
3. `RemoteMicroVMBackend` — future per-task isolated VM for higher-risk execution.
4. `UserOwnedRunnerBackend` — future export/dispatch/import interface for a user-controlled GPU/TPU or lab machine.

The orchestrator should ask the backend for capabilities before planning. If Docker/Podman, cgroups or network namespace support is unavailable, the plan must adapt or block; it must not silently pretend the requested runtime exists.

### 5.4 Environment profiles

A repository map should not just detect `package.json` and stop. It should identify a project environment and build/test lifecycle:

| Ecosystem | Detection inputs | Initial runnable actions |
|---|---|---|
| Python | `pyproject.toml`, `uv.lock`, `requirements*.txt`, `tox.ini`, `pytest.ini` | create isolated venv/container, install pinned deps, pytest, lint, type-check |
| Node/TypeScript | `package.json`, npm/pnpm/yarn lock, `tsconfig.json` | install from lock, lint, type-check, unit tests, build, dev server |
| Rust | `Cargo.toml`, `Cargo.lock`, workspace manifests | `cargo check`, `cargo test`, clippy, build |
| Go | `go.mod`, `go.sum` | `go test`, `go vet`, build, benchmark |
| Java/Kotlin | Maven/Gradle wrapper and lock/version files | compile, unit/integration tests, test report |
| C/C++/CUDA | CMake/Make/Ninja, compile database, toolchain metadata | configure, compile, test, sanitizer/perf profiles where installed |
| Data/ML | notebooks, datasets, model configs, training scripts, environment locks | smoke evaluation, dataset validation, controlled experiment runs |
| Infrastructure | Dockerfile/Compose, Terraform, Kubernetes YAML, Helm | static validation, plan/diff, local integration; no live apply by default |
| Database | migrations, schema, ORM models, fixtures | schema validation, migration dry-run, integration tests on disposable DB |

Never run package install scripts on the user's host. Package-manager commands run inside a disposable environment with network policy and artifact caching. Capture lockfile hashes and image digests.

---

## 6. Repository and context intelligence upgrades

### 6.1 Multi-layer index

Use a layered retrieval model, not a single vector database:

1. **File index:** paths, sizes, MIME/type, content hash, language, generated/vendor/test/config flags.
2. **Syntax index:** AST/CST symbols, signatures, docstrings/comments, imports, exports, inheritance, call sites where supported.
3. **Build/dependency graph:** packages, workspaces, build targets, services, environment dependencies, container layers, schema dependencies.
4. **Runtime/evidence graph:** tests covering symbols, CI jobs, log/error associations, experiment configs, model/dataset links.
5. **Research graph:** paper passages → claims → equations/algorithms → candidate symbols → invariants → tests/experiments.
6. **Memory graph:** confirmed project conventions, prior decisions, failed hypotheses and validated outcomes.

Each record must identify the source revision, parser/tool version and extraction method. An index with a stale base commit must be visibly stale.

### 6.2 Retrieval should be hybrid and evidence-first

Keep BM25/exact search. Add embeddings and reranking as optional augmentations, not as replacements. A strong query should:

- Identify likely symbols and exact identifiers first.
- Search issue/error strings and relevant config/test names.
- Traverse import/build/dependency relationships.
- Retrieve semantic candidates for conceptual matches.
- Rerank a bounded candidate pool.
- Read source spans directly before making claims or edits.
- Include negative results and uncertainty when the search coverage is incomplete.

**PPR is optional, not the product goal.** The v2.0 blueprint describes a specific damped PPR variant, but it is not present in the ZIP. Implement it only if a measured retrieval evaluation shows a gain over simpler graph traversal and reranked retrieval. Publish recall/precision, context token use, latency and task success with/without PPR. Do not let a sophisticated ranking formula become a substitute for good retrieval evaluation.

### 6.3 Context packer

Create a deterministic `ContextPack` with:

- query/task contract;
- relevant source spans and their exact revision/line ranges;
- symbol and dependency relations;
- relevant tests/checks;
- prior decisions and failed approaches;
- research claims/equations as applicable;
- unresolved ambiguities;
- token/cost estimate and retrieval reason for each item.

Do not claim context coverage just because the prompt reached a token limit. Save a manifest of included and excluded candidates with score and exclusion reason. Detect duplicate/overlapping spans. Keep whole function/class bodies only when useful; otherwise include signatures plus selected blocks and retrieve on demand.

### 6.4 Incremental indexing

P1 must add content-hash-based incremental indexing. Re-index changed files and rebuild only affected graph edges. Store `commit_sha`, file hash, parser version and index schema version. A parser upgrade invalidates only the affected language records, not necessarily the entire repository. Tests must verify that deleted symbols disappear and stale graph edges do not persist.

---

## 7. Research-to-Implementation: make the flagship workflow scientifically defensible

### 7.1 Claim-to-code DAG

The existing claim and DAG services are a useful foundation. Evolve them to a versioned claim graph with typed claims:

- `PAPER_METHOD`: what the paper method actually specifies.
- `PAPER_RESULT`: result reported by the paper authors.
- `REPRODUCTION_REQUIREMENT`: a detail necessary to reproduce the method.
- `USER_REQUIREMENT`: the user's desired behaviour or constraint.
- `INFERRED_REQUIREMENT`: an inference made by Maya; must be labelled as such.
- `VALIDATION_CLAIM`: what a test or experiment is intended to establish.

Each node should include:

```json
{
  "claim_id": "claim-...",
  "kind": "PAPER_METHOD",
  "statement": "Use causal masking before normalisation",
  "source": {"document_id": "paper-...", "page": 4, "section": "Method", "span_hash": "..."},
  "explicitness": "EXPLICIT",
  "depends_on": ["claim-..."],
  "target_resources": [{"kind": "SYMBOL", "path": "src/model/attention.py", "symbol": "Attention.forward"}],
  "mapping_status": "PARTIAL",
  "validation_ids": ["invariant-causal-mask"],
  "uncertainty": ["paper does not specify kernel implementation"]
}
```

Mapping statuses should include `PRESENT`, `PARTIAL`, `ABSENT`, `CONFLICTING`, `UNCERTAIN`, `NOT_APPLICABLE`, and `NOT_CHECKED`. `ABSENT` is allowed only after recording search scope and coverage. An incomplete index must not produce a confident absence claim.

### 7.2 Mathematical implementation checks

Do not attempt to “prove” arbitrary PyTorch code correct with an LLM. Combine:

- equation extraction with original source references;
- symbolic shape/dimension contracts where the expressions are machine-readable;
- parameter-domain constraints;
- forward shape/dtype/device checks;
- numerical reference comparisons on tiny inputs;
- backward gradient/finite-difference checks;
- invariance tests (causality, permutation, masking, conservation, equivariance where appropriate);
- deterministic seed and tolerance manifests;
- stress tests for long sequence, batch extremes and invalid inputs.

A gradient test where every parameter has a non-zero gradient is not universally correct: some parameters may be inactive for a valid branch or specific input. The invariant must be defined per method and per parameter group, with justified exceptions. The micro-probe should be **method-specific** rather than a universal assertion.

### 7.3 Reproduction risk score

A single scalar is useful for triage but must not pretend to be a scientific probability. Display separate dimensions:

- source completeness;
- method detail completeness;
- implementation mapping coverage;
- dataset/license availability;
- compute availability;
- baseline comparability;
- evaluation protocol quality;
- environment reproducibility.

If a combined score is shown, expose the formula and weights and call it a heuristic readiness score. Block full reproduction if critical information is missing; allow a clearly labelled smoke test or partial reproduction where useful.

### 7.4 Experimental discipline

The current statistical functions include more than the V2.0 document's headline: paired and Welch t-tests, confidence intervals, Hedges' g and Holm-Bonferroni adjustment are present. Keep this work, but strengthen interpretation:

- Pre-register the primary metric, direction, minimum practically important effect and analysis plan.
- Treat seeds as paired only when the experimental design genuinely pairs comparable random conditions.
- Ensure baseline and candidate use the same evaluation script, dataset split/fingerprint, hardware/software profile and relevant preprocessing.
- Separate run-to-run variation from dataset sampling uncertainty.
- Check normality/robustness when sample sizes are small; consider bootstrap/permutation intervals as an additional option.
- Report effect size and interval, not only p-value.
- Correct for multiple comparisons; label exploratory metrics.
- Avoid declaring an improvement from a tiny number of seeds.
- Record failed runs and missing metrics; do not discard them invisibly.

A small p-value does not establish correct implementation, external validity or a practically useful improvement.

---

## 8. Tool Gateway: the key to a real engineering agent

### 8.1 All tools must go through one policy gateway

Every local or connected tool call should be a typed `ToolInvocation`. The model should never be the component that authorises a dangerous action. The gateway checks:

1. caller role and task contract;
2. project/workspace/resource scope;
3. required capability and granted permission;
4. approval token or human gate when required;
5. execution profile and supported capability;
6. runtime, token, cost, network and data-sharing budgets;
7. idempotency and duplicate-call handling;
8. audit event and evidence capture.

MCP should be treated as an interoperability protocol, not the security boundary. A remote MCP server can still return malicious or misleading content or request actions outside the task. The local tool gateway owns consent, scopes, approvals, secrets and audit policy.

### 8.2 Tool families to add

| Family | Initial tools | Example use | Default permission |
|---|---|---|---|
| Workspace/files | `workspace_tree`, `read_resource`, `write_resource`, `search_resource`, `diff_resource` | Inspect config, docs, code, notebook | Read by default; writes require approved task scope |
| Repository/Git | branch/worktree, status, log, blame, diff, cherry-pick proposal, patch export | Change management | Isolated workspace only; publish requires approval |
| Terminal/process | open session, exec argv, stream output, send input, stop process, inspect process | Build, test, run server | Isolated runtime; command policy and budgets |
| Environment | detect, resolve, provision, install, lock, inspect, export | Create project toolchain | Install only in disposable environment with approval |
| Build/test | build, lint, typecheck, unit/integration/e2e test, coverage | Validate full project | Pre-approved checks or user approval |
| Browser/research | web search, fetch URL, official docs lookup, source capture | Verify API docs, paper discovery | Read-only, network allowlist, source/provenance logs |
| HTTP/API | request with scoped auth, OpenAPI inspect, service health check | Test local/approved API | Domain allowlist; secrets broker; mutations approval-gated |
| Database | schema inspect, read-only query, migration dry-run, explain query | Debug data layer | Read-only by default; writes on disposable DB only |
| Notebook/data | inspect notebook, execute cell/range, profile dataset, validate schema | ML/data workflows | Isolated runtime, dataset/privacy policy |
| Containers/services | compose up/down, inspect health/logs, build image, list services | Multi-service development | Disposable isolated container runtime |
| Observability | logs, traces, metrics, crash dumps, profiles | Diagnose runtime/performance | Read-only scope and data redaction |
| Experiment/compute | run manifest, schedule, poll, cancel, import artifacts | Training/evaluation | Budget and hardware approval required |
| CI/code hosting | inspect checks, fetch issue/PR, create draft PR | Maintainer workflow | Narrow OAuth scope; publish/merge requires approval |
| Infrastructure | validate/plan/diff IaC, inspect state metadata | Infrastructure review | No apply/destroy without explicit high-risk approval |

Do not add every tool in one release. Start with workspace, environment, process, build/test and artifact tools. These unlock general engineering tasks without opening high-risk cloud/deployment actions.

### 8.3 Tool contract

Each tool needs a machine-readable contract:

```json
{
  "tool_id": "process.exec",
  "version": "1.0",
  "input_schema": "...",
  "output_schema": "...",
  "capabilities": ["process.execute"],
  "risk": "MEDIUM",
  "side_effects": ["filesystem_write", "process_spawn"],
  "network": "DENIED_BY_DEFAULT",
  "resource_limits": ["wall_time", "cpu", "memory", "pids", "output_bytes"],
  "idempotency": "CALLER_KEY_REQUIRED",
  "approval": "PROFILE_POLICY",
  "evidence_outputs": ["argv", "exit_code", "stdout_hash", "stderr_hash", "duration", "runtime_id"]
}
```

The exact schema can evolve, but tool identity, schema version, capability, risk, side effects, idempotency, permission and evidence must be explicit. Tool errors should be structured (`POLICY_BLOCKED`, `RUNTIME_UNAVAILABLE`, `TIMEOUT`, `OUTPUT_LIMIT`, `APPROVAL_REQUIRED`, `CONNECTOR_AUTH_EXPIRED`) rather than only free-form strings.

### 8.4 Persistent terminal sessions

A real engineering environment needs stateful processes. Introduce a session manager with:

- session ID, runtime ID, command, cwd and environment hash;
- PTY or non-PTY mode;
- bounded input/output stream and sequence numbers;
- process tree/cgroup ID;
- idle timeout and absolute maximum lifetime;
- cancellation and cleanup acknowledgement;
- reconnect/replay from last event sequence;
- redaction policy for sensitive outputs.

Keep typed operations for common checks, and expose a terminal session only inside the approved isolated runtime. Do not confuse `run_command(argv)` with a complete terminal experience.

---

## 9. Agentic architecture and intelligence upgrades

### 9.1 One orchestrator, specialised roles, typed handoffs

Do not build a swarm where every agent invents its own objective and tools. Build one deterministic orchestrator with role-specific invocations. A role can use a different model/provider, but it must emit a typed artifact and cannot silently change workflow state.

| Role | Main responsibility | Output artifact | Default authority |
|---|---|---|---|
| Task Analyst | Convert user intent into task contract; detect missing constraints | `TaskContract` | No writes or external actions |
| Research Analyst | Investigate papers/docs/issues and capture source evidence | `ResearchDossier` | Read-only research sources |
| Workspace Analyst | Map code, environments, services, data and dependencies | `WorkspaceMap` | Read-only workspace |
| Architect | Design minimal change and explain trade-offs | `ImplementationPlan` | No direct writes |
| Implementer | Make scoped changes and add tests | `PatchSet` | Approved isolated write scope |
| Environment Engineer | Provision environment and run builds/tests | `ExecutionPlan`, `RunArtifacts` | Runtime-specific capabilities |
| QA/Test Engineer | Design adversarial, regression and acceptance tests | `VerificationPlan`, `TestResults` | Test runtime; no weakening baseline tests |
| Security Reviewer | Threat model changes, dependencies, secrets, permissions | `SecurityFindings` | Read-only by default |
| Performance/Experiment Analyst | Profile, compare runs and interpret metrics | `ExperimentAnalysis` | Read-only metrics/artifacts |
| Independent Reviewer | Inspect change/evidence separately from implementer | `ReviewReport` | Read-only diff and logs |
| Release Coordinator | Assemble artifact bundle and proposed publication | `DeliveryPackage` | Draft only; publishing requires human approval |

Parallelise only independent tasks. A research analyst and workspace analyst can work concurrently; implementation must wait for relevant findings. A reviewer should receive the contract, patch, deterministic results and evidence, not simply the implementer's self-evaluation.

### 9.2 The task DAG

Represent work as a dependency graph of typed steps. Each step has:

- objective and acceptance criteria;
- dependencies;
- responsible role;
- required capabilities;
- inputs/outputs with content hashes;
- budget/time limits;
- retry policy;
- state and terminal reason.

The orchestration state remains backend-owned. An LLM cannot announce itself `COMPLETED`. A terminal task state requires the required deterministic gates and a valid evidence report.

### 9.3 Bounded repair loop

Keep the current bounded repair concept. Improve it by requiring each repair attempt to contain:

1. failing evidence and exact reproduction;
2. root-cause hypothesis;
3. predicted change and expected test impact;
4. patch hash;
5. targeted validation;
6. regression validation.

Do not retry a deterministic failure with the same patch. Limit repair attempts and token/runtime budgets. If no new hypothesis emerges, stop and report a diagnosis rather than making random edits.

### 9.4 Planning that reasons about the whole environment

Before an implementation plan, Maya should answer:

- What resources does the task touch (repos, files, data, services, database, cloud/CI)?
- What must be read first?
- What is the baseline state and how is it measured?
- What dependencies/environment are needed?
- What side effects can occur?
- What approvals are needed?
- What tests/evidence prove acceptance?
- What is the rollback plan?
- What will remain unverified?

This is the difference between “generate a file patch” and “engineer a working change.”

### 9.5 Model routing and context policy

Route by task requirement, not leaderboard alone. Example policy:

| Task | Model route | Deterministic companion |
|---|---|---|
| Repository summarisation | Low-cost local/fast model | AST/parser and exact retrieval |
| Architecture design | Strong reasoning model | Dependency graph, build/test metadata |
| Routine code edits | Reliable coding model | Patch validator and tests |
| Paper reasoning | Strong long-context model | PDF extraction, citations, equation/source checks |
| Research search synthesis | Retrieval-capable model | Search source registry and quote validation |
| Code review | Separate model/context | Static analysis, type checks, secret scan, tests |
| Result analysis | Reasoning/analysis model | Statistics, manifest parity and plots |
| Embedding/retrieval | Embedding model + reranker | Lexical search fallback |

Record provider, exact model ID, model API/version if available, configuration, token usage, latency, cost estimate, retry history and failure. Keep provider credentials outside the workspace database and never inject them into arbitrary execution environments.

---

## 10. Connected AI and external integrations

Connected systems should extend Maya's capabilities without bypassing its policy gateway.

### 10.1 Integration roadmap

**First wave (P1):**

- OpenAI-compatible endpoints and local Ollama/llama.cpp adapters.
- At least two additional first-party model providers with structured output/streaming support, subject to user's selected services and current SDK compatibility.
- Literature/documentation search adapters (arXiv and a scholarly metadata API, then official docs search).
- GitHub read-only integration for issues, pull requests, commit checks and review context.
- Jupyter/notebook artifact read/execute inside isolated runtime.
- Local Docker/Podman runtime adapter.

**Second wave (P2):**

- GitHub draft PR creation and CI check retrieval.
- MLflow and TensorBoard experiment adapters; W&B only with explicit user connection/data controls.
- Remote runner bundles for user-owned GPU/TPU/Colab/Kaggle compute where technically and contractually supported.
- Database connectors with read-only default and disposable test database mode.
- Sentry/OpenTelemetry/logging backends for user-approved observability context.
- GitLab/Bitbucket and additional hosting providers if users request them.

**Later:** Slack/Notion/team collaboration, richer infrastructure providers, multi-user organisation control plane and deployment workflows. These need a stronger privacy and permission model first.

### 10.2 Connector security

- OAuth or provider-scoped tokens with least privilege.
- Read-only scopes by default.
- Store secrets in OS keyring or a dedicated secret store, not plaintext SQLite.
- Redact credentials and sensitive payloads from event logs.
- No ambient secret inheritance into code execution.
- Show the exact external action and target before write operations.
- Require approval for creating PRs, comments, issues, deployments, DB writes, cloud jobs or data exports.
- Record connector identity, account, scope, request hash, response hash and timestamp.
- Provide revoke/disconnect and data deletion controls.

### 10.3 Browser/research tools are not general web automation

Start with source retrieval: search, fetch, extract, capture source metadata and evidence. A general browser computer-use agent is a high-risk capability and not required for the first Engineering Workbench release. When a browser action is eventually added, treat it as an explicit external side-effect tool, isolate its cookies/session, require user approval for submissions and purchases, and keep provenance for pages it reads.

---

## 11. Verification, security and trust model

### 11.1 Verification must have independent dimensions

Do not reduce task success to a single green badge. Report:

1. **Scope integrity:** changes are within allowed paths/resources.
2. **Change integrity:** tests were not deleted/weakened without justification; lockfiles and configs are reviewed.
3. **Functional validation:** declared tests/build/lint/type checks and exit codes.
4. **System validation:** integration across processes, databases, APIs, queues and services.
5. **Security validation:** secrets, dangerous permissions, dependencies, unsafe inputs and network policy.
6. **Scientific validation:** mathematical invariants, dataset/evaluation parity and experiment method.
7. **Reproducibility:** commit, environment, config, seed, dataset hash, model ID and commands captured.
8. **Evidence completeness:** every consequential claim links to a verifiable artifact.

Possible final statuses: `VERIFIED`, `VERIFIED_WITH_LIMITATIONS`, `PARTIALLY_VALIDATED`, `INCONCLUSIVE`, `FAILED`, `BLOCKED`, `CANCELLED`. The backend—not the model—assigns the status.

### 11.2 Test integrity is necessary but not sufficient

Current AST checks can catch some deleted tests, skip decorators, simple tautologies and obvious tolerance increases. They cannot reliably prove that an assertion remains meaningful. Tests can be weakened through helper changes, fixture manipulation, monkeypatches, changed expected data, altered test discovery or indirect control flow.

Add layered defenses:

- Diff and AST comparison of tests, fixtures and test configuration.
- Critical test set protected by baseline hash or protected policy.
- Detect changed pytest configuration, xfail/skip marks, collection filters, environment flags and test selection.
- Mutation testing for selected critical properties.
- Run baseline tests against the base snapshot and candidate tests against the patch under matched environment.
- Independent reviewer checks whether new tests actually fail on the old buggy behaviour.
- Never call the AST verifier an absolute “anti-gaming proof.” Report rule coverage and known blind spots.

### 11.3 Threat model upgrades before wider execution

| Threat | Required mitigation |
|---|---|
| Malicious repository code reads host files | Rootless container/microVM, no home mount, no ambient secrets |
| Symlink/path traversal | Canonical path policy plus mount namespace; validate after resolution and during operations |
| Process escapes/can't be killed | Cgroup-scoped process tree and cleanup verification |
| Fork bomb/resource exhaustion | cgroups PID/memory/CPU/IO and wall-clock supervisor |
| Network exfiltration | Deny by default, allowlist proxy, DNS/egress policy and log |
| Secret exfiltration | Scoped secret broker, no secrets in task env or mounted host files |
| Malicious package install | Disposable environment, pin/lock, package provenance and network egress controls |
| Prompt injection in repository/docs/web | Treat all retrieved content as untrusted data; tool gateway authority remains external to the model |
| Malicious MCP/connector response | Schema validation, capability allowlist, strict connector scopes and approval gates |
| Forged evidence bundle | Optional external signature; immutable CI identity; provenance chain |
| DB/artifact tampering | File permissions, append-only event records where feasible, backup, signatures for export |
| Cost/runaway loops | Per-task token/model/tool/time/compute budgets and kill switch |

### 11.4 Approval gates

Require human approval for:

- modifying paths outside pre-approved scope;
- installing dependencies or enabling network access;
- accessing private external data or sending source code to a hosted model;
- running long/expensive compute;
- changing infrastructure or database state;
- creating external issues/comments/PRs;
- merging, releasing, deploying or publishing artifacts;
- deleting user data or project resources.

An approval should be one-time, scoped, expiring and bound to a task/resource/action hash. A cryptographic signature can protect token integrity, but HMAC alone is not a complete authorisation system. Policy still needs issuer identity, expiry, replay protection, target scope and revocation.

---

## 12. CLI and UX: proposed commands for the next product stage

The current CLI command families are real and useful:

`init`, `doctor`, `project`, `repo`, `snapshot`, `index`, `inspect`, `search`, `symbol`, `ask`, `qa`, `issues`, `events`, `config`, `model`, `schemas`, `db`, `research`, `map`, `task`, `verify`, `experiment`, `memory`, `worker`.

The following commands are **proposed target commands**, not claims that they already exist. Add them as vertical slices with backend services and tests.

### 12.1 Workspace and environment

```bash
maya workspace create my-engineering-workspace
maya workspace attach repo ./backend --kind git
maya workspace attach repo ./frontend --kind git
maya workspace attach dataset ./data --kind local-dataset
maya workspace show
maya workspace graph

maya env detect
maya env inspect
maya env resolve --profile dev
maya env create --profile dev --runtime rootless-container
maya env lock
maya env doctor
```

### 12.2 Terminal and runtime

```bash
maya runner doctor
maya runner profiles
maya runner start --profile rootless-container --task task-123
maya runner status
maya terminal open --runtime runtime-123
maya terminal exec --session session-123 -- python -m pytest
maya terminal logs --session session-123
maya terminal stop --session session-123
maya runner export --run run-123 --out run-bundle.tar.zst
maya runner import run-bundle.tar.zst
```

### 12.3 Agent and task control

```bash
maya task plan task-123
maya task approve task-123 --gate workspace-write
maya task run task-123
maya task watch task-123
maya task pause task-123
maya task resume task-123
maya task cancel task-123
maya agent trace task-123
maya agent budget task-123
maya agent roles
maya agent policy explain task-123
```

### 12.4 Tools and connectors

```bash
maya tools list
maya tools inspect process.exec
maya tools policy show
maya connector list
maya connector add github --scopes repo:read,issues:read
maya connector test github
maya connector revoke github
maya model list
maya model route explain --task-type architecture
maya model budget show
```

### 12.5 Research, verification and experiments

```bash
maya research search "efficient attention mechanism"
maya research add --doi 10.xxxx/example
maya research extract paper.pdf
maya research claims paper-123
maya research compare paper-123 paper-456
maya map build --task task-123
maya verify plan task-123
maya verify run task-123
maya verify export task-123 --format bundle
maya experiment plan --task task-123
maya experiment run --manifest experiment.yaml
maya experiment compare baseline-123 candidate-123
maya experiment report candidate-123
```

### 12.6 UX screens

Build the web experience only after application services and event contracts are stable. The first useful UI should have:

1. **Workspace overview:** attached resources, environments, status and recent tasks.
2. **Task workbench:** objective, acceptance criteria, plan DAG, budgets and approvals.
3. **Live execution:** current step, session logs, tool calls, resource use and cancel/pause.
4. **Change review:** diff, file scope, test modifications, security findings and reviewer comments.
5. **Research view:** papers, cited evidence, claims and claim-to-symbol mapping.
6. **Experiment view:** manifests, environment, metrics, confidence intervals and artifacts.
7. **Evidence report:** clear verified/partial/unverified claims with downloadable artifacts.
8. **Connectors and policies:** provider connections, permission scopes, data sharing and budget controls.

Do not build decorative dashboards before the task timeline, approval state, logs, diff and evidence are correct. Those are the UI's trust surfaces.

---

## 13. Proposed implementation work packages (Part 2)

The order below is intentional. Each package has exit criteria. Do not start ten packages in parallel.

### WP0 — Correctness and claims reconciliation (P0)

**Scope**
- Fix PDF fallback.
- Correct process-limit reporting and root-run behaviour.
- Clarify runner trust profiles and README wording.
- Reconcile V2.0 status claims with actual files/tests.
- Make test suite complete and repeatable; identify slow/hanging worker tests.

**Deliverables**
- Two fixes merged with regression tests.
- `execution_capabilities` report showing actual runtime enforcement.
- CI test matrix and release test summary.
- `IMPLEMENTATION_STATUS.md` mapping vision feature → module → test → status (`SHIPPED`, `PARTIAL`, `SPEC_ONLY`, `NOT_STARTED`).

**Exit:** no P0 defect in PDF fallback or resource-claim reporting; full suite green on a declared baseline environment; status document truthful.

### WP1 — Execution isolation v1 (P0)

**Scope**
- Add rootless OCI container backend and capability detection.
- Cgroup v2 process/memory/CPU enforcement where available.
- Read-only base source mount and separate writable overlay.
- Network deny by default; explicit allowlist proxy for approved dependencies.
- No host home, Docker socket, SSH agent or ambient credentials.
- Runtime cleanup and artifact collection.

**Exit:** adversarial runtime tests show host canary/secret inaccessible; process tree stopped; unsupported security capabilities cause blocking rather than false-green status.

### WP2 — Task contract and capability policy v2 (P0/P1)

Add:
- resource scope beyond repository paths;
- environment and runtime selection;
- capability grants (`file.read`, `file.write`, `process.execute`, `network.fetch`, `connector.github.read`, `experiment.run`, etc.);
- action risk levels;
- explicit side effects;
- approval binding to resource/action hashes;
- budget and deadline policy;
- rollback plan and expected evidence.

**Exit:** all tool invocations are checked by policy; role prompt cannot widen scope; replayed/expired approvals fail; all state changes are evented.

### WP3 — Tool gateway and process/session manager (P1)

Add typed tool registry, schema validation, idempotency, structured errors, process sessions/PTY, event streaming and cleanup. Initially integrate workspace read/write, Git, terminal, environment, build/test and artifacts.

**Exit:** one agent task can inspect a project, provision an isolated environment, run its build/test lifecycle, stream logs, cancel safely and export artifacts without ad hoc shell escape hatches.

### WP4 — Engineering Workspace data model (P1)

Add Workspace, ResourceAttachment, EnvironmentSpec, Runtime, Session, ToolInvocation and Artifact records. Keep current Project/Repo entities compatible through migrations. Support multiple related repositories and data/document resources.

**Exit:** a single workspace can contain a frontend, backend and dataset; the task graph can express dependencies across all three and evidence points to exact resource revisions/hashes.

### WP5 — Multi-language indexing and change-impact analysis (P1)

Add Tree-sitter adapters for TypeScript/JavaScript, Rust and Go first. Enrich build graph and configuration references. Add incremental re-indexing and index quality metrics. C/C++/CUDA is a subsequent adapter based on design-partner need.

**Exit:** cross-language fixture benchmark and manually reviewed symbol/edge gold set; incremental index updates are correct after rename, move, deletion and parser version changes.

### WP6 — Research Discovery and Claim Matrix v2 (P1)

Add scholarly search, source registry, passage citations, versioning, extraction confidence, hierarchical claims and code mapping. Add method-specific invariant templates and reproduction risk dimensions.

**Exit:** on a curated set of papers/repos, report citation precision, claim extraction quality, mapping precision/recall, unsupported-claim rate and reproduction-risk calibration. Include failures and ambiguous cases.

### WP7 — Model gateway and connected AI (P1)

Add chat/embedding/rerank/vision adapter interfaces, provider health, routing policy, token/cost budgets and local-model support. Add one or two user-selected external connectors with least-privilege scopes. Keep credentials out of execution runtimes.

**Exit:** model routing tests with mock providers; opt-in real-provider smoke tests; provider outage fallback; structured tool-call validation; user can revoke connector and see data-sharing scope.

### WP8 — Agent DAG, specialised roles and bounded repair (P1)

Promote the current bounded implementer into a workflow graph. Add role-specific task contracts and typed outputs, independent review context and role capability restrictions. No swarm marketplace.

**Exit:** a multi-step task can pause for approval, resume after restart, recover idempotently, delegate independent analysis steps and stop when evidence/budget gates fail.

### WP9 — Verification/QA v2 (P1)

Expand from Python AST test checks to multi-language test/config integrity, mutation probes for critical invariants, full diff and environment audit, integration/service health and structured security report.

**Exit:** seeded adversarial test edits (delete, skip, xfail, test filter, fixture mutation, tolerance weakening, altered helper) are detected or explicitly labelled blind spots. The reviewer cannot change files without a separate approval.

### WP10 — Experiment Lab and remote runner bundles (P1/P2)

Add user-owned runner export/import, dataset fingerprints, environment locks, hardware metadata, experiment queues, time/cost budgets, optional MLflow/TensorBoard adapters and richer experiment comparison.

**Exit:** baseline/candidate experiments can be rerun on user-controlled compute from a saved manifest and produce matching provenance. Imported results are clearly labelled as imported/unattested unless signed by a trusted runner.

### WP11 — Web/API control plane (P1)

Build thin API routes and UI using stable app services. Add durable event streaming and reconnect. Keep long-running work in workers; do not run training or shell commands in web request handlers.

**Exit:** user can create task, view plan, grant approval, observe live events, inspect diff, see evidence and export bundle entirely from UI. CLI remains first-class and can perform the same operations.

### WP12 — Advanced engineering connectors and safe autonomy (P2/P3)

Add CI, issue/PR, database, observability, infrastructure-plan and additional MCP integrations. Add bounded ablation search and automated hypothesis comparison only after execution and evaluation quality is established.

**Exit:** each connector has a security review, permission contract, audit event, revocation flow and integration test. Autonomy improvements must increase accepted task completion, not merely tool call counts.

---

## 14. Evaluation plan — prove the system works on engineering tasks

Do not use a single benchmark as proof of MayaWorkbench quality. Evaluate the complete system: model + retrieval + environment + tool gateway + orchestration + verification + UX.

### 14.1 Evaluation tracks

| Track | Example benchmark/tasks | What to measure |
|---|---|---|
| Repository understanding | CodeQL-style queries, custom repo Q&A gold set, architecture questions | Evidence precision, answer correctness, symbol recall, unsupported claims |
| Software engineering | SWE-bench Verified/Pro-style tasks, custom multi-repo issues | Resolved rate, accepted patch rate, regression rate, cost/task |
| Terminal/build engineering | Terminal-Bench-style tasks, build-failure fixtures | Task success, number of attempts, unsafe-action blocks, reproducibility |
| Research-to-code | Paper replication/reproduction rubric, custom brownfield ML changes | Claim extraction quality, method fidelity, mapping accuracy, experiment validity |
| ML engineering | MLAgentBench/MLE-bench-style tasks, training/debugging exercises | Score improvement, data leakage detection, run reproducibility, compute cost |
| Cross-language engineering | Full-stack repos, monorepos, native dependencies | Build/test success, dependency understanding, cross-resource impact accuracy |
| Security | Malicious repo fixtures, prompt injection, sandbox escape probes | Escape rate, secret access, policy violation rate, cleanup reliability |
| Agent reliability | Long tasks with interruption, provider failure, budget exhaustion | Recovery success, duplicate actions, state correctness, human interventions |

Re-check current benchmark versions and licensing before external claims. Maintain an internal fixed set of private evaluation tasks to reduce benchmark leakage.

### 14.2 Metrics

Core product metric:

**Verified task completion per unit of human supervision and total cost.**

Track:

- Task success on hidden acceptance tests.
- Accepted patch rate and post-merge regression rate.
- Time to first useful evidence.
- Human approval count and human intervention time.
- Unsupported claim/hallucinated evidence rate.
- Retrieval precision/recall and context tokens per successful task.
- Build/test pass and reproducibility rate.
- Security policy block accuracy, escape rate and cleanup reliability.
- Experiment protocol validity and independent reproduction rate.
- Model/tool/compute costs and latency.

Report medians and distributions, not just best runs. Compare with baselines under the same model, temperature/settings, tools, budgets, network access and task conditions. Include failed tasks.

### 14.3 Minimum internal release gates

Suggested targets to tune with a fixed evaluation corpus:

- Zero P0 data/execution escapes in the adversarial test suite.
- 100% of mutating/external actions pass through policy and durable audit event.
- 100% of terminal task claims supported by required deterministic evidence or labelled limitations.
- No hidden test/fixture weakening in protected baseline checks.
- At least 95% successful cleanup of deliberately interrupted process trees in supported runtime profiles; zero orphaned processes in the release suite.
- 100% reproducibility-manifest completeness for runs that are declared reproducible; missing fields block that status.
- Measurable improvement over the current baseline on cross-language retrieval and task-completion corpus before shipping the new parser layer.

Do not choose numerical task-completion targets without running the baseline first. Establish baseline numbers, then set a release threshold that reflects user value.

---

## 15. First 30 days: concrete sequencing

This is an implementation order, not a promise of elapsed time.

**Sprint A — Make truth reliable**
- Fix PDF fallback.
- Fix process-limit status and root behaviour.
- Add per-limit enforcement fields.
- Clarify `LOCAL_TRUSTED` wording and warnings.
- Run full suite in a clean declared environment; fix flakes/timeouts.
- Publish implementation status and test report.

**Sprint B — Secure execution vertical slice**
- Add a rootless container backend behind `ExecutionBackend`.
- Add cgroup capability probe and runtime profile selection.
- Add process/session lifecycle and cleanup.
- Add security fixtures for host path, secrets, network and process trees.

**Sprint C — Full project environment vertical slice**
- Detect Python and Node projects.
- Provision disposable environments from lockfiles.
- Run build/test/lint commands in isolated runtime.
- Capture environment hash, command, outputs and artifacts.

**Sprint D — Agent and evidence vertical slice**
- Extend TaskContract with resource scopes/capabilities and environment selection.
- Add typed plan DAG with planner → implementer → test engineer → reviewer.
- Support pause/resume/cancel/restart and approvals.
- Generate one evidence bundle for a multi-resource task (e.g., frontend + API).

After these four vertical slices are dependable, add extra languages, research connectors and remote compute.

---

## 16. Final priorities: what not to do next

Avoid these tempting but low-value moves:

1. **Do not add dozens of agents.** First prove that a single orchestrated workflow with role boundaries and typed artifacts outperforms the current single loop.
2. **Do not add unrestricted shell to the current host process.** Add execution isolation first.
3. **Do not claim PPR, symbolic math, HMAC approvals, signed proof bundles or remote runner mesh until those exact behaviours exist and tests prove them.**
4. **Do not build a vector database just to say “RAG.”** Measure hybrid retrieval against lexical/symbol graph baselines.
5. **Do not make “zero-cost compute” a product guarantee.** Free-tier providers can change quotas, availability, policies and hardware.
6. **Do not start with a large frontend.** Build the UI around durable task events and evidence, not around mock agent activity.
7. **Do not equate an AST test-integrity result with semantic correctness.** Add mutation probes and independent evidence.
8. **Do not auto-deploy, merge, destroy or alter live infrastructure.** Keep consequential external side effects approval-gated.
9. **Do not optimise for the count of tool calls, lines changed, agent messages or completed subtasks.** Optimise for verified user outcomes.

---

## 17. Final assessment

MayaWorkbench is moving in the right direction. The current implementation has the right foundation and a coherent initial wedge. The strongest assets to preserve are:

- commit-pinned repository snapshots;
- source-grounded retrieval and claims;
- explicit task contracts and bounded workflow state;
- isolated Git worktrees;
- scoped file tools and argv-based command execution;
- durable task events;
- deterministic verification and evidence records;
- reproducibility-aware experiment tracking;
- user-confirmed, provenance-aware project memory.

The next stage should convert the repo-centric CLI into an **Engineering Workspace** with a stronger execution plane, language-aware context, connected tools and role-based agent orchestration. This is the path to real complex engineering capability without losing the product's most important differentiator: trustworthy, reviewable outcomes.

**My readiness judgment:**

- **Ready for:** continued alpha development, controlled local use on trusted repositories, repository analysis, scoped Python-centric engineering tasks and experimentation with mock/provider-backed workflows where the user understands the limitations.
- **Not ready for:** untrusted repository execution on the host, claiming a secure sandbox, broad cross-language engineering autonomy, production infrastructure changes, multi-user hosted deployment, remote GPU/TPU dispatch, or a claim that the V2.0 named core engine is already implemented as described.
- **Next release should be judged on:** correctness of limits, verified execution isolation, a real environment/build/test loop, complete task recovery and clear evidence—not the number of new agent features.

The strategic direction is to become the place where a user can hand off a difficult engineering objective and receive a reproducible result, not just a plausible answer. The product is not there yet, but the existing codebase is a sound base to build that system on—provided the trust gaps and capability boundaries in this report are addressed in the right order.

---

## Appendix A — Fast audit reproduction commands

Run these from the extracted repository root in a clean environment. These commands are for reproducing the audit observations; they do not repair the defects.

```bash
# Compile source files
python -m compileall -q src

# CLI smoke test
PYTHONPATH=src python -m mayaworkbench.cli.main --help

# Collect the suite
python -m pytest --collect-only -q

# PDF fallback failure (requires optional pypdf dependency installed)
python -m pytest -vv tests/test_research_documents.py::test_pdf_content_stream_over_budget_is_skipped_with_a_warning

# Root process-count enforcement case (Linux behaviour depends on privileges/kernel)
python -m pytest -vv tests/test_runner_limits.py::test_process_limit_stops_a_fork_bomb

# Other runner limit tests
python -m pytest -vv tests/test_runner_limits.py -k 'not test_process_limit_stops_a_fork_bomb'

# Selected passing regression groups
python -m pytest -q tests/test_scale_guards.py tests/test_snapshots.py tests/test_storage.py tests/test_task_budget.py

# Full suite — do not treat a timeout/interrupted run as green
python -m pytest -q
```

### Audit execution observations

- `compileall`: passed.
- CLI help: passed.
- Selected scale/snapshot/storage/task-budget groups: 37 passed.
- Runner limit tests excluding fork-bomb: 3 passed.
- PDF fallback test: failed with `pypdf` raising `PdfReadError: startxref not found` instead of attempting the built-in parser.
- Fork-bomb process-count test: failed under root; 200 children were created despite configured maximum of 8.
- Broad suite: not observed completing green; execution windows expired after failure isolation. Do not report the whole suite as passed.

## Appendix B — Key source areas to modify

| Concern | Current source area |
|---|---|
| PDF extraction fallback | `src/mayaworkbench/research/pdf.py` |
| Runner resource limits | `src/mayaworkbench/workspace/runner.py` |
| Command/path security policy | `src/mayaworkbench/workspace/policy.py`, `src/mayaworkbench/workspace/tools.py` |
| Worktree isolation | `src/mayaworkbench/workspace/worktree.py` |
| Repository indexing/parsing | `src/mayaworkbench/indexing/` |
| Retrieval/context assembly | `src/mayaworkbench/retrieval/` |
| Research claims and DAG | `src/mayaworkbench/research/`, `src/mayaworkbench/mapping/` |
| Agent/tool loop and task lifecycle | `src/mayaworkbench/tasks/` |
| Evidence and integrity verification | `src/mayaworkbench/verification/` |
| Experiment manifests/statistics | `src/mayaworkbench/experiments/` |
| Project memory | `src/mayaworkbench/memory/` |
| Provider gateway | `src/mayaworkbench/models/` |
| Product truth and threat model | `README.md`, `docs/phase-6-report.md`, `docs/audit-phase-6.md`, `docs/threat-model.md` |


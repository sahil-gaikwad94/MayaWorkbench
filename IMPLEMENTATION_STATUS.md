# Implementation status (truth table)

This document maps vision claims to implemented code and tests. It exists to
prevent a specification, a phase report, or a blueprint pseudocode from being
described as a shipped capability. Status values:

- `SHIPPED` — implemented, exercised by an automated test, and honestly bounded.
- `PARTIAL` — a real but narrower implementation exists; the named capability does not.
- `SPEC_ONLY` — designed/specified in a vision document but not implemented.
- `NOT_STARTED` — no implementation.

Generated/verified on 21 November 2026. See `docs/test-matrix.md` for the
declared test environment and the latest full-suite result. The authoritative
product specification is `MayaWorkbench_MasterProductandEngineeringSpec.md`
(v2.1), which explicitly states that its requirements are specifications, not
claims of implementation.

## Product direction

| Vision claim | Actual implementation | Status |
|---|---|---|
| Research-to-Implementation as flagship wedge | repository + research + mapping + task + verification pipeline | `SHIPPED` (alpha scope) |
| One connected workflow, not four products | shared project/snapshot/evidence/task model | `SHIPPED` |
| North star: verified task completion per human supervision and cost | `evaluation/metrics.py` + `maya eval score`; only accepted+evidenced outcomes count | `tests/test_evaluation.py` | `SHIPPED` |
| Engineering Workspace (multi-repo, data, services, environments) | `workspaces`, `resource_attachments`, `environment_specs`, `workspace_sessions`, `evidence_items` + `WorkspaceService` + `maya workspace` CLI | `tests/test_workspace_v2.py` | `SHIPPED` (typed attachments and evidence; no connector/resource-specific adapters yet) |
| Hosted multi-user control plane / auth / tenancy | none | `NOT_STARTED` |
| General browser automation / agent marketplace / swarm | explicitly rejected by v2.1 | `NOT_STARTED` |

## Repository and context intelligence

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Immutable commit-pinned snapshots, worktree patch + hash | `repositories/snapshots.py` | `tests/test_snapshots.py` | `SHIPPED` |
| Python AST symbols/imports/calls with confidence | `indexing/python_parser.py`, `indexing/resolve.py` | `tests/test_indexing.py` | `SHIPPED` |
| JS/TS/Go/Rust symbol discovery | `indexing/language_adapters.py` heuristic declarations/imports/calls, LOW confidence, `HEURISTIC` provenance, `PARTIAL` coverage; repo map + search + `symbol show` | `tests/test_multi_language.py` | `SHIPPED` (heuristic, not AST) |
| C/C++/CUDA and other languages | `language_adapters.py` heuristic declarations for C/C++/CUDA, LOW confidence, HEURISTIC provenance; others lexical-only | `tests/test_multi_language.py` | `SHIPPED` (C-family), `PARTIAL` (exotic languages) |
| BM25 + exact + regex + symbol retrieval | `retrieval/search.py` | `tests/test_search.py` | `SHIPPED` |
| Context pack with revision, exclusions, token estimate | `ContextPack.snapshot_id`, `query`, `candidates_manifest`, `ambiguities`, deterministic exclusions | `tests/test_context_provenance.py` | `SHIPPED` |
| Embeddings / reranker / vector search | `retrieval/semantic.py`: deterministic hashing embeddings, lexical reranker, optional modes + Hit@k evaluation | `tests/test_semantic_retrieval.py` | `SHIPPED` (optional, not default) |
| Personalized PageRank (damped PPR) | `retrieval/semantic.py::ppr_scores` with optional `--ppr` ranking | `tests/test_semantic_retrieval.py` | `SHIPPED` (optional, normalized) |
| Submodular token-knapsack packer | deterministic submodular-coverage selection mode (`maya ask --selection submodular`) beside the greedy baseline | `tests/test_context_provenance.py` | `SHIPPED` (approximation, not optimality proof) |
| Content-hash incremental reindexing | durable per-file prepared-row cache keyed by indexer version + policy + path + content hash; rows still rebuilt atomically; snapshot-scoped reuse | `tests/test_incremental.py` | `SHIPPED` (same-snapshot reuse; new snapshots rebuild) |
| `RepoIntelligenceEngine` (v2.0 named class) | `indexing/engine.py` facade over index/search/retrieval | `tests/test_named_engines.py` | `SHIPPED` as facade |

## Research-to-Implementation

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Document registration, versioning, guarded URL/DOI fetch | `research/ingestion.py`, `research/fetch.py` (SSRF/address pinning) | `tests/test_fetch_pinning.py`, `tests/test_research_documents.py` | `SHIPPED` |
| PDF text extraction with bounded builtin parser + pypdf fallback | `research/pdf.py` (typed `EncryptedPdfError`, verified fallback) | `tests/test_research_documents.py` | `SHIPPED` |
| OCR for scanned PDFs | `research/ocr.py`: explicit opt-in Tesseract adapter, page confidence, separate SOURCE_OCR artifact, never exact quotes | `tests/test_ocr.py` | `SHIPPED` (requires external binaries) |
| Claim extraction (deterministic default, optional model), quote verification | `research/claims.py` | `tests/test_claims.py` | `SHIPPED` |
| Claim DAG with cycles/conflicts | `research/dag.py` (flat dependency/parent graph) | `tests/test_claims.py` | `SHIPPED` |
| Typed hierarchical claim graph (`PAPER_METHOD`, `VALIDATION_CLAIM`, …) | `ClaimKind` (6 kinds), `explicitness`, `target_resources`, `validation_ids`, `uncertainty`, `ClaimMappingStatus.NOT_CHECKED`, migration 0009 | `tests/test_typed_claims.py` | `SHIPPED` |
| Claim-to-code matrix with `PRESENT`/`PARTIAL`/`ABSENT`/`UNCERTAIN`/`CONFLICTING` | `mapping/rules.py`, `mapping/service.py`; `PRESENT` needs definition-level code evidence | `tests/test_mapping.py` | `SHIPPED` |
| `PaperToCodeMatrixEngine` (v2.0 named class) | `mapping/matrix_engine.py` facade over claim mapping + typed graph | `tests/test_named_engines.py` | `SHIPPED` as facade |
| Literature discovery (Crossref/OpenAlex/arXiv search, citation graph) | `research/discovery.py` with host allowlist, SSRF guard, immutable metadata + versions; `maya research search|versions` | `tests/test_typed_claims.py` | `SHIPPED` (metadata discovery; citation graph not implemented) |
| Reproducibility risk as multi-dimensional heuristic | `research/readiness.py` (6 weighted factors, `UNKNOWN` suppression) | `tests/test_readiness_memo.py` | `SHIPPED` |
| Method-specific mathematical invariant proofs | `verification/math_invariants.py`: 6 executable method-scoped property probes (softmax, causal, permutation, finite difference, normalization, conservation) | `tests/test_method_invariants.py` | `SHIPPED` (property probes, not general proofs) |

## Agent, workflow and tools

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Durable task contract, hash-bound single-use approvals | `tasks/`, `storage/migrations/0003*` | `tests/test_task_workflow.py` | `SHIPPED` |
| HMAC-signed approval tokens | optional HMAC-SHA256 artifact envelope from `MAYA_APPROVAL_KEY`; server enforce remains authoritative | `tests/test_hmac_approvals.py` | `SHIPPED` (HMAC not authority) |
| Typed capability grants and closed allowlist | `ToolCapability`, `TaskConstraints.capabilities`, `ToolGateway._require_capability`, `maya task create --capability` | `tests/test_task_capabilities.py` | `SHIPPED` |
| Contract-selected execution trust profile | `TaskConstraints.execution_trust`, `--trust`, `CommandPolicy.from_constraints` | `tests/test_task_capabilities.py` | `SHIPPED` (profiles themselves block when unavailable) |
| Resource scopes beyond repository paths | `TaskScope.workspace_id/resource_ids` validated against Engineering Workspace attachments; `--workspace/--resource` CLI | `tests/test_task_capabilities.py` | `SHIPPED` |
| 18-state orchestrator | 18 backend-owned `WorkflowState` values with CAS transitions (plan review, env provisioning, repair, evidence, draft publishing) | `tests/test_task_workflow.py` | `SHIPPED` |
| Detached worktree, scoped file tools, argv runner | `workspace/` | `tests/test_worktree.py`, `tests/test_tool_gateway_runner.py` | `SHIPPED` |
| One bounded implementer, typed tools, in-loop declared checks, bounded repair | `tasks/agent.py`, `tasks/inloop.py` | `tests/test_agent_intelligence.py` | `SHIPPED` |
| One orchestrator + specialised role DAG with typed handoffs | `tasks/roles.py`, `tasks/orchestrator.py`, backend-owned state, independent reviewer context, same-patch repair refusal | `tests/test_role_orchestrator.py` | `SHIPPED` |
| Many specialist agents / swarm | explicitly rejected by v2.1 | — | `NOT_STARTED` |
| Persistent process sessions (stream/replay/input/lifetime/cleanup) | `workspace/sessions.py`, 5 ToolGateway session tools, worker lifecycle cleanup | `tests/test_sessions.py` | `SHIPPED` (in-process; no cross-process reconnect) |
| PTY mode / cross-process session reconnect | PTY sessions with fd-safe cleanup + durable event record replay (`session_replay`); live process reattach across interpreters remains impossible | `tests/test_sessions.py` | `SHIPPED` (record replay), live reattach not applicable |
| Environment detection/provisioning (Python, Node, Rust, Go, …) | `workspace/environments.py` + `maya env detect|plan|doctor`; argv-only plans, no host install | `tests/test_environments.py` | `SHIPPED` |
| Tool families beyond workspace/Git/command/check | 18 typed tools incl. API/DB/log/notebook/env/session/skill families | `tests/test_advanced_tools.py` | `SHIPPED` |
| Execution trust profiles | `ExecutionTrust` enum; only `LOCAL_TRUSTED` implemented; others block | `tests/test_runner_limits.py`, `tests/test_runner_capabilities.py` | `SHIPPED` (profiles), `NOT_STARTED` (isolation backends) |
| `ExecutionBackend` protocol + `TrustedLocalBackend` | `workspace/backends.py` | `tests/test_execution_backends.py` | `SHIPPED` |
| `RootlessContainerBackend` (read-only source, writable overlay, cgroup limits, no home/socket/secrets, deny network) | `workspace/backends.py`; capability probe + fail-closed provision | `tests/test_execution_backends.py` | `SHIPPED` |
| Worker routing by contract trust profile | `BackendCommandRunner`; worker selects isolated backend or fails closed | `tests/test_execution_backends.py` | `SHIPPED` (backend path unit-tested with fake OCI client) |
| Remote microVM / user-owned GPU runner backends | `workspace/remote_backends.py`: allowlisted endpoint/executor, fail-closed provision/submit, no secret forwarding | `tests/test_remote_backends.py` | `SHIPPED` as adapters (execution needs user infrastructure) |

## Execution trust and resource enforcement (WP0)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Per-limit enforcement evidence | `runner.LimitEnforcement`, child-side `setrlimit` + `getrlimit` readback, derived `limits_enforcement` | `tests/test_runner_limits.py` | `SHIPPED` |
| Process-count limit (`max_processes`) | reported `unsupported`; `RLIMIT_NPROC` removed; isolated profiles blocked before spawn | `tests/test_runner_limits.py` | `SHIPPED` as truthful reporting; `NOT_STARTED` as enforcement |
| Fork bomb containment | verified cgroup v2 `pids.max` limiter with readback + cleanup; fail-closed when unavailable | `tests/test_cgroup_limits.py` | `SHIPPED` (requires cgroup-capable host) |
| Network deny-by-default | verified `unshare` probe, fail-closed before spawn | `tests/test_tool_gateway_runner.py` | `SHIPPED` |
| Runtime capability report | `workspace/capabilities.py`, `maya runner doctor\|profiles` | `tests/test_runner_capabilities.py` | `SHIPPED` |
| Host-canary / secret isolation fixtures for a container backend | adversarial argv assertions: no canary/home/SSH/socket/token in OCI flags, sanitized PATH | `tests/test_execution_backends.py` | `SHIPPED` (argv-level; real kernel escape test needs OCI host) |

## Verification, evidence and experiments

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Append-only verification attempts, deterministic gates | `verification/` | `tests/test_verification.py` | `SHIPPED` |
| Baseline vs candidate execution, regression classification | `verification/execution.py` | `tests/test_verification_evidence.py` | `SHIPPED` |
| Backend-assigned final status; model review advisory only | `verification/report.py::decide` | `tests/test_verification.py` | `SHIPPED` |
| Structural AST test-integrity checks | `verification/integrity.py` (structural warnings, explicit blind spots) | `tests/test_verification_evidence.py` | `SHIPPED` as heuristic; `NOT_STARTED` as semantic proof |
| Mutation probes / protected critical test baselines | `verification/mutation.py`, `verification/security_report.py`, `maya verify mutation\|security` | `tests/test_mutation_verification.py` | `SHIPPED` (property-level, not semantic proof) |
| Hash-verified evidence bundles | `verification/bundle.py` | `tests/test_verification_evidence.py` | `SHIPPED` |
| Optional signed bundle envelope / attestation levels | `verification/attestation.py`: HMAC-SHA256 over manifest root, `UNATTESTED`/`SIGNED_BY_USER`, key from `MAYA_SIGNING_KEY`, `maya verify attest\|check-attestation` | `tests/test_attestation.py` | `SHIPPED` (user-key HMAC only; no CI identity or `.maya-proof.json` protocol) |
| `SIGNED_BY_CI` / `.maya-proof.json` protocol | `verification/attestation.py::write_proof/verify_proof`, `maya verify proof|check-proof`, explicit CI identity | `tests/test_attestation.py` | `SHIPPED` |
| Pre-registered experiments, manifests, paired/Welch t, Hedges' g, Holm | `experiments/` | `tests/test_experiment_stats.py`, `tests/test_experiment_comparison.py` | `SHIPPED` |
| `ExperimentLabAuditor` named class | `experiments/auditor.py` facade over plan/compare/stats | `tests/test_named_engines.py` | `SHIPPED` as facade |
| Remote Kaggle/Colab/TPU runner bridge | user-owned runner adapter: allowlisted argv dispatch, poll, unattested result import; no free-tier promise | `tests/test_remote_runners.py` | `SHIPPED` (user-owned only) |
| MLflow/TensorBoard adapters | `experiments/adapters.py` deterministic JSON/TSV exports, `maya experiment export-adapter` | `tests/test_experiment_adapters.py` | `SHIPPED` |

## Memory and connected AI

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Provenance-aware, user-confirmed, commit-stale memory | `memory/service.py` | `tests/test_memory.py` | `SHIPPED` |
| Negative-result records | `MemoryKind.NEGATIVE_RESULT` with scoped details, expiry and similarity recall | `tests/test_experience_memory.py`, `tests/test_experiment_comparison.py` | `SHIPPED` |
| Experience learning / failure-signature retrieval | `memory/experience.py`, `maya experience record\|suggest`, confirmation/expiry gates, advisory suggestions | `tests/test_experience_memory.py` | `SHIPPED` |
| Provider-neutral chat gateway + OpenAI-compatible adapter | `models/` | `tests/test_qa.py`, `tests/test_cli.py` | `SHIPPED` |
| Chat/Embedding/Reranker/Vision/ToolConnector interfaces, routing, health | `models/capabilities.py`, `models/routing.py`, local deterministic embeddings/reranker, explicit unsupported vision, health tracking | `tests/test_model_platform.py` | `SHIPPED` (interfaces + routing; real vision adapter not claimed) |
| GitHub read-only connector, MCP integrations | `models/connectors/github.py` + `models/connectors/mcp.py`: verified contracts, local capability gate, provenance hashes, revoke; MCP is protocol only | `tests/test_model_platform.py`, `tests/test_mcp.py` | `SHIPPED` (GitHub read-only, MCP adapter) |
| Web/API control plane and UI | stdlib loopback JSON API + NDJSON event streaming + thin truth-surface UI (health, projects, tasks, approvals, events, evidence) | `tests/test_api_control_plane.py` | `SHIPPED` (thin local UI; no auth/multi-user) |

## Advanced capabilities added after WP12

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Advanced connectors (CI, database, observability, infra plan/diff) | `models/connectors/`: approval-gated draft-PR, read-only SQLite, redacted log tail, static infra validate/plan/diff, consent registry, revoke | `tests/test_advanced_connectors.py` | `SHIPPED` (no apply/destroy) |
| Persistent skills system | `skills/` (Skill, versioning, enable/disable, bundles, seeds, bounded executor), migration 0010, `maya skill` CLI | `tests/test_skills.py` | `SHIPPED` (procedures/check templates; no arbitrary execution) |
| Advanced tool families | `ToolGateway`: `api_request` (host-gated), `db_query` (read-only), `env_detect`, `logs_read`, `notebook_inspect`, `skill_list`, `skill_run` | `tests/test_advanced_tools.py` | `SHIPPED` (advanced tools require explicit capability grants) |
| Raw LLM / agent capabilities | `tasks/llm_capabilities.py`: task routing, bounded structured-output repair, tool reliability, usage accounting; wired into agent loop; `maya task reliability|explain-params` | `tests/test_agent_capabilities.py` | `SHIPPED` (scripted providers only) |
| Advanced memory governance | `memory/advanced.py`: conflict audit, retention/expiry, portable bundles with unconfirmed imports; `maya memory audit|retention|export-bundle|import-bundle` | `tests/test_memory_advanced.py` | `SHIPPED` |

## Reconciliation of the separate v2.0 blueprint

The separate V2.0 document claims `maya_workbench_engine.py` and
`test_maya_workbench_engine.py`, `PaperToCodeMatrixEngine`,
`ExperimentLabAuditor`, `RepoIntelligenceEngine` with damped PPR, a submodular
token-knapsack packer, HMAC approval tokens, an 18-state orchestrator, a remote
Kaggle/Colab bridge, a signed `.maya-proof.json`, a Veritas/Vexx symbolic bridge
and an "anti-gaming proof". **None of those named artifacts exist in this
repository.** Its status line should read "Target Architecture and Algorithm
Specification; implementation coverage varies by module", not
"Architecture-Verified & Core Engine Implemented". This table is the matrix the
audit requested; link future acceptance tests here as work packages land.

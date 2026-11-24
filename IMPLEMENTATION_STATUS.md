# Implementation status (truth table)

This document maps vision claims to implemented code and tests. It exists to
prevent a specification, a phase report, or a blueprint pseudocode from being
described as a shipped capability. Status values:

- `SHIPPED` — implemented, exercised by an automated test, and honestly bounded.
- `PARTIAL` — a real but narrower implementation exists; the named capability does not.
- `SPEC_ONLY` — designed/specified in a vision document but not implemented.
- `NOT_STARTED` — no implementation.

Generated/verified on 21 November 2026; reconciled with the 22 November 2026
audit-remediation work package, then with the 23 November 2026 Trustworthy
Runtime + Verification package (see the section at the end of this file and
`docs/wiki/audit-2026-11-23-remediation.md`). See
`docs/test-matrix.md` for the
declared test environment and the latest full-suite result. The authoritative
product specification is `MayaWorkbench_MasterProductandEngineeringSpec.md`
(v2.1), which explicitly states that its requirements are specifications, not
claims of implementation.

## Product direction

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Research-to-Implementation as flagship wedge | repository + research + mapping + task + verification pipeline | `tests/test_task_workflow.py` | `SHIPPED` (alpha scope) |
| One connected workflow, not four products | shared project/snapshot/evidence/task model | `tests/test_cli.py` | `SHIPPED` |
| North star: verified task completion per human supervision and cost | `evaluation/metrics.py` + `maya eval score`; only accepted+evidenced outcomes count | `tests/test_evaluation.py` | `SHIPPED` |
| Engineering Workspace (multi-repo, data, services, environments) | `workspaces`, `resource_attachments`, `environment_specs`, `workspace_sessions`, `evidence_items` + `WorkspaceService` + `maya workspace` CLI | `tests/test_workspace_v2.py` | `SHIPPED` (typed attachments and evidence; no connector/resource-specific adapters yet) |
| Hosted multi-user control plane / auth / tenancy | none | `NOT_STARTED` |
| General browser automation / agent marketplace / swarm | explicitly rejected by v2.1 | `NOT_STARTED` |

## Repository and context intelligence

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Immutable commit-pinned snapshots, worktree patch + hash | `repositories/snapshots.py` | `tests/test_snapshots.py` | `SHIPPED` |
| Python AST symbols/imports/calls with confidence | `indexing/python_parser.py`, `indexing/resolve.py` | `tests/test_indexing.py` | `SHIPPED` |
| JS/TS/Go/Rust symbol discovery | `indexing/language_adapters.py`: optional `TreeSitterAdapter` (real CST, HIGH confidence, `AST_SYMBOL`/`AST_IMPORT` provenance) activated only when `tree-sitter` extras are installed; deterministic regex/LOW/HEURISTIC fallback otherwise. The repository map reports the parser that actually ran. | `tests/test_multi_language.py` | `SHIPPED` (fallback), `SHIPPED` (tree-sitter path via injected fakes; no live grammar here) |
| C/C++/CUDA and other languages | `language_adapters.py` heuristic declarations for C/C++/CUDA, LOW confidence, HEURISTIC provenance; others lexical-only | `tests/test_multi_language.py` | `SHIPPED` (C-family), `PARTIAL` (exotic languages) |
| BM25 + exact + regex + symbol retrieval | `retrieval/search.py` | `tests/test_search.py` | `SHIPPED` |
| Context pack with revision, exclusions, token estimate | `ContextPack.snapshot_id`, `query`, `candidates_manifest`, `ambiguities`, deterministic exclusions | `tests/test_context_provenance.py` | `SHIPPED` |
| Embeddings / reranker / vector search | `retrieval/semantic.py`: deterministic hashing embeddings, lexical reranker, optional modes + Hit@k evaluation | `tests/test_semantic_retrieval.py` | `SHIPPED` (optional, not default) |
| Personalized PageRank (damped PPR) | `retrieval/semantic.py::ppr_scores` with optional `--ppr` ranking | `tests/test_semantic_retrieval.py` | `SHIPPED` (optional, normalized) |
| Submodular token-knapsack packer | deterministic submodular-coverage selection mode (`maya ask --selection submodular`) beside the greedy baseline | `tests/test_context_provenance.py` | `SHIPPED` (approximation, not optimality proof) |
| Content-hash incremental reindexing | durable per-file prepared-row cache keyed by indexer version + policy + path + content hash; rows still rebuilt atomically; snapshot-scoped reuse | `tests/test_incremental.py` | `SHIPPED` (same-snapshot reuse; new snapshots rebuild) |
| Change-impact analysis (affected files, tests and symbols from resolved edges) | `indexing/impact.py::ImpactAnalyzer` + `maya impact`; reverse-dependency BFS over import/call/inheritance edges, confidence propagation, bounded depth, honest Python-first lower-bound notes | `tests/test_impact.py` (10) | `SHIPPED` (Python-first static analysis; dynamic dispatch unresolved, so the set is a lower bound, not a proof) |
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
| One bounded implementer, typed tools, in-loop declared checks, bounded repair | `tasks/agent.py`, `tasks/inloop.py`; checks execute through the task's injected `TaskExecutionRuntime`, and an isolated task with no runtime records `BLOCKED` | `tests/test_agent_intelligence.py`, `tests/test_isolation_contract.py` | `SHIPPED` |
| One orchestrator + specialised role DAG with typed handoffs | `tasks/roles.py`, `tasks/orchestrator.py`, backend-owned state, independent reviewer context, same-patch repair refusal | `tests/test_role_orchestrator.py` | `SHIPPED` |
| Many specialist agents / swarm | explicitly rejected by v2.1 | — | `NOT_STARTED` |
| Persistent process sessions (stream/replay/input/lifetime/cleanup) | `workspace/sessions.py`, 5 ToolGateway session tools, worker lifecycle cleanup; processes are created by an injected `SessionBackend` | `tests/test_sessions.py`, `tests/test_container_sessions.py` | `SHIPPED` (host `LOCAL_TRUSTED` and container-owned `ROOTLESS_CONTAINER`; no cross-process reconnect; PTY refused in a container; runtime without a backend gets a typed refusal) |
| PTY mode / cross-process session reconnect | PTY sessions with fd-safe cleanup + durable event record replay (`session_replay`); live process reattach across interpreters remains impossible | `tests/test_sessions.py` | `SHIPPED` (record replay), live reattach not applicable |
| Environment detection/lockfile-aware planning (Python, Node, Rust, Go, Java, C/C++) | `workspace/environments.py`: uv/poetry/requirements/pyproject, npm/pnpm/yarn, Cargo, Go, wrapper-first Maven/Gradle, CMake/Make; content fingerprint; argv-only plans, no host install | `tests/test_environments.py` | `SHIPPED` (dry-run plans; no provisioning execution) |
| Tool families beyond workspace/Git/command/check | 18 typed tools incl. API/DB/log/notebook/env/session/skill families | `tests/test_advanced_tools.py` | `SHIPPED` |
| Execution trust profiles | `ExecutionTrust` enum; `LOCAL_TRUSTED` and code-enforced `READ_ONLY_ANALYSIS` available; isolated profiles block unless a real runtime is verified | `tests/test_runner_limits.py`, `tests/test_runner_capabilities.py`, `tests/test_tool_gateway_runner.py` | `SHIPPED` |
| `ExecutionBackend` protocol + `TrustedLocalBackend` | `workspace/backends.py` | `tests/test_execution_backends.py` | `SHIPPED` |
| `RootlessContainerBackend` (read-only source at `/src`, populated writable overlay at `/work`, cgroup args, no home/socket/secrets, deny network) | `workspace/backends.py`; no "enforced" claim from controller names; must be smoke-verified before provision | `tests/test_execution_backends.py`, `tests/test_runtime_workspace.py` | `EXPERIMENTAL` (fail-closed; no real OCI host in CI) |
| Worker routing by contract trust profile | worker provisions one `RuntimeWorkspace`, verifies the task's own container evidence, and injects one `CommandRunnerTaskRuntime` into file tools, commands, **in-loop checks**, sessions (capability-gated) and artifact/diff capture | `tests/test_runtime_workspace.py`, `tests/test_task_workflow.py`, `tests/test_isolation_contract.py` | `SHIPPED` (routing/boundary); `EXPERIMENTAL` (real isolation pending the live OCI gate) |
| Remote microVM / user-owned GPU runner backends | `workspace/remote_backends.py`: allowlisted endpoint/executor, fail-closed provision/submit, no secret forwarding | `tests/test_remote_backends.py` | `SHIPPED` as adapters (execution needs user infrastructure) |

## Execution trust and resource enforcement (WP0)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Per-limit enforcement evidence | `runner.LimitEnforcement`, child-side `setrlimit` + `getrlimit` readback, derived `limits_enforcement` | `tests/test_runner_limits.py` | `SHIPPED` |
| Process-count limit (`max_processes`) | `RLIMIT_NPROC` removed; verified cgroup v2 scope auto-created by the worker runner when a writable delegation exists ("max" readback rejected), otherwise `unsupported`; container `pids.max` only when smoke-verified | `tests/test_runner_limits.py`, `tests/test_cgroup_limits.py` | `SHIPPED` (truthful reporting + verified scope) |
| Fork bomb containment | cgroup v2 `pids.max` limiter with exact readback, live-pid drain and `rmdir` cleanup; fail-closed when unavailable | `tests/test_cgroup_limits.py` | `SHIPPED` (requires cgroup-capable host) |
| Network deny-by-default | positive loopback-unreachability probe used by both the runner and the canonical capability report; fail-closed before spawn | `tests/test_tool_gateway_runner.py`, `tests/test_runner_capabilities.py` | `SHIPPED` |
| Runtime capability report | one `ExecutionCapabilityReport` in `workspace/capabilities.py`, `maya runner doctor\|profiles`; controller presence != enforcement; OCI smoke opt-in | `tests/test_runner_capabilities.py` | `SHIPPED` |
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
| Experience learning / failure-signature retrieval | `memory/experience.py`, `maya experience record\|suggest`, confirmation/expiry gates, advisory suggestions; **wired into the live worker**: prior confirmed failures are attached to orientation and failed runs record unconfirmed negative results | `tests/test_experience_memory.py`, `tests/test_task_workflow.py` | `SHIPPED` (advisory, not prohibitive) |
| Provider-neutral chat gateway + three native adapters + local mode | `models/gateway.py` resolves the provider-specific key variable, permits a keyless loopback endpoint, counts tool schemas in its input bound and records the attempts that actually occurred; selectable `openai_compatible`, `anthropic`, `gemini` and `local_openai_compatible` providers | `tests/test_provider_adapters.py`, `tests/test_qa.py`, `tests/test_cli.py` | `SHIPPED` (fake transports; no live provider call here) |
| Chat/Embedding/Reranker/Vision/ToolConnector interfaces, routing, health | `models/capabilities.py`, `models/routing.py`, local deterministic hashing embeddings/reranker, explicit unsupported vision, health tracking | `tests/test_model_platform.py` | `SHIPPED` (interfaces + routing; hashing is lexical, not learned semantic) |
| Native tool calling, structured output and streaming | `models/base.py` (`ToolDefinition`, `ToolCall`, cache fields, `is_final` stream terminal), `openai_compatible.py` (tools/tool_calls, buffered + incremental SSE, `stream_options.include_usage`, strict-schema projection, parameter adaptation, `Retry-After`), `anthropic_adapter.py` (GA `output_config.format`, message-level cache breakpoints, merged tool results, refusal stop reason, buffered + incremental SSE), `gemini_adapter.py` (`responseMimeType`+projected `responseSchema`, function-response names, safety blocks, buffered + incremental SSE); shared bounded SSE/schema modules `models/sse.py`, `models/schema.py` | `tests/test_model_platform.py`, `tests/test_provider_adapters.py`, `tests/test_provider_wiring.py` | `SHIPPED` (adapters tested against documented wire shapes with fake transports; live gate `UNVERIFIED` without a key) |
| Pluggable embeddings + hybrid fusion | `retrieval/semantic.py`: `EmbeddingProvider`, fail-closed `ApiEmbeddingProvider`, `reciprocal_rank_fusion` over BM25/PPR/dense; opt-in, hashing fallback stays deterministic | `tests/test_semantic_retrieval.py` | `SHIPPED` (no learned semantic model claimed) |
| GitHub read-only connector, MCP integrations | `models/connectors/github.py` + `models/connectors/mcp.py`: real stdio JSON-RPC 2.0 transport, risk-tiered tool contracts, one-time hash-bound grants, revoke; MCP remains transport, not trust | `tests/test_model_platform.py`, `tests/test_mcp.py`, `tests/test_advanced_connectors.py` | `SHIPPED` (contract and transport; no live MCP server in CI) |
| Web/API control plane and UI | stdlib loopback JSON API + NDJSON event streaming; browser client uses `fetch()` line-buffered NDJSON (not SSE), working approve/reject and task controls, verification/evidence view, `/ui`→`/` redirect | `tests/test_api_control_plane.py` | `SHIPPED` (thin local truth surface; no auth/multi-user, no source-diff rendering) |

## Advanced capabilities added after WP12

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| Advanced connectors (CI, database, observability, infra plan/diff) | `models/connectors/`: approval-gated draft-PR, read-only SQLite, redacted log tail, static infra validate/plan/diff, consent registry, revoke | `tests/test_advanced_connectors.py` | `SHIPPED` (no apply/destroy) |
| Persistent skills system | `skills/` (Skill, versioning, enable/disable, bundles, seeds, bounded executor), migration 0010, `maya skill` CLI | `tests/test_skills.py` | `SHIPPED` (procedures/check templates; no arbitrary execution) |
| Advanced tool families | `ToolGateway`: `api_request` (host-gated), `db_query` (read-only), `env_detect`, `logs_read`, `notebook_inspect`, `skill_list`, `skill_run` | `tests/test_advanced_tools.py` | `SHIPPED` (advanced tools require explicit capability grants) |
| Raw LLM / agent capabilities | `tasks/llm_capabilities.py`: task routing, bounded structured-output repair, tool reliability, usage accounting; wired into agent loop; `maya task reliability|explain-params` | `tests/test_agent_capabilities.py` | `SHIPPED` (scripted providers only) |
| Advanced memory governance | `memory/advanced.py`: conflict audit, retention/expiry, portable bundles with unconfirmed imports; `maya memory audit|retention|export-bundle|import-bundle` | `tests/test_memory_advanced.py` | `SHIPPED` |

## 2026-11-22 final audit gaps (WP LSP-1 / WP MLFLOW-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Optional LSP client adapter | `indexing/lsp_client.py` stdio JSON-RPC (definition/references/hover), allowlisted binaries, sanitized env, fail-closed `unsupported`; `maya inspect lsp-*` | `tests/test_lsp_client.py` | `SHIPPED` (disabled by default; no live language server here) |
| Read-only MLflow tracking connector | `models/connectors/mlflow.py` with allowlisted https endpoint, env token, redacted errors, IMPORTED/UNATTESTED provenance, registry grant, `maya connector mlflow-link` | `tests/test_mlflow_connector.py` | `SHIPPED` (no live MLflow server here) |

## 2026-11-22 real-model eval harness (WP EVAL-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Versioned 25-task internal seed corpus | `evaluation/tasks.py` + `evaluation/gold/eval_tasks_seed.json`; stable corpus hash; five families | `tests/test_eval_live.py` | `SHIPPED` (internal seed, not external benchmark) |
| Verified-completion benchmark with hard spend cap | `evaluation/live.py` + `maya eval run/tasks`; scripted offline dry-run by default, live mode gated by `MAYA_LIVE_EVAL`, key and positive cap; rates computed from recorded evidence | `tests/test_eval_live.py` | `SHIPPED` (no live provider run here) |

## 2026-11-22 parallel discovery (WP ORCH-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Opt-in parallel read-only role discovery with ordered publication | `tasks/parallel.py` (pure bounded worker pool), `RoleOrchestrator._run_parallel`; workers compute payloads only, main thread publishes artifacts/events/state sequentially; default sequential | `tests/test_role_orchestrator.py` | `SHIPPED` (opt-in via `run.details.role_plan_parallel`) |

## 2026-11-22 research evidence quality (WP RESEARCH-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Claim-granularity, version-bound citations | `ClaimCitation` record + migration 0011 + store + `ClaimService.cite/verify_citations`; verbatim quote verification reuses the existing authority | `tests/test_research_evidence_quality.py` | `SHIPPED` |
| Typed cross-source disagreements with append-only resolution | `EvidenceDisagreement` record, store, `ClaimService.record_disagreement/list_disagreements/resolve_disagreement`, event types, CLI commands | `tests/test_research_evidence_quality.py` | `SHIPPED` |
| Paper status / correction / retraction alerts | `SourceMetadata` status fields + migration 0011 + `DiscoveryService.versions` `status_alerts` + `research versions --alerts` | `tests/test_research_evidence_quality.py` | `SHIPPED` |

## 2026-11-22 signed run bundles (WP RUNBUNDLE-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Sign/verify/import portable run bundles with named key | `sign_run_bundle`, `verify_run_bundle(key=...)` with structured attestation fields, key threading through service and CLI (`--sign`, `--verify-with`) | `tests/test_runner_bundles.py`, `tests/test_attestation.py` | `SHIPPED` (HMAC user key; no CI identity claim) |

## 2026-11-22 workbench UI package (WP UI-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Read-only task workbench endpoints | `GET /runs/{id}/timeline`, `/plan`, `/evidence`, `GET /experiments/{id}/comparison`: real service data, no new artifacts/events, typed 404/empty states | `tests/test_api_control_plane.py` | `SHIPPED` |
| Supervisable browser trust surfaces | Workspace, task overview, plan & approvals, execution timeline, evidence graph, diff, experiment comparator, working Approve action; `textContent`-only rendering, NDJSON stream intact | `tests/test_workbench_ui.py`, real browser smoke check | `SHIPPED` (local loopback, no auth) |

## 2026-11-22 evaluation-integrity package (WP R2I-2)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Frozen evaluation harness with hash-bound approved treatments | `verification/evaluation_integrity.py` + `evaluation.integrity` built-in check; declared via `CheckSpec.details["evaluation_paths"]`/`["approved_evaluation_changes"]` | `tests/test_evaluation_integrity.py` | `SHIPPED` |
| Additional method-specific property probes | `verification/math_invariants.py`: reduced-precision stability, gradient-flow contract, dataset-split parity, seed determinism; PASS/FAIL/SKIP discipline, no proof claim | `tests/test_method_invariants.py` | `SHIPPED` |
| Formal-constraint plugin boundary | `verification/constraints.py`: `ConstraintVerifier`, `ConstraintRegistry`; no verifier → `NOT_CHECKED`, never PASSED | `tests/test_constraints.py` | `SHIPPED` (no solver adapter shipped) |

## 2026-11-23 model/provider hardening

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Provider-correct structured output | OpenAI strict-schema projection (closed objects, all properties required) with `json_schema` → `json_object` → prompt-only degradation; Anthropic GA `output_config.format`; Gemini `responseMimeType` + projected OpenAPI-subset `responseSchema`; local `$ref` inlining | `tests/test_provider_adapters.py`, `models/schema.py` | `SHIPPED` (wire shapes only; no live provider) |
| Provider-correct tool use and caching | Anthropic merges consecutive `tool_result` blocks into one user turn and applies cache breakpoints to the requested message blocks (max 4); Gemini resolves `functionResponse.name` from the assistant's earlier `functionCall`; OpenAI keeps `tool_call_id` correlation | `tests/test_provider_adapters.py` | `SHIPPED` |
| Real streaming contract | Incremental `stream()` on all three adapters yields text deltas and one terminal response with usage/tool calls; `generate(stream=True)` assembles the same stream; SSE framing and byte/event/line limits are shared and bounded | `tests/test_provider_adapters.py`, `tests/test_model_platform.py` | `SHIPPED` (fake SSE; no live stream) |
| Honest provider failure accounting | Typed `auth`/`quota`/`context_length`/`content_filter`/`refusal`/`protocol` errors; bounded `Retry-After`; adapter parameter adaptation (token parameter, temperature, parallel tools, `stream_options`, response format); gateway records actual attempts and safe status/code details | `tests/test_provider_adapters.py` | `SHIPPED` |
| Truthful usage and cost | Cached prompt tokens and reasoning tokens are recorded separately; cache and reasoning pricing caveats are stated in `cost_basis` instead of invented prices | `tests/test_provider_adapters.py` | `SHIPPED` |

## 2026-11-22 launch-readiness package (WP LAUNCH-1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Real OCI boundary harness | `tests/integration/test_container_escape_live.py`: host/secret canary denial, argv flags, network none, pids honesty, cleanup; opt-in `MAYA_OCI_LIVE=1` | collected; skips with explicit reason without a verified runtime | `SHIPPED` as harness; `UNVERIFIED` on this host |
| Live model harness with hard spend cap | `tests/integration/test_model_live.py`: schema-valid output (streamed when the adapter supports it), declared tool calls; `MAYA_LIVE_MODEL=1` + the provider-resolved key (or a keyless loopback endpoint) + positive cap; fails closed without cap | collected; skips with explicit reason without gates | `SHIPPED` as harness; `UNVERIFIED` without a key |
| Release gate documentation | `docs/release-gates.md`: exact commands, pass/skip semantics, artifact fields | documentation | `SHIPPED` |
| Machine-readable release evidence | `release/evidence.py` + `maya release evidence`: real git/version/capability/test-report inputs, redaction guard, canonical hash, fail-closed on missing report | `tests/test_release_evidence.py` | `SHIPPED` |

## 2026-11-22 R2I evidence package (WP R2I-E1)

| Claim | Implementation | Test | Status |
|---|---|---|---|
| Four-tier reproducibility report (code found / implemented / validated / reproduced) | `research/reproduction.py` (pure, backend-assigned), `research/reproduction_service.py`, `REPRODUCIBILITY_REPORT` artifact + append-only event, CLI `maya research reproduction`, API `GET /research/reproduction/{run}` | `tests/test_reproduction.py`, `tests/test_reproduction_integration.py` | `SHIPPED` |
| Typed validation obligations derived from claims/checks | `research/obligations.py` | `tests/test_reproduction.py` | `SHIPPED` |
| Offline R2I evaluation (claim precision/recall/F1, span validity, mapping uncertainty) | `evaluation/r2i.py` + versioned seed corpus in `evaluation/gold/` + `maya eval r2i` | `tests/test_evaluation_r2i.py` | `SHIPPED` (internal seed corpus; no external benchmark claim) |

## 2026-11-22 audit-remediation package

| Fix | Implementation | Test | Status |
|---|---|---|---|
| Cgroup finite-limit readback, `rmdir` cleanup, worker opt-in wiring | `workspace/cgroups.py`, `workspace/runner.py` | `tests/test_cgroup_limits.py` | `SHIPPED` |
| One canonical, probe-driven capability report; honest rootless limits | `workspace/capabilities.py`, `workspace/backends.py`; per-limit `enforced` only when a live in-container readback equals the requested value | `tests/test_runner_capabilities.py`, `tests/test_execution_backends.py` | `SHIPPED` (mechanism + evidence plumbing); `LIVE_VERIFIED` not achieved here |
| `READ_ONLY_ANALYSIS` enforced by a read-only tool mask | `workspace/tools.py`, `workspace/task_runtime.py`, `worker.py` | `tests/test_runner_capabilities.py`, `tests/test_tool_gateway_runner.py` | `SHIPPED` |
| One `RuntimeWorkspace` for file tools, commands, checks and artifacts | `workspace/runtime_workspace.py`, `workspace/task_runtime.py`, `worker.py`, `verification/service.py` | `tests/test_runtime_workspace.py` | `SHIPPED` (host); `EXPERIMENTAL` (container isolation) |
| Sanitized immutable task snapshot; the worktree is never mounted | `workspace/sanitize.py`, `workspace/backends.py`, `workspace/runtime_workspace.py` | `tests/test_execution_backends.py`, `tests/test_runtime_workspace.py` | `SHIPPED` (unit-tested policy and mount list); `LIVE_VERIFIED` not achieved here |
| Matched isolated baseline and candidate runtimes | `verification/service.py::_baseline_runtime`, `verification/execution.py::execute_check(runtime=…)` | `tests/test_isolation_contract.py` | `SHIPPED` (parity evidence); a mismatch or unavailable baseline yields `BLOCKED` |
| Persistent sessions owned by the task container | `workspace/container_sessions.py`, `workspace/sessions.py` (`SessionBackend.build/spawn/terminate`), `workspace/backends.py` (retained container, generated terminator) | `tests/test_container_sessions.py` | `SHIPPED` (argv/evidence); `LIVE_VERIFIED` pending |
| Immutable environment spec (lockfile hashes, managers, image, services, secret refs) | `workspace/environments.py` (`EnvironmentSpec`, `resolve_spec`) | `tests/test_environment_provisioning.py` | `SHIPPED` |
| Provisioning only inside an isolated runtime, network-gated and inventoried | `workspace/provisioning.py`, `cli/environment_commands.py` (`env spec`, `env provision`) | `tests/test_environment_provisioning.py` | `PARTIAL` (no image build, package cache, service lifecycle or live fixture) |
| Local API request security contract | `api/validation.py::guard_local_request`, `api/server.py` | `tests/test_api_local_security.py` | `SHIPPED` (loopback Host, same-origin mutations, security headers, remote-bind opt-in; no user authentication by design) |
| Broken-checker classification and clean REVIEWING transition | `tasks/inloop.py`, `tasks/agent.py` | `tests/test_agent_intelligence.py` | `SHIPPED` |
| Native-language lockfile planning and CLI ergonomics | `workspace/environments.py`, `cli/environment_commands.py` | `tests/test_environments.py` | `SHIPPED` |
| NDJSON `fetch` client, real approvals, `/ui` redirect | `api/ui/index.html`, `api/server.py` | `tests/test_api_control_plane.py` | `SHIPPED` |
| Experience memory wired into the live agent context | `worker.py` | `tests/test_task_workflow.py` | `SHIPPED` |

## Reconciliation of the separate v2.0 blueprint

The separate V2.0 document claims `maya_workbench_engine.py` and
`test_maya_workbench_engine.py`, `PaperToCodeMatrixEngine`,
`ExperimentLabAuditor`, `RepoIntelligenceEngine` with damped PPR, a submodular
token-knapsack packer, HMAC approval tokens, an 18-state orchestrator, a remote
Kaggle/Colab bridge, a signed `.maya-proof.json`, a Veritas/Vexx symbolic bridge
and an "anti-gaming proof". The named `.py` files do **not** exist; the three
named engines exist only as honest thin facades (rows above), while damped PPR,
submodular packing, HMAC tokens, 18 states, runner bundles and signed proofs are
real, separately named implementations. There is **no** Veritas/Vexx symbolic
bridge and no general anti-gaming proof. Its status line should read "Target
Architecture and Algorithm Specification; implementation coverage varies by
module", not "Architecture-Verified & Core Engine Implemented". This table is
the authoritative matrix; link future acceptance tests here as work packages
land.

## 2026-11-23 — Trustworthy Runtime + Verification package

Audit source: `MayaWorkbench_Audit_and_Engineering_Upgrade_Roadmap_2026-11-23.md`.
Decision record: [ADR 0015](docs/adr/0015-trustworthy-runtime-and-verification-release.md).
Per-item status and remaining work: [docs/wiki/audit-2026-11-23-remediation.md](docs/wiki/audit-2026-11-23-remediation.md).
Release evidence: [docs/reports/test-run-2026-11-23.json](docs/reports/test-run-2026-11-23.json).

| Vision claim | Actual implementation | Test | Status |
|---|---|---|---|
| A task never sees the user's worktree | `workspace/sanitize.py` (single filter, versioned policy), immutable sanitized snapshot, one `/work` mount | `tests/test_execution_backends.py` | `SHIPPED` (unit); `LIVE_VERIFIED` pending |
| Limit claims come from inside the task container | `RootlessContainerBackend.verify_capabilities` (`pids.max`, `memory.max`, `cpu.max`, rlimits, egress, canaries, active fork/CPU tests) | `tests/test_execution_backends.py` | `SHIPPED` (probe + evidence); `LIVE_VERIFIED` pending |
| One runtime for the entire lifecycle | in-loop checks, invariant scripts and sessions receive the task runtime; an isolated session runs in the task container and a runtime that cannot own one refuses | `tests/test_isolation_contract.py`, `tests/test_container_sessions.py`, `tests/test_sessions.py` | `SHIPPED` |
| Baseline and candidate are comparable | `_baseline_runtime` parity (image, mount policy, resource policy, sanitizer policy, manifest) | `tests/test_isolation_contract.py` | `SHIPPED` (parity blocks on mismatch) |
| CPU time ≠ CPU quota | `cpu_time_seconds` (`cpu_time`) vs `cpu_quota` (`cpu_quota`), separate flags and evidence | `tests/test_runner_limits.py`, `tests/test_execution_backends.py` | `SHIPPED` |
| Session output framing is a documented contract | chunk stream + `stream_text()`; no line-per-event promise | `tests/test_sessions.py` | `SHIPPED` |
| Tests do not depend on globally installed optional packages | `sitecustomize` torch blocker | `tests/test_method_invariants.py` | `SHIPPED` |
| Local API cannot be driven cross-origin or rebound | `guard_local_request`, security headers, remote-bind opt-in | `tests/test_api_local_security.py` | `SHIPPED` (no user auth by design) |
| Environment provisioning / live evaluation / held-out corpora | — | — | `NOT_STARTED` (see the remediation page for the concrete blockers) |

## 2026-11-24 merge of the parallel engineering release (both implementations)

`main` carried a parallel commit (079b793) covering the same P0/runtime and
provisioning scope with a different internal API; this branch carries the
WP2–WP7 release. The merge keeps the more extensive runtime/provisioning/
impact implementations from `main` and ports the additive guarantees from
this branch (validation helpers, CPU-field aliases, capability probe evidence,
session backend, sanitizer helpers, in-loop isolation guard) plus the unique
packages that had no counterpart: `evaluation/providers.py`, `evaluation/r2i_grading.py`,
`research/equations.py`, `verification/method_adapters.py`, `retrieval/evaluation.py`,
`models/connectors/extended.py`, the R2I/impact/gold corpora, and the
X-ray/DAG/trace/report workbench surfaces.

| Package | Merged status | Evidence |
|---|---|---|
| P0 execution trust | `SHIPPED` (unit); `LIVE_VERIFIED` pending OCI host | `tests/test_execution_backends.py`, `tests/test_isolation_contract.py`, `tests/test_container_sessions.py` |
| WP1 determinism/security | `SHIPPED` | chunk framing, hermetic torch, canonical JUnit, local API guards |
| WP2 provisioning | `SHIPPED` (unit) | `tests/test_environment_provisioning.py`, `tests/test_provisioning.py` |
| WP3 provider/eval | `IMPLEMENTED`/`UNIT_TESTED`; live quality pending keys | `tests/test_eval_live.py`, `tests/test_provider_adapters.py` |
| WP4 R2I benchmark | contract + UI `SHIPPED`; held-out corpus pending experts | `tests/test_r2i_expansion.py` |
| WP5 repo intelligence | `SHIPPED` (gold sets + impact + retrieval eval) | `tests/test_repo_intelligence_gold.py`, `tests/test_impact.py` |
| WP6 connectors | `SHIPPED` (contracts; no live services) | `tests/test_extended_connectors.py` |
| WP7 workbench | `SHIPPED` (local loopback) | `tests/test_workbench_polish.py`, `tests/test_workbench_ui.py` |

Live OCI containment, real-provider task quality, expert-labelled held-out R2I
and hosted tenancy remain gated in `docs/release-gates.md`.

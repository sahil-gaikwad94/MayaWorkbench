# MayaWorkbench v2.1 — Master Product, Architecture & Implementation Specification

**Document status:** Authoritative pre-build specification  
**Date:** 16 November 2026  
**Supersedes:** MayaWorkbench Brief v1.0 and MayaWorkbench v2.0 Master Blueprint as separate build instructions  
**Audience:** Founder, product designer, principal engineer, coding agent, future contributors  
**Product category:** AI/ML Research-to-Implementation and Reproducible Engineering Workbench  
**Initial deployment philosophy:** Local-first, model-agnostic, bring-your-own-key (BYOK), user-controlled compute  
**Primary product wedge:** Turn a research or engineering objective plus an existing repository into a grounded plan, controlled implementation, and inspectable evidence package.

---

# 0. Instructions to the coding agent

This document is the single source of truth for the first build. Read it completely before changing code. It combines the product intent of v1, the technical direction of v2, and the product and architecture decisions made in this specification.

## 0.1 Your assignment

Build MayaWorkbench as a reliable research-engineering product—not as a generic chatbot, a thin model API wrapper, a collection of agent personas, or a benchmark dashboard.

The system must help a technically sophisticated user:

1. understand an existing AI/ML repository;
2. understand a research paper, method, or engineering objective;
3. connect the research requirements to the real repository;
4. identify what exists, what is missing, what is uncertain, and what conflicts;
5. plan a minimal, reviewable implementation;
6. make approved changes in an isolated working copy;
7. run relevant checks and, when appropriate, controlled experiments;
8. have the result independently reviewed;
9. receive an evidence-backed report and reproducible artifacts.

## 0.2 Rules for implementation

- Preserve the core product direction and the user's existing repository conventions. Inspect the repository before selecting or changing its structure.
- Treat the uploaded `Maya Workbench Engine.py` as a reference implementation of important domain concepts, not as proof that a complete application already exists. Reuse sound algorithms and types where appropriate; do not copy weak assumptions into production code merely because they exist in the reference file.
- Do not treat the accompanying test file as a product specification. Tests and evaluation must be designed from the acceptance criteria in this document.
- Do not rewrite the entire application in one pass. Build vertical slices with clear interfaces and observable outcomes.
- Do not introduce microservices, Kubernetes, Kafka, a proprietary vector database, a large agent framework, or many autonomous agents without a demonstrated requirement.
- Do not make the product dependent on one model provider, one model name, or one embedding service.
- Do not allow the model to grant itself permissions, certify its own success, change budgets, or mark a task complete by writing a natural-language claim.
- Do not call a paper-to-code mapping correct merely because a symbol with a matching name exists.
- Do not call a Git worktree a security sandbox. It is a source-control isolation mechanism, not a complete execution boundary.
- Do not claim a statistical test proves scientific truth or that a heuristic risk score is a calibrated probability.
- Do not silently omit unavailable checks, failed commands, unsupported languages, missing data, or uncertainty.
- Do not add dependencies without explaining why they are needed and how they affect install size, portability, licensing, and security.
- Keep expensive or irreversible features disabled unless the user explicitly enables them.

## 0.3 Required work style

Before implementation, produce a repository-aware implementation plan containing:

- current repository structure and existing code worth preserving;
- a gap analysis against this specification;
- architecture decisions and any deviations;
- phase-by-phase work breakdown;
- new and modified files;
- migration strategy for existing data;
- security assumptions;
- acceptance criteria for each vertical slice.

When an ambiguity is non-blocking, choose the simplest reversible option and document it. Ask the user only when a decision materially changes product behavior, data handling, security, or cost. Never invent completed functionality.

At the end of each implementation phase, provide: what changed, how it works, how to run it, known limitations, evidence from execution, and the next smallest useful increment.

---

# 1. Executive product decision

## 1.1 Product definition

**MayaWorkbench is an AI/ML research and engineering environment that connects technical research, repository understanding, implementation, experimentation, and independent verification into one observable workflow.**

It should feel less like chatting with an assistant and more like supervising a capable research-engineering system. The user sets the objective and constraints; MayaWorkbench investigates, plans, performs approved work, records what happened, and shows the evidence.

## 1.2 The core promise

> Give MayaWorkbench a research or engineering objective and an existing codebase. It will investigate the problem, understand the relevant parts of the system, map requirements to implementation, propose a defensible plan, make approved changes in an isolated workspace, run appropriate checks, and return a traceable evidence package—including failures and limitations.

This promise is intentionally bounded. MayaWorkbench must not promise that every paper can be reproduced, every research claim can be verified automatically, or every task can be completed without human judgement or sufficient compute.

## 1.3 Mission

Make serious AI/ML research and software engineering more accessible, systematic, reproducible, and trustworthy by turning capable AI models into inspectable research-engineering systems.

## 1.4 Long-term vision

Build an AI environment that can understand complex technical projects, investigate relevant literature, formulate hypotheses, reason about implementation trade-offs, modify systems, design and run experiments, learn from previous results, and contribute useful research and engineering outcomes—with humans setting direction and retaining authority over consequential decisions.

## 1.5 The product is not

At launch, MayaWorkbench is not:

- a new foundation model;
- a general-purpose chatbot;
- another generic coding copilot or IDE fork;
- an unrestricted autonomous employee;
- an open-ended agent swarm;
- a public untrusted-code execution service;
- a full distributed model-training platform;
- a paper summarizer that calls a summary a reproduction;
- a benchmark leaderboard wrapper;
- a promise of universal superiority over Cursor, Codex, Devin, OpenHands, Aider, SWE-agent, or any other system.

## 1.6 North-star outcome

**Accepted, verified task completion per unit of human supervision and total cost.**

A successful task must create useful work: a correct and reviewable patch, a grounded technical decision, a reproducible experiment, or an honest explanation of why the objective could not be achieved under the current constraints.

The north star must account for quality, human effort, wall-clock time, model spend, compute, and operational cost. Activity—agent messages, tool calls, tokens, files edited, or number of agents—is not progress by itself.

---

# 2. Product strategy and why this wedge is worth building

## 2.1 The underlying customer problem

AI/ML work is fragmented across papers, code repositories, documentation, notebooks, training scripts, datasets, model checkpoints, terminals, experiment trackers, and Git workflows.

A researcher trying to apply a new method to an existing model must coordinate several kinds of reasoning:

- scientific reasoning: what does the paper actually specify?
- mathematical reasoning: what equations, dimensions, constraints, and invariants matter?
- repository reasoning: where does the existing system implement related behavior?
- engineering reasoning: what is the smallest safe change?
- experimental reasoning: what result would demonstrate an improvement or falsify the hypothesis?
- epistemic reasoning: what has actually been verified, what is only inferred, and what remains unknown?

Current AI tools can help with individual steps, but the user still needs to coordinate the whole workflow and establish whether the final claim is justified. MayaWorkbench should connect those steps and preserve their evidence.

These pain points are product hypotheses, not proven market facts. Validate them with interviews and observed workflows before assuming demand.

## 2.2 Initial customer segments

### Primary beachhead: independent AI/ML engineers and research engineers

They work on model architectures, training and inference systems, evaluation pipelines, and existing ML repositories. They need repository understanding, implementation support, experiment discipline, and reliable technical evidence.

### Secondary segment: independent AI researchers

They study papers, reproduce methods, develop hypotheses, compare architectures, and work with limited compute. They benefit from source-linked research analysis, claim-to-code mapping, and reproducibility support.

### Secondary segment: open-source ML maintainers

They maintain complex ML libraries and research repositories. They need issue reproduction, grounded repository analysis, small patches, regression checks, and high-quality pull-request evidence.

### Later segment: research labs and small AI teams

They may need shared project knowledge, permissions, experiment history, data governance, and team review. Do not prioritise this segment until access control, security, retention, and collaboration are designed properly.

## 2.3 Core jobs-to-be-done

1. “Help me understand this unfamiliar ML repository before I change it.”
2. “Translate this paper into a technically faithful implementation checklist.”
3. “Show how the method maps to my existing code, and distinguish actual implementation from a superficial name match.”
4. “Identify the smallest safe changes needed to implement the method.”
5. “Make the change without silently breaking existing behavior.”
6. “Design a fair baseline-versus-candidate experiment.”
7. “Explain why an implementation or experiment failed, with supporting evidence.”
8. “Give me a reviewable patch and a report another engineer can inspect or reproduce.”

## 2.4 Positioning statement

For AI/ML researchers and engineers who need to turn complex technical goals into working, measured implementations, MayaWorkbench is a research-engineering workbench that connects paper and method understanding, repository intelligence, controlled implementation, experimentation, and independent verification. Unlike a chat-only assistant or a general coding agent, MayaWorkbench treats research fidelity, reproducibility, project memory, and evidence as first-class outputs.

## 2.5 What should differentiate it

The differentiated experience is not one algorithm. It is the coherent combination of:

1. **Research fidelity:** important paper claims link to the original source passage, equation, figure, or table.
2. **Brownfield repository reasoning:** method requirements map to actual files, symbols, call paths, configurations, tests, and uncertainties in an existing repository.
3. **Minimal, controlled engineering:** implementation changes are scoped, isolated, reviewable, and reversible.
4. **Domain-aware verification:** tests are supplemented by ML-specific invariants, numerical checks, and methodological review where appropriate.
5. **Experiment discipline:** baseline and candidate conditions, seeds, datasets, environments, metrics, and deviations are recorded.
6. **Evidence-backed reporting:** claims link to source references, code changes, commands, test outputs, run manifests, or human assessment.
7. **Persistent project knowledge:** validated architecture decisions, experiment outcomes, and prior failures are tied to source revisions and provenance.
8. **Cost-aware orchestration:** models and compute are selected according to quality requirements, data policy, availability, latency, and budget.

These are moat hypotheses. They become defensible only if MayaWorkbench repeatedly performs well on real workflows and users return to use it.

---

# 3. Product shape: one connected workflow, not four separate products

MayaWorkbench has four product modes, but they must share one project model, evidence system, task workflow, and user experience.

## 3.1 Mode A — Repository Intelligence

**Purpose:** Build a grounded, revision-aware understanding of an existing codebase.

User capabilities:

- attach a local Git repository or a repository archive;
- select a commit or explicitly include a captured working-tree patch;
- detect languages, frameworks, package managers, build systems, entry points, configuration, tests, and likely execution paths;
- search files, exact identifiers, symbols, and error messages;
- inspect classes, functions, methods, imports, references, and supported call relationships;
- create a repository map and architecture summary;
- ask natural-language questions and receive answers with file and line references;
- see uncertainty and unsupported parser coverage;
- preserve project instructions, conventions, and architecture decisions.

The first parser target should be Python because the product is AI/ML-focused and the reference engine already uses Python AST. Add Tree-sitter grammars for other high-value languages as demand and evaluation justify them. Unsupported syntax must be labelled as lexical-only or unparsed, never presented as fully understood.

## 3.2 Mode B — Engineering Task Execution

**Purpose:** Complete a scoped repository change safely.

Capabilities:

- convert the user's request into a structured Task Contract;
- identify assumptions, unknowns, constraints, allowed paths, and acceptance criteria;
- inspect repository history and related implementation areas;
- generate a dependency-aware plan;
- request approval for the exact consequential action;
- create an isolated Git worktree from a pinned commit;
- edit only permitted paths through a controlled tool gateway;
- execute approved commands in a separately controlled runner;
- capture command, exit status, duration, and redacted output;
- run baseline and post-change checks where feasible;
- create a minimal, reviewable diff;
- perform a read-only independent review;
- present the result for user approval before publication or merge.

## 3.3 Mode C — Research-to-Implementation (R2I)

**Purpose:** Translate a paper, method, or technical idea into a faithful implementation inside an existing repository.

Capabilities:

- ingest paper PDF, DOI, URL, Markdown, or user notes;
- preserve source version and page/section/equation/figure references where available;
- extract contributions, equations, assumptions, algorithms, hyperparameters, datasets, metrics, and limitations;
- distinguish explicit source content from inference and user-defined requirements;
- build a hierarchical Claim-to-Code matrix/DAG;
- map claims to repository symbols and code spans with evidence and uncertainty;
- identify missing or conflicting implementation behavior;
- score reproduction risk as a transparent heuristic, not a probability;
- produce a research memo and implementation plan;
- implement behind an approved scope;
- define and run appropriate smoke tests and experiments;
- record deviations from the paper and report what was and was not reproduced.

This is the flagship mode. It is the most important product workflow and should shape architecture decisions.

## 3.4 Mode D — Experiment Lab

**Purpose:** Manage controlled, reproducible ML experiments without building a heavyweight distributed training platform.

Capabilities:

- define experiment hypotheses and primary/secondary metrics;
- record run configurations, code revision, dataset fingerprint, model/checkpoint identity, environment, seeds, hardware, commands, and resource usage;
- execute local CPU smoke tests and user-controlled compute runs;
- compare baseline and candidate runs only when protocol differences are understood;
- ingest CSV/JSON/TensorBoard-compatible outputs as optional adapters;
- capture logs, metrics, artifacts, and failures;
- report uncertainty, variability, and deviations;
- retain failed experiments and negative results.

Do not build a proprietary experiment-tracking platform or distributed training scheduler in the first release. Start with immutable manifests, local execution, metric ingestion, and exports.

## 3.5 The core value loop

**Input:** repository snapshot + research paper/objective + constraints.  
**Investigation:** repository indexing + source-linked research extraction.  
**Reasoning:** claim mapping + uncertainty + technical plan.  
**Action:** user-approved code and command operations in an isolated workspace.  
**Evaluation:** deterministic checks + independent review + experiments where appropriate.  
**Delivery:** patch + evidence report + reproducibility bundle + preserved project memory.

The first “aha” moment should be an actionable repository/research gap report, not an animated agent or a wall of chain-of-thought. A short first-pass result is a target; a full, reliable scientific mapping may take longer.

---

# 4. Product principles and UX rules

## 4.1 Evidence over confidence

An agent statement that something works is not evidence. Evidence is a source passage, code reference, actual command result, experiment artifact, reproducible metric, or explicitly recorded human assessment.

## 4.2 Correctness over activity

Do not measure progress by agent messages, token usage, tool calls, changed-file count, or number of subagents. Progress must be tied to acceptance criteria and verified artifacts.

## 4.3 One strong workflow before broad capability

Do not build a general agent marketplace, voice interface, arbitrary browser automation, social network, or general business automation before the research-engineering workflow works.

## 4.4 Models are replaceable; the system is the product

Model providers are adapters. Core workflow state, tool permissions, evidence records, repository metadata, and experiment provenance must not be tied to one vendor's SDK or response format.

## 4.5 Human authority at consequential boundaries

Require explicit approval for write access, dependency installation, network access, access to sensitive files, long/expensive experiments, publication, pull-request creation, merge, deployment, deletion, and external data sharing.

Approvals must be action-specific, scope-specific, revocable, auditable, and enforced server-side or by the local control process—not by a prompt or disabled UI button.

## 4.6 Fail honestly

A task may finish as completed, completed with limitations, failed, blocked, inconclusive, or awaiting human decision. A model cannot convert an unexecuted check into a pass or a missing result into an improvement.

## 4.7 Reproducibility is a feature

Record enough provenance to understand or rerun work: repository revision, patch hash, source version, environment, dependencies, config, seeds, dataset/checkpoint identifiers, execution profile, tool versions, model identifier/settings, timestamps, metrics, and output artifacts.

## 4.8 Cost is part of quality

Record model tokens, estimated or provider-reported cost, execution time, compute resources, retries, artifact storage, and human review time. Hard budgets must be enforced outside the model.

## 4.9 The UI must explain consequential actions

Before approval, show what will happen, which paths or resources are affected, whether network access is requested, what may be sent to a model provider, expected cost/resource range if known, and how the user can stop or revert.

## 4.10 UX hierarchy

The user should see meaningful state and outputs, not every internal agent message. Make the task objective, current stage, important findings, pending approvals, blockers, diff, raw evidence, and final decision easy to inspect. “Stop,” “pause,” “revert,” and “export” must be obvious.

---

# 5. Primary user journey and screen design

## 5.1 Flagship journey

Example request:

“Read this paper about an efficient attention mechanism, inspect my existing transformer repository, implement the method, and determine whether it improves memory use and throughput without unacceptable quality loss.”

1. **Intake:** user selects a project/repository and supplies a paper or objective, plus compute constraints and target metrics.
2. **Snapshot:** system records a full commit SHA. If the working tree is dirty, it asks whether to use the committed revision only or capture a separate patch snapshot. It never silently mixes the two.
3. **Task Contract:** system records objective, scope, allowed/excluded paths, success criteria, budget, approval gates, and unknowns.
4. **Research:** system extracts source-linked method claims, equations, assumptions, and evaluation protocol.
5. **Repository investigation:** system indexes the pinned revision and finds model blocks, training/inference paths, configs, tests, and evaluation code.
6. **Claim mapping:** system links research claims to code spans/symbols, marks statuses, and explains uncertainty.
7. **Plan:** system proposes the smallest set of changes, validation strategy, experiment plan, dependencies, risks, and resource needs.
8. **Approval:** user approves write access and each additional consequential operation as needed.
9. **Baseline:** system records baseline checks or clearly states why a baseline could not be run.
10. **Implementation:** system modifies only the isolated worktree and records a patch.
11. **Verification:** runs appropriate static checks, tests, numerical invariants, security checks, and reviewer analysis.
12. **Experiment:** performs a smoke run first; full runs require separate approval and resource limits.
13. **Review:** a read-only reviewer analyses the original contract, full diff, logs, checks, and experiment artifacts—not just the implementer's summary.
14. **Delivery:** user receives the patch, report, manifests, artifacts, limitations, and a decision such as accepted, rejected, partially validated, inconclusive, or blocked.

Not every task requires a paper, experiment, or all workflow phases. Use task templates and optional stages rather than forcing every request through a single rigid linear pipeline.

## 5.2 Main application navigation

Use **Projects** as the primary container. A project represents an ongoing research or engineering effort, not just a chat thread.

Inside a project, provide:

1. **Overview:** project summary, attached repository, active tasks, recent runs, project health, decisions, and recent evidence.
2. **Repository:** file tree, search, symbols, architecture map, dependency map, entry points, tests, configurations, and commit snapshots.
3. **Research:** uploaded papers/documents, source passages, claims, equations, methodology, citations, and claim-to-code matrix.
4. **Tasks:** task contracts, plans, workflow timeline, approvals, current status, and results.
5. **Changes:** patch/diff, impacted files, test modifications, review comments, and apply/export controls.
6. **Experiments:** run queue, profiles, manifests, metrics, logs, comparisons, and artifacts.
7. **Memory:** project knowledge, past decisions, prior implementations, experiments, and negative results, all with source/provenance.
8. **Evidence:** final reports, verification records, and exportable bundles.
9. **Settings:** providers, keys, execution profiles, budgets, data sharing, retention, and project permissions.

## 5.3 First useful interface

Do not build a large dashboard before the workflow. Start with:

- CLI for local setup and deterministic developer operations;
- API for project/task orchestration;
- a small web UI for project overview, research mapping, task supervision, diff review, and evidence presentation.

A CLI-only alpha is acceptable for early internal proof, but the flagship product should eventually expose the Claim-to-Code matrix and task/evidence timeline visually. The web UI must not become a separate business-logic implementation; it calls the same API/domain services as the CLI.

---

# 6. Scope and prioritisation

## 6.1 P0 — First useful alpha

P0 is the smallest coherent product that proves the core concept.

- Local project creation and repository attachment.
- Pinned repository snapshots and dirty-tree handling.
- File/language detection and Python AST indexing.
- Lexical and symbol search with file/line references.
- Repository summary and grounded repository Q&A.
- Paper/Markdown/plain-text ingestion with source references; PDF text extraction can be added in the same phase if reliable tooling is available.
- Manual/model-assisted claim extraction with explicit/inferred distinction.
- Initial Claim-to-Code matrix with confidence/uncertainty and evidence references.
- Task Contract and inspectable plan.
- Model-provider adapter for at least one structured-output-capable provider and a local-compatible endpoint if feasible.
- Separate local execution worker for approved bounded changes.
- Isolated Git worktree for source-control safety.
- Tool gateway for file reads/edits and constrained command execution.
- User approval before writes and other consequential actions.
- Captured command logs, exit code, timeouts, diff, and artifact records.
- Basic deterministic verification and read-only model review.
- Structured evidence report.
- Local SQLite metadata store and local artifact storage.
- User can stop, inspect, export, and revert work.

P0 should support one high-value codebase profile well rather than advertise universal language support.

## 6.2 P1 — Strong public beta

- Tree-sitter support for additional prioritized languages.
- Better call/dependency graph and incremental index invalidation.
- External literature/documentation retrieval with source metadata.
- Improved claim DAG and claim-to-symbol mapping.
- AST test-integrity checks and suspicious diff alerts.
- ML-specific invariant probe templates (shapes, gradients, numerical stability, causality where applicable).
- Experiment manifests and local run profiles.
- Baseline/candidate metric ingestion and comparability checks.
- Negative-result memory and project knowledge controls.
- Pause/resume/cancel/recovery across process restarts.
- Exportable reproducibility/evidence bundles.
- GitHub integration with least-privilege access and user-approved PR creation.
- Better onboarding and task timeline UI.

## 6.3 P2 — Advanced research workbench

- Symbolic dimension/type constraints and optional Veritas/Vexx integration.
- More advanced paper equation extraction and method comparison.
- User-defined invariant libraries and forkable research blueprints.
- Multi-run statistical analysis, uncertainty intervals, multiple-comparison handling, and domain-aware experiment templates.
- Optional MLflow/TensorBoard adapters.
- User-owned remote runner bundles for Kaggle/Colab/cloud/local GPU environments.
- Bounded ablation search with strict budget and verification gates.
- MCP integrations where they provide real interoperability and preserve MayaWorkbench permissions.
- Collaboration, shared projects, and role-based permissions after product demand.

## 6.4 Explicit non-goals until validated

- custom foundation model;
- autonomous large-scale model training;
- unrestricted self-modification of MayaWorkbench production code;
- general-purpose AI employee for all industries;
- dozens of autonomous agents;
- public multi-tenant arbitrary code execution;
- automatic production deployment or merge;
- social network or agent marketplace;
- enterprise SSO/compliance before enterprise demand;
- mobile-native app;
- broad browser automation;
- complex billing before there are paying users;
- Kubernetes/microservices before scale requires them;
- proprietary experiment tracker or vector database;
- claims of “100x better” or “SOTA” without narrow, independent evidence.

---

# 7. System architecture

## 7.1 Architecture decision

Use a **modular monolith for the control plane, with a separately executed worker boundary for indexing and code execution**.

This keeps the product maintainable for a small team while ensuring that long-running or untrusted processes do not execute inside the API process. Split into additional services only when measured load, security boundaries, or team ownership require it.

## 7.2 Logical architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                    Web UI / CLI                               │
│ Projects · Repository · Research · Tasks · Diff · Experiments │
└──────────────────────────────┬───────────────────────────────┘
                               │ typed API / local IPC
┌──────────────────────────────▼───────────────────────────────┐
│                    Application API                            │
│ Auth · Project APIs · Task APIs · Approval UI · Event stream  │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│              Domain / Workflow Control Plane                   │
│ Task contracts · State machine · Policies · Budgets · Events   │
│ Project memory · Evidence registry · Task templates            │
└───────────────┬──────────────────┬────────────────────────────┘
                │                  │
┌───────────────▼──────────┐  ┌────▼────────────────────────────┐
│ Knowledge & Context       │  │ Model Gateway                   │
│ Repository index          │  │ Provider adapters               │
│ Research documents        │  │ Structured output validation    │
│ Claim-to-code graph       │  │ Routing / usage / cost           │
│ Retrieval / context pack  │  │ No direct tool authority         │
└───────────────┬──────────┘  └────┬────────────────────────────┘
                │                  │ proposals/tool requests
                └────────────┬─────┘
                             │
┌────────────────────────────▼─────────────────────────────────┐
│                  Tool Gateway / Policy Enforcement            │
│ Typed tools · Path policy · Approval checks · Budgets · Audit  │
│ Read · Search · Patch · Git · Execute · Research · Metrics     │
└────────────────────────────┬─────────────────────────────────┘
                             │ approved job specification
┌────────────────────────────▼─────────────────────────────────┐
│                    Worker / Runner Boundary                    │
│ Index worker · Local execution worker · Later isolated runner  │
│ Git worktree · Process limits · Network policy · Cleanup       │
└────────────────────────────┬─────────────────────────────────┘
                             │ signed/identified outputs
┌────────────────────────────▼─────────────────────────────────┐
│                  Verification & Evidence                      │
│ Checks · Review · Manifest audit · Artifact hashes · Reports   │
└────────────────────────────┬─────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────┐
│                     Persistence                               │
│ SQLite local / PostgreSQL hosted · Artifact filesystem/object  │
│ Append-only workflow events · Project memory · Observability   │
└──────────────────────────────────────────────────────────────┘
```

## 7.3 Separation of intelligence and authority

MayaWorkbench must separate two classes of capability.

### Probabilistic intelligence

Model-driven capabilities:

- paper interpretation;
- hypothesis generation;
- repository question answering;
- architecture reasoning;
- planning;
- implementation proposals;
- failure diagnosis;
- experiment interpretation;
- review suggestions.

### Deterministic authority and evidence

Ordinary software controls:

- authentication and authorization;
- approval records;
- state transitions;
- path and resource policies;
- budget enforcement;
- commit and artifact hashing;
- command execution and exit statuses;
- test invocation and result capture;
- experiment manifest validation;
- evidence linking;
- terminal-state decisions.

A model can propose an action. The tool gateway checks whether it is permitted. The runner executes it. The evidence system records what happened. The model does not get to bypass any of these boundaries.

## 7.4 Core technology choices

These are recommendations, not a requirement to use the newest version of every framework.

| Layer | Recommended direction | Reason and constraints |
|---|---|---|
| Core language | Python, supported modern release (minimum Python 3.11 unless a dependency requires otherwise) | Strong fit for AI/ML, research documents, orchestration, and the existing reference engine. |
| CLI | Typer or argparse | Local-first workflow and easy scripting. Choose one, avoid duplicate command layers. |
| API | FastAPI + Pydantic schemas | Python-native typed API; keep routes thin. |
| Data layer | SQLAlchemy or a small repository abstraction + Alembic migrations | Avoid direct SQL scattered across domain logic; support SQLite locally and PostgreSQL later. |
| Local database | SQLite with WAL where appropriate + FTS5 for lexical document search | Simple local install. WAL supports reader/writer concurrency on one host but is not suitable for a shared network filesystem. |
| Hosted database | PostgreSQL | Use when multi-user hosted control plane is actually needed. |
| Repository parsing | Python `ast` first; Tree-sitter for broader language coverage | Tree-sitter produces concrete syntax trees, supports incremental parsing, and remains useful with syntax errors. |
| Search | ripgrep/lexical exact search + symbol index + SQLite FTS5; semantic embeddings optional | Keep exact identifier search reliable; do not require embeddings for core usefulness. |
| Frontend | React + TypeScript; Next.js or Vite based on deployment needs | Build a focused project/task/evidence experience; do not tie domain logic to frontend framework. |
| Model adapters | Provider-neutral async interface and structured response schemas | Models/providers change; capture exact model/version/config and usage. |
| Execution | Separate local worker for trusted local alpha; stronger isolation before accepting hostile public workloads | Worktree is not a sandbox. Avoid shell access inside API process. |
| Artifacts | Local filesystem with content hashes; object storage later | Store metadata in DB and large logs/blobs as artifacts. |
| Observability | Structured logs first; OpenTelemetry when tracing across components is useful | Trace each task, model invocation, tool call, and runner job. |
| Optional integrations | GitHub API and MCP after core flows | Keep least privilege and route every action through policy enforcement. |

### Stack constraints

- Do not pin the design to a specific free-tier provider, exact model name, or free compute quota. Availability, limits, and terms change.
- SQLite WAL is appropriate for local single-host mode, not a multi-host shared database. See the SQLite WAL documentation.
- FTS5 is an optional built-in SQLite full-text search capability; check that the runtime SQLite build supports it.
- Tree-sitter is a parser infrastructure, not semantic program analysis by itself. Import resolution, dynamic dispatch, reflection, generated code, and runtime behavior still require separate analysis or model-assisted interpretation.
- Firecracker or an equivalent microVM boundary is a later hosted-execution option, not a zero-effort local dependency. Firecracker relies on KVM and requires host-level hardening such as seccomp, cgroups/namespaces, privilege dropping, and regular patching.
- MCP should be an integration protocol, not a permission model. MayaWorkbench must implement its own consent, authorization, tool scope, and data-sharing controls.

## 7.5 Dependency direction and service boundaries

Enforce this dependency direction:

```text
CLI / Web API
    ↓
Application services / use cases
    ↓
Domain entities, contracts, policies and state machine
    ↑
Infrastructure adapters (database, Git, filesystem, model APIs, runner, search)
```

The domain layer must not import FastAPI, SQLAlchemy session objects, a provider SDK, a frontend package, or runner-specific implementation code. Application services coordinate domain behavior through interfaces. Infrastructure adapters implement those interfaces.

The `ToolGateway` is the only route from model-generated tool requests to side-effecting tools. The model gateway must not receive raw filesystem or shell access. The API must not call `subprocess` to execute repository commands.

## 7.6 Canonical service interfaces

Use these as conceptual contracts. Adapt names to the existing repository, but preserve the responsibilities and direction of control.

```python
from typing import Protocol, Sequence

class RepositorySnapshotService(Protocol):
    def create_snapshot(self, repository_id: str, *, include_worktree_patch: bool = False): ...

class RepositoryIndexer(Protocol):
    def index(self, snapshot_id: str): ...

class RetrievalService(Protocol):
    def retrieve(self, snapshot_id: str, query: str, *, token_budget: int): ...

class ResearchIngestionService(Protocol):
    def ingest(self, project_id: str, source: object): ...

class ClaimMappingService(Protocol):
    def map_claims(self, task_id: str, snapshot_id: str): ...

class ModelProvider(Protocol):
    async def generate_text(self, request: object): ...
    async def generate_structured(self, request: object, schema: object): ...
    def capabilities(self) -> object: ...

class PolicyEngine(Protocol):
    def authorize(self, actor_id: str, action: object, context: object): ...

class ToolGateway(Protocol):
    async def invoke(self, actor_id: str, run_id: str, tool_name: str, arguments: object): ...

class ExecutionRunner(Protocol):
    async def submit(self, job_spec: object): ...
    async def stop(self, execution_id: str): ...
    async def get_status(self, execution_id: str): ...

class VerificationEngine(Protocol):
    async def verify(self, run_id: str, verification_plan: object): ...

class EvidenceService(Protocol):
    def create_report(self, run_id: str): ...
    def export_bundle(self, run_id: str, destination: str): ...
```

These are architectural interfaces, not a demand to use these exact Python `Protocol` declarations. Define concrete input/output schemas and typed return values before implementation. Avoid `Any` in production contracts where a stable schema can be defined.

### Required call sequence for a write action

1. The workflow planner proposes a typed action.
2. The application validates the task contract and current workflow state.
3. The policy engine evaluates actor, scope, action, budget, and required approval.
4. If approval is missing, create an `ApprovalRequest` and stop before side effects.
5. After approval, the tool gateway verifies that the approved action hash still matches the requested action.
6. The runner receives a bounded job specification, not arbitrary authority.
7. The runner returns execution metadata and artifact references.
8. The evidence service records the result and links it to the task, patch, source snapshot, and checks.

Never allow an agent role to skip a step in this sequence.

## 7.7 Core modules and ownership

```text
mayaworkbench/
  app/                    # application composition and startup
  api/                    # HTTP routes, auth dependencies, request/response schemas
  cli/                    # command line interface
  domain/                 # entities, enums, contracts, state machine, domain services
  projects/               # project lifecycle and project settings
  repositories/           # Git operations, snapshots, worktree metadata
  indexing/               # file scanners, parsers, file/symbol indexes
  retrieval/              # lexical search, symbol search, ranking, context packing
  research/               # document ingestion, source passages, claims, citations
  mapping/                # claim DAG and repo reconciliation
  tasks/                  # task contracts and task templates
  workflows/              # orchestrator, state transitions, retries, recovery
  models/                 # provider interface, adapters, router, usage accounting
  tools/                  # typed tool schemas and tool gateway
  policies/               # approvals, path policy, network policy, budgets
  execution/               # job spec, runner client, local worker adapter
  verification/            # deterministic checks, AST audits, reviewer input packages
  experiments/             # manifests, metrics, comparability and analysis
  memory/                   # project knowledge and negative-result records
  evidence/                 # evidence graph, report builder, bundle export/verify
  storage/                  # database repositories, migrations, artifact store
  observability/            # structured logs, traces, usage telemetry
  integrations/             # GitHub, MCP, MLflow, TensorBoard adapters as needed
  schemas/                  # shared JSON schemas / versioned external contracts
apps/
  web/                      # optional thin TypeScript frontend
worker/
  local/                    # separately launched local worker process
  remote/                   # later remote runner adapter
migrations/
evals/
  repo_qa/
  research_fidelity/
  engineering_tasks/
  experiment_reproducibility/
examples/
docs/
```

Do not create empty folders merely to match this tree. Create modules when a vertical slice needs them. Keep domain types independent of FastAPI, provider SDKs, and the frontend.

---

# 8. Repository intelligence architecture

Repository Intelligence is the first foundational subsystem. It must produce grounded, revision-aware facts rather than a generic AI-generated README.

## 8.1 Snapshot correctness

For every index and task, record:

- repository identity/path/remote where available;
- full resolved Git commit SHA;
- branch name as metadata, not as immutable identity;
- whether the working tree was clean;
- if dirty changes are included, a separately captured patch hash and list of included paths;
- submodule commit identities where applicable;
- indexer version and parser/grammar versions;
- index creation time.

The system must not label an index as commit-pinned if it actually read a mutable working tree without capturing its changes. Prefer indexing a detached worktree or Git object snapshot at the resolved commit. If the user explicitly chooses to include uncommitted changes, capture them as a distinct snapshot input.

## 8.2 File discovery and exclusions

Indexing must:

- honor `.gitignore` and project-level exclusions;
- skip `.git`, caches, virtual environments, generated build output, dependency trees, binaries, large datasets, and configured artifact directories by default;
- detect likely secrets and exclude them from indexing and model context;
- avoid following symlinks outside the repository root;
- set configurable per-file size and total index budgets;
- record files skipped and the reason;
- allow explicit user inclusion of otherwise excluded documentation or data when safe;
- avoid indexing `.env`, private keys, credentials, tokens, and secret directories;
- redact secret-like values from tool outputs and logs.

Secret detection is best-effort, not a guarantee. Never say “all secrets have been removed” solely because filename patterns matched.

## 8.3 Language parsing

Initial sequence:

1. Python AST parsing for classes, functions, methods, imports, annotations, decorators, and source ranges.
2. Tree-sitter grammars for additional priority languages based on user demand (likely TypeScript/JavaScript, Rust, Go, C/C++).
3. Lexical fallback for unsupported languages or parse failures.
4. Explicit parser coverage metadata per file.

Extract:

- file and module metadata;
- symbols and qualified names;
- signatures and source spans;
- imports and import aliases;
- class bases and decorators;
- function/method calls where statically visible;
- constants and configuration keys where feasible;
- test files and test function names;
- references to symbols and likely dependency edges.

Static call graphs are inherently incomplete in dynamic languages. Mark inferred relationships and parser uncertainty. Do not imply full runtime call graph coverage.

## 8.4 Index data model

At minimum:

- `RepositorySnapshot`: immutable snapshot identity, commit, tree hash, dirty patch hash, indexer version.
- `FileRecord`: path, blob/content hash, language, line count, byte size, status, parse coverage, test/config/document flags.
- `SymbolRecord`: stable symbol ID, qualified name, kind, file ID, start/end line, signature, docstring, parser confidence.
- `ImportRecord`: source file, imported module/symbol, alias, location.
- `ReferenceEdge`: source symbol/file, target symbol/file if resolved, edge type, provenance, confidence.
- `IndexIssue`: ignored/skipped file, parser error, unresolved import, unsupported language, secret exclusion, or size limit.

Content hash is the identity of a file's bytes; commit SHA is the identity of the repository revision. Do not conflate the two.

## 8.5 Retrieval strategy

Use a hybrid retrieval pipeline. Do not start with vector search as the only source of relevance.

1. **Exact and lexical retrieval:** file path, symbol name, identifier, exception text, configuration key, FTS/BM25.
2. **Symbol retrieval:** exact/qualified symbol search and signatures.
3. **Relationship expansion:** callers, callees, imports, bases, config-to-constructor links, tests, and nearby modules where statically available.
4. **Semantic retrieval:** optional embeddings for conceptual questions and documents.
5. **Git context:** recent history and blame only when useful and permitted.
6. **Direct source inspection:** before the model claims a code behavior or edits a file, retrieve the source span and related tests/config.

Search ranking must be explainable enough for debugging. Keep the raw retrieval candidates and ranking features in the task trace.

## 8.6 Personalized PageRank and graph ranking

The v2 PPR concept is worth retaining as an optional ranking feature. It can spread relevance through repository dependencies from query-matched seed files and help retrieve related code.

However:

- it should supplement lexical and symbol retrieval, not replace them;
- a file-level graph can lose important symbol-level detail;
- the graph quality depends on language parsing and dependency resolution;
- common identifiers and unresolved dynamic references can distort edges;
- PPR relevance is not proof of semantic correctness;
- graph ranking must be evaluated against simpler BM25/symbol baselines.

Start with a file/symbol graph and a transparent score combination. Add PPR only when it improves retrieval metrics on held-out repository questions. The exact formula and damping factor are configurable and versioned.

## 8.7 Context packing

Build context packs from:

- Task Contract and acceptance criteria;
- project instructions and relevant decisions;
- pinned commit and snapshot identity;
- repository map summary;
- ranked source snippets with file/line provenance;
- directly related tests and configurations;
- dependency/build metadata;
- relevant research claims and citations;
- prior validated facts and similar past failures;
- current diff and previous tool results when applicable.

Selection should account for query relevance, graph/relationship relevance, source authority, recency, duplicate/overlapping spans, token cost, and coverage of different required concepts.

Use a provider tokenizer when available. Otherwise estimate conservatively and leave headroom. Record the estimator/tokenizer version and target budget. If a function is too large, include signature/docstring and retrieve focused body spans rather than truncating blindly.

Do not call the packer “optimal” unless it is proven to optimise the stated objective. Greedy knapsack/MMR-style selection is an approximation. Keep selected and rejected candidates inspectable for debugging.

## 8.8 Incremental indexing

P0 may perform full indexing for small repositories. P1 should use commit diffs and per-file content hashes to re-index only changed files while rebuilding affected graph edges. The index must always identify its base commit and parser version. Do not claim incremental cache invalidation until persisted cache metadata and invalidation behavior actually exist.

---

# 9. Research Intelligence and Claim-to-Code system

This is the flagship differentiator. It must be grounded, inspectable, and honest about what a paper does not specify.

## 9.1 Document ingestion

Support in stages:

- Markdown and plain text first;
- PDF text extraction with page references;
- DOI and URL retrieval through a controlled fetch adapter;
- supporting source code/docs when supplied or explicitly retrieved.

For every document, store:

- document ID and version;
- title, authors/organisation, publication date where known;
- source URL/DOI/upload origin;
- file hash;
- extraction tool/version;
- page/section/chunk location;
- extraction warnings and missing pages;
- licensing or reuse notes if available.

Do not fabricate bibliographic metadata. If extraction fails or a page is unavailable, show it.

## 9.2 Claim categories

Each claim must be classified as one of:

- `EXPLICIT_SOURCE_CLAIM`: directly stated or shown in the source;
- `INFERRED_REQUIREMENT`: a reasoned interpretation needed to implement or evaluate the method;
- `USER_REQUIREMENT`: explicitly supplied by the user;
- `IMPLEMENTATION_DECISION`: a chosen engineering detail not specified by the paper.

These categories must not be merged. An inferred implementation choice must never be attributed to the paper.

## 9.3 Claim node schema

A Claim node should contain:

```json
{
  "claim_id": "claim-opaque-id",
  "document_id": "paper-version-id",
  "parent_claim_id": null,
  "claim_type": "EXPLICIT_SOURCE_CLAIM",
  "title": "Causal attention constraint",
  "statement": "The output at position t must not depend on future input positions.",
  "source_locations": [
    {"page": 4, "section": "Method", "equation": "Eq. 3", "quote_hash": "sha256:..."}
  ],
  "implementation_requirement": "Future tokens must not influence earlier output positions.",
  "depends_on": [],
  "target_paths": [],
  "target_symbols": [],
  "mapping_status": "UNCERTAIN",
  "mapping_evidence_ids": [],
  "validation_ids": [],
  "missing_details": [],
  "risk_factors": [],
  "created_by": "model-or-user",
  "review_status": "NEEDS_REVIEW"
}
```

Use schema validation, stable IDs, explicit versioning, and links to source spans. The JSON is illustrative; adapt field names to existing code conventions without changing semantics.

## 9.4 Claim mapping statuses

- `PRESENT`: strong evidence that the required behavior is already implemented; not necessarily experimentally validated.
- `PARTIAL`: some, but not all, required behavior appears implemented.
- `ABSENT`: repository inspection found no implementation evidence in the inspected scope.
- `UNCERTAIN`: source/code evidence is insufficient, ambiguous, or parser coverage is weak.
- `CONFLICTING`: observed implementation appears to contradict a requirement or source claim.
- `NOT_APPLICABLE`: claim does not apply to this repository/task.

Never infer `PRESENT` from name matching alone. A mapping should include the exact code span, explanation of semantic correspondence, counter-evidence or gaps, and validation method. If the model cannot provide a defensible mapping, use `UNCERTAIN`.

## 9.5 Hierarchical Claim DAG

Represent top-level contributions, subclaims, mathematical requirements, architecture components, configuration requirements, and evaluation requirements as linked nodes.

Use dependency edges to order implementation work. For example:

```text
Paper contribution
  ├── mathematical operator
  │     ├── tensor shape constraints
  │     ├── numerical/scaling rule
  │     └── gradient behavior
  ├── model integration
  │     ├── config/API integration
  │     └── checkpoint compatibility
  └── evaluation protocol
        ├── dataset/split
        ├── metrics
        └── baseline conditions
```

Validate missing dependencies and cycles. A cycle is an explicit modelling issue to resolve; do not silently break it.

## 9.6 Reproduction risk assessment

The v2 weighted coverage/risk concept may be used as an initial heuristic. It must:

- show contributing factors;
- separate missing paper details, code mapping uncertainty, data access, compute availability, licensing, and evaluation gaps;
- expose weights and score version;
- avoid calling the output a probability;
- allow “not enough information” as a result;
- never use a low score as proof that reproduction will succeed.

A suitable user-facing label is **Reproduction Readiness / Risk Assessment**, accompanied by a list of blockers and assumptions.

## 9.7 Research memo output

Before implementation, generate a concise but complete memo:

- research question;
- paper identity/version;
- central contribution;
- method breakdown;
- equations and assumptions;
- required implementation behavior;
- paper-specified vs inferred values;
- repository mapping;
- unresolved ambiguities;
- data and compute requirements;
- proposed validation;
- reproduction limitations;
- source references.

After implementation and experimentation, append observed deviations and results; do not overwrite the original source interpretation.

---

# 10. Task contracts, agents, and workflow orchestration

## 10.1 One controlled workflow, not an autonomous swarm

Use a workflow graph/state machine with role-bounded model calls. Roles describe responsibilities and permission levels; they do not require separate long-running agent services.

A single model may perform multiple roles. Different models may be used for research, implementation, or review when cost and quality justify it. Parallel work is allowed only when tasks are independent and outputs can be reconciled safely.

## 10.2 Initial roles

| Role | Responsibility | Write authority |
|---|---|---|
| Task Analyst | Convert user objective into scope, criteria, constraints, and unknowns | None |
| Research Analyst | Extract source-linked research findings | None |
| Repository Analyst | Inspect repository and build grounded architecture map | None |
| Technical Planner | Propose minimal implementation and validation plan | None |
| Implementer | Make approved edits | Scoped isolated worktree only |
| Verification Planner | Select deterministic checks and identify validation gaps | None; may propose checks |
| Independent Reviewer | Review task, source, diff, checks, and evidence | Read-only by default |
| Experiment Analyst | Interpret recorded metrics and deviations | None |
| Report Builder | Assemble evidence bundle and decision | Artifact creation only |

Do not create “agent personalities.” Define each role with input schema, allowed tools, output schema, stop conditions, and permitted side effects.

## 10.3 Task Contract

Every task must be represented by a validated contract. Example:

```json
{
  "task_id": "task-uuid",
  "project_id": "project-uuid",
  "task_type": "PAPER_TO_CODE",
  "goal": "Integrate the specified attention method into the existing model",
  "repository_snapshot_id": "snapshot-id",
  "scope": {
    "allowed_paths": ["src/", "tests/", "configs/"],
    "excluded_paths": ["secrets/", "production/", ".env"],
    "base_commit_sha": "full-40-character-sha",
    "include_working_tree_patch": false
  },
  "constraints": {
    "network_access": "DENY_BY_DEFAULT",
    "dependency_install": "APPROVAL_REQUIRED",
    "compute_profile": "CPU_SMOKE",
    "max_runtime_seconds": 1800,
    "max_model_tokens": 50000,
    "max_model_cost_usd": 0,
    "max_repair_attempts": 2
  },
  "acceptance_criteria": [
    "The required behavior is implemented or the blocker is explained",
    "Existing relevant behavior is preserved",
    "Relevant checks are executed and recorded",
    "The final diff is reviewed",
    "Every result claim links to evidence"
  ],
  "unknowns": [],
  "required_approvals": ["WRITE_ACCESS"],
  "created_at": "ISO-8601",
  "contract_version": "1.0"
}
```

Budgets must be enforced by application/runner code. A zero monetary budget means no paid usage, not that an external free-tier call is guaranteed to be available. The task contract must distinguish model cost, compute cost, wall-clock time, token budget, and resource ceilings.

## 10.4 Workflow states

Implement explicit states with validated transitions. Suggested states:

- `CREATED`
- `INTAKE`
- `CONTRACTED`
- `INVESTIGATING`
- `RESEARCHING` (optional)
- `INDEXING` (optional)
- `PLANNING`
- `AWAITING_APPROVAL`
- `BASELINE` (optional)
- `IMPLEMENTING` (optional)
- `VERIFYING` (optional)
- `AWAITING_EXPERIMENT_APPROVAL` (optional)
- `EXPERIMENTING` (optional)
- `REVIEWING`
- `REPORTING`
- terminal: `COMPLETED`, `COMPLETED_WITH_LIMITATIONS`, `FAILED`, `BLOCKED`, `CANCELLED`, `INCONCLUSIVE`, `NEEDS_HUMAN_DECISION`.

States represent lifecycle status, not every internal tool action. Use named workflow steps and append-only events for detail. A repository Q&A task should not be forced through implementation or experiment states. A paper-to-code task may require all relevant stages.

## 10.5 State transition rules

- Only the workflow service can mutate task state.
- Every transition must be validated against the task template and current state.
- Every transition is persisted transactionally with a workflow event.
- Terminal states cannot be reopened; create a new run linked to the prior run.
- A terminal success requires all mandatory gates for that task type.
- A skipped or not-applicable check must have a reason.
- A model response cannot directly change state.
- Recovery after process restart must use persisted state and idempotent jobs.
- Cancellation must stop or terminate active child processes and mark artifacts as partial.

## 10.6 Approval model

Approvals should be represented as durable records, not just an in-memory dictionary or HMAC token.

Each approval record should include:

- approval ID;
- task/run ID;
- actor/user ID;
- action/gate;
- resource and path scope;
- exact command or command class when relevant;
- network destination scope where relevant;
- budget/resource limits;
- hash of the action specification;
- issue and expiration times;
- one-time nonce or replay protection;
- status: pending, approved, denied, expired, revoked, consumed;
- audit event.

The approval must apply only to the action specification shown to the user. A changed command, changed path scope, larger budget, new network destination, or different target resource requires a new approval.

HMAC or digital signatures can protect approval artifacts across boundaries, but they do not themselves establish that a real human approved the operation. Authentication, authorization, replay protection, expiry, and server-side enforcement are still required. In local-first mode, the trusted local control process is the authority.

## 10.7 Bounded retry policy

- Set a configurable retry ceiling; default to no more than two targeted repair attempts after the first implementation.
- A retry must record a distinct diagnosis/hypothesis, changed plan, changed patch hash, and relevant new evidence.
- Do not retry a deterministic failure without new information.
- Do not allow the agent to weaken/delete tests just to pass.
- Stop after budget exhaustion, repeated failure, permission violation, unexpected destructive action, secret detection, or unclear root cause.
- Preserve failed attempts as project evidence.

---

# 11. Model gateway and agent contracts

## 11.1 Provider-neutral interface

Define a stable internal interface. Suggested operations:

- `generate_text(request)`
- `generate_structured(request, schema)`
- `stream(request)`
- `capabilities()`
- `health_check()`
- `estimate_tokens()` where supported

Model adapters must translate provider-specific behavior into normalized internal messages and usage records.

## 11.2 Required call metadata

Record where available:

- provider and exact model identifier;
- API/model version;
- structured-output or tool-call mode;
- sampling/reasoning settings where exposed;
- input/output token counts;
- provider-reported cost or clearly labelled estimate;
- latency;
- request/response identifiers;
- retry count;
- error class and sanitized message;
- data-handling/provider configuration.

Do not log raw API keys, secrets, or unnecessary full prompts containing private data. Apply per-project data-sharing settings before constructing requests.

## 11.3 Routing policy

Route by task and constraints, not only model leaderboard position:

- difficult paper/mathematical/architecture reasoning: strongest permitted model;
- code generation and tool use: model with demonstrated tool reliability;
- summarisation/classification: lower-cost model if quality is adequate;
- review: separate model/provider when feasible, or a genuinely independent review pass plus deterministic checks;
- embeddings: optional provider adapter;
- parsing, hashing, policy decisions, test execution, budget enforcement, and metric calculations: deterministic code.

A different model is not automatically an independent reviewer. Reduce shared failure modes by giving the reviewer raw evidence, separating its role and prompt, and keeping it read-only.

## 11.4 Structured outputs

Use versioned JSON schemas/Pydantic models for:

- Task Contract;
- research claim extraction;
- claim mappings;
- implementation plan;
- tool invocation arguments;
- verification plan;
- review findings;
- final report structure.

Validate every model output. Retry schema repair only within a small limit. Invalid output must not be executed or silently coerced into unsafe defaults.

## 11.5 Model memory and context

Do not rely on a long conversation transcript as the only memory. Build context from structured project state, validated facts, source references, previous task evidence, and the current task contract.

---

# 12. Tool gateway and execution architecture

## 12.1 All tool use goes through one gateway

Tools should be typed operations rather than arbitrary shell text wherever possible.

Initial tool families:

- `repo.list_files`
- `repo.read_file`
- `repo.search_text`
- `repo.search_symbols`
- `repo.get_symbol`
- `repo.get_related_symbols`
- `repo.get_git_diff`
- `research.read_source`
- `research.search_sources` (only through approved retrieval adapter)
- `workspace.apply_patch`
- `workspace.create_file`
- `workspace.delete_file` (approval and path checks)
- `git.status`
- `git.diff`
- `git.create_branch_or_worktree`
- `runner.execute_command`
- `runner.run_tests`
- `runner.collect_metrics`
- `artifact.store`
- `artifact.read`

Every tool definition includes:

- versioned name and schema;
- description of side effects;
- permission requirements;
- resource budget;
- timeout;
- idempotency behavior;
- audit event type;
- output schema;
- data sensitivity classification.

Tool descriptions and content returned by tools are untrusted data. A repository README, code comment, paper, webpage, or model output must never be allowed to change policy or grant permissions.

## 12.2 Avoid free-form shell where a typed operation is possible

Prefer direct file read/write APIs, Git library/CLI operations with argument arrays, test runner adapters, and package-manager adapters. If shell is required:

- parse and validate the requested command against an explicit allowlist/policy;
- execute without a shell (`shell=False`) where practical;
- use a known working directory;
- pass a minimal environment;
- apply timeout, process-group termination, CPU/memory/process/file-size limits where supported;
- disable network by default;
- capture stdout/stderr with size limits and secret redaction;
- record exact argv, environment allowlist, exit status, and duration;
- reject command chaining, shell expansion, command substitution, unapproved interpreters, and unapproved path references unless explicitly supported by a safe profile.

A regex blacklist over shell command strings is not a sufficient command security layer.

## 12.3 Git worktree behavior

Use detached worktrees or equivalent isolated source-control copies so the user's original checkout is not the default write target.

Required behavior:

- resolve and store full base commit SHA;
- capture source working-tree state before setup;
- create a unique per-task workspace path;
- reject path traversal and symlink escape;
- do not follow external symlinks;
- use exclusive workspace IDs and handle concurrent tasks;
- capture tracked and untracked changes;
- capture staged and unstaged diffs;
- preserve patch hash and file list;
- verify original HEAD and working tree remain unchanged;
- cleanup only the task-owned worktree and its artifacts;
- never silently overwrite an existing worktree belonging to another run.

Git worktree is not a security boundary. It does not contain processes, network access, resource consumption, or all filesystem/symlink attack paths.

## 12.4 Execution profiles

Define named profiles with explicit capabilities and limits:

- `READ_ONLY`: no writes and no process execution.
- `STATIC_ANALYSIS`: source parsing and permitted static tools only.
- `CPU_SMOKE`: short, bounded local tests with no network by default.
- `USER_APPROVED_LOCAL`: user-approved commands in the user's controlled local environment, with clearly documented limitations.
- `REMOTE_SANDBOX`: future isolated remote execution with stronger host/guest boundary.
- `GPU_EXPERIMENT`: future user-owned or managed runner with explicit resources and budget.

Do not let the model choose a more privileged profile. The user and policy layer choose it.

## 12.5 Local alpha security boundary

For the first trusted local alpha:

- keep API/control-plane and runner as separate processes;
- run the worker under the user's account, never as root;
- use a dedicated run directory with minimal mounts/permissions;
- set CPU, memory, process count, file-size, and wall-clock limits where supported;
- default to network disabled for executed code;
- do not pass provider API keys or source-control credentials into the code process;
- do not execute code from arbitrary public uploads automatically;
- disclose that local execution inherits some trust from the user's machine;
- require explicit opt-in to run repository scripts or install dependencies.

This is a local developer tool, not a hostile multi-tenant security guarantee.

## 12.6 Future hosted execution

Before allowing public users to run arbitrary repository code in a shared service, use a separately operated isolation boundary such as vetted microVMs or a managed sandbox. Design for:

- one run/tenant per isolated environment;
- no host filesystem or metadata-service access;
- egress allowlists and DNS controls;
- ephemeral short-lived credentials;
- read-only base images and constrained writable mounts;
- cgroups, namespaces, seccomp, privilege dropping, and kernel patching as applicable;
- time, CPU, memory, disk, process, and network quotas;
- artifact scanning and cleanup;
- abuse monitoring, incident response, and security review.

Firecracker is a possible future technology, not an assumed dependency: it requires KVM-capable hosts and deliberate host hardening. Do not promise “secure because Docker/worktree/HMAC is used.”

---

# 13. Verification, evidence and trust system

Verification is a core product subsystem. It is not a final LLM prompt.

## 13.1 Evidence must have multiple dimensions

Do not force all evidence into one ranked enum. Store at least three separate dimensions:

### Provenance

Where did the claim originate?

- repository file/symbol/line range;
- paper/document/page/section/equation;
- command output;
- test result;
- experiment artifact;
- model interpretation;
- human note or confirmation.

### Verification method

How was it checked?

- source inspection;
- static analysis;
- deterministic test;
- type/shape/numerical invariant;
- runtime observation;
- experiment;
- independent model review;
- human review;
- not checked.

### Outcome

What was the result?

- passed;
- failed;
- partially passed;
- blocked;
- skipped with reason;
- not applicable;
- inconclusive;
- unverified.

Each evidence item must include timestamp, tool/verifier version, relevant inputs, artifact references, and a stable content hash when applicable.

## 13.2 Verification gates

### Gate A — Scope integrity

- correct repository and commit;
- correct working-tree snapshot;
- changes limited to approved paths;
- exclusions preserved;
- approval checks passed;
- budgets respected.

### Gate B — Change integrity

- diff is relevant and reviewable;
- no unexplained generated files or formatting noise;
- dependencies changed only when justified;
- tests were not deleted, skipped, weakened, or bypassed without explicit review;
- no secrets were introduced;
- patch is reproducible/exportable.

### Gate C — Functional validation

- baseline result recorded where feasible;
- targeted tests executed;
- new tests cover intended behavior;
- existing relevant behavior preserved;
- static/type/lint checks included as appropriate;
- failures and unavailable checks are visible.

### Gate D — ML-specific validation

Where applicable:

- tensor dimensions and broadcast assumptions;
- gradient flow and non-NaN gradients;
- numerical range/stability;
- causal masking invariance;
- expected parameter updates;
- deterministic behavior under controlled conditions;
- checkpoint/config compatibility;
- model forward/backward smoke behavior.

Do not apply generic invariants where they do not fit the component. User-defined or paper-derived invariants must link to a source or explicit rationale.

### Gate E — Regression and security review

- inspect affected code paths and dependencies;
- inspect new commands and permissions;
- review filesystem/network behavior;
- inspect test modifications;
- review logs for secrets;
- identify backward-compatibility risks;
- reviewer findings are recorded by severity and evidence.

### Gate F — Experiment validity (only when experiments are run)

- baseline/candidate identity;
- dataset and split parity;
- evaluation script parity;
- resolved configuration comparison;
- model/checkpoint identity;
- seed pairing and count;
- hardware/software comparability;
- run exit status and raw metric provenance;
- deviations disclosed;
- statistical method is appropriate to the design.

### Gate G — Report integrity

- every meaningful claim links to evidence;
- completed/failed/skipped/blocked states reflect actual tool results;
- implemented is distinguished from validated;
- model assessment is not presented as deterministic proof;
- limitations and assumptions are stated;
- output artifacts can be inspected/exported.

## 13.3 Anti-gaming / test integrity

The v2 AST test-integrity verifier is a useful structural warning system. It may flag:

- test function deletion/rename;
- added skip/xfail decorators;
- inline skip calls;
- assertion count reductions;
- tautological assertions;
- tolerance inflation;
- altered fixtures or weakened test setup where detectable.

However, AST analysis cannot prove semantic equivalence or guarantee that no test has been gamed. It must produce findings such as `POTENTIAL_WEAKENING` with exact locations, not the absolute guarantee “the test suite is impossible to game.” Use clean baseline test artifacts, protected evaluation tests, diff inspection, and trusted runner invocation as additional defenses.

## 13.4 Independent review

The reviewer receives:

- original task contract and acceptance criteria;
- pinned base revision and candidate patch;
- relevant research claims and source citations;
- complete diff;
- baseline and candidate test commands/results;
- raw logs and structured verification artifacts;
- dependency/configuration changes;
- experiment manifests and metrics, if any;
- unresolved uncertainties.

The reviewer is read-only by default. It returns structured findings with severity, file/line evidence, reasoning, and recommended action. A separate model may be used, but “different provider” alone does not guarantee independence. Deterministic checks remain authoritative for mechanically testable facts.

## 13.5 Final task decisions

Use explicit decisions:

- `ACCEPTED`: all mandatory criteria passed with sufficient evidence for the task type;
- `COMPLETED_WITH_LIMITATIONS`: useful work completed, but one or more non-critical requirements remain unresolved;
- `REJECTED`: evidence shows the change is incorrect or regresses a required objective;
- `INCONCLUSIVE`: data or evidence is insufficient to decide;
- `BLOCKED`: permission, environment, dependency, data, or resource issue prevents progress;
- `FAILED`: execution failed without an acceptable deliverable;
- `NEEDS_HUMAN_DECISION`: multiple plausible paths or consequential ambiguity require the user.

Never infer acceptance solely from a model self-assessment.

---

# 14. Experiment Lab and scientific reproducibility

## 14.1 Experiment philosophy

MayaWorkbench should manage small and medium experiments before attempting to orchestrate full-scale distributed training. The first value is complete provenance and fair comparison, not a bespoke training scheduler.

## 14.2 Experiment manifest

An immutable manifest should contain:

- run ID and parent task ID;
- baseline/candidate/ablation role;
- repository identity, commit SHA, dirty patch hash;
- source paper/method version and claim IDs;
- dataset identity/version/fingerprint where lawful and feasible;
- train/validation/test split identifiers and preprocessing code hash;
- model architecture/checkpoint identity and hash where available;
- resolved configuration and configuration hash;
- evaluation script hash;
- dependency lockfile/container image digest;
- Python/framework/CUDA/driver versions where relevant;
- hardware profile and device details;
- seeds and determinism settings;
- exact command/entry point;
- redacted environment allowlist;
- start/end time, exit status, timeout/termination reason;
- CPU/GPU/memory/time usage where available;
- raw metrics, summary metrics, logs, and artifacts;
- deviations from reference protocol;
- manifest schema version and content hash.

If a dataset cannot be hashed fully, record the version/URI and the method/coverage of the available fingerprint. Do not imply a fingerprint proves dataset identity if only metadata was hashed.

## 14.3 Fair baseline/candidate comparison

Before comparison, explicitly compare:

- dataset and splits;
- preprocessing;
- evaluation code;
- model/checkpoint initialization;
- training and optimizer configuration;
- batch size and sequence length;
- hardware and software versions;
- seeds and pairing structure;
- runtime limits and stopping rules;
- metric definitions and directions.

If there are differences, classify them as expected/part of the intervention, acceptable nuisance variation, or a fairness violation. A mismatch should not always automatically invalidate a comparison if the changed dimension is the intended treatment; it must be explained and analysed.

Separate metrics such as quality, throughput, latency, peak memory, training cost, and energy. Do not collapse them into one score without an explicit utility function supplied by the user.

## 14.4 Statistical reporting

The paired Student's t-test and Hedges' g proposed in v2 can be useful for appropriate matched-run designs, but they are not universal pass/fail truth machines.

Requirements:

- choose the analysis method based on the experimental design and assumptions;
- preserve raw per-run observations;
- report sample count and pairing method;
- report means/medians and variability;
- include effect sizes and confidence intervals where appropriate;
- disclose distributional assumptions and small-sample limitations;
- account for multiple comparisons when many metrics or ablations are tested;
- distinguish exploratory results from confirmatory claims;
- support non-inferiority/equivalence only when margins were specified in advance;
- avoid declaring an improvement merely because p is below a threshold;
- never use a universal “three seeds means scientific significance” rule.

The system should explain the statistical method and caveats in accessible language. A result can be directionally promising but inconclusive.

## 14.5 Compute profiles

- `STATIC`: no model execution; parsing/analysis only.
- `CPU_SMOKE`: tiny batch/sequence/data smoke checks.
- `SMALL_VALIDATION`: limited data and bounded runtime.
- `FULL_EXPERIMENT`: user-approved hardware/time budget.
- `LARGE_TRAINING`: explicit hardware plan, resource budget, and human approval.

Before launch, estimate cost and time only when a measured environment or reliable user-provided hardware profile exists. Otherwise run a calibration step or state that no reliable estimate is available.

## 14.6 Runner portability

Define a runner adapter contract so execution can happen in:

- local CPU worker;
- user-owned local GPU;
- user-controlled notebook/runtime export;
- future remote sandbox;
- future managed execution service.

Kaggle, Colab, and other external services are optional providers, not guaranteed free infrastructure. Their current quotas, terms, APIs, and availability must be checked at the time of integration. MayaWorkbench should export reproducible bundles instead of depending on undocumented automation.

---

# 15. Memory and durable project knowledge

## 15.1 Four memory types

1. **Working memory:** current task plan, current workflow state, tool outputs, and temporary reasoning artifacts.
2. **Project knowledge:** architecture decisions, conventions, user-approved constraints, and validated facts.
3. **Research/experiment memory:** papers, claim mappings, hypotheses, manifests, measured results, and negative outcomes.
4. **Conversation history:** user/agent exchanges for continuity, subject to retention controls; not automatically treated as factual knowledge.

## 15.2 Memory record

Every durable memory item must contain:

- memory ID and project ID;
- category/type;
- concise fact/decision/result;
- source/evidence IDs;
- repository commit or document version where applicable;
- creation time and last-validated time;
- confidence or status;
- scope and expiry/staleness policy;
- author (user, tool, model proposal, human-confirmed);
- user visibility/edit/delete controls.

## 15.3 Memory safeguards

- Do not store secrets, provider keys, private tokens, or credentials in memory.
- Do not promote model hypotheses to validated facts because they are repeated.
- Mark repository claims stale when the relevant commit changes.
- Preserve failed attempts and their conditions; do not turn one failed experiment into a universal prohibition.
- Let the user inspect, correct, export, and delete project memory.
- Keep raw transcripts separate from structured facts.

---

# 16. Data architecture and persistence

## 16.1 Local-first storage

For local alpha:

- SQLite database under a documented MayaWorkbench data directory;
- WAL mode when suitable;
- FTS5 for document/file-text search if enabled in the SQLite build;
- local artifact directory using content-addressed filenames;
- explicit database migrations;
- configurable retention and export.

SQLite WAL supports concurrent readers and a writer on the same host, but all processes must share the same host and WAL is not suitable for a network filesystem. Use PostgreSQL for a multi-host hosted control plane. Do not share a live SQLite WAL database across remote workers.

## 16.2 Persistence principles

- Use stable UUID/ULID identifiers.
- Preserve immutable snapshots and evidence history.
- Use append-only workflow events; maintain a current-state projection for fast reads.
- Store large logs, diffs, PDFs, model artifacts, and binary outputs outside the relational DB; store metadata and hashes in the DB.
- Keep schema migrations versioned and reversible where feasible.
- Use transactions around state changes, approval consumption, and budget accounting.
- Make jobs idempotent or provide duplicate-execution protection.
- Define backup and export format before hosted deployment.

## 16.3 Core entities

| Entity | Purpose | Key relationships |
|---|---|---|
| `UserProfile` | Local or hosted user identity/settings | owns projects, approvals |
| `Project` | Ongoing research/engineering workspace | repositories, sources, tasks, memories |
| `Repository` | Git/local repository identity | snapshots, connections |
| `RepositorySnapshot` | Immutable source revision/patch state | files, index, tasks |
| `FileRecord` | Indexed file metadata and hash | symbols, refs, FTS entry |
| `SymbolRecord` | Parser-derived code symbol | file, references, mappings |
| `ResearchDocument` | Paper/document identity and version | source passages, claims |
| `SourcePassage` | Page/section/code/document excerpt reference | claims, evidence |
| `ResearchClaim` | Explicit/inferred/user requirement | DAG edges, mappings |
| `ClaimMapping` | Claim-to-code relationship and evidence | claim, symbols, validation |
| `Task` | User's requested objective | contract, workflow runs |
| `TaskContract` | Immutable scope/budget/criteria snapshot | task/run |
| `WorkflowRun` | One execution attempt | events, approvals, invocations |
| `WorkflowEvent` | Append-only state/activity record | run |
| `ModelInvocation` | Model call metadata/usage | run, provider, output artifacts |
| `ToolInvocation` | Tool call request/result/side effect | run, evidence |
| `ApprovalRequest` | Scoped human decision | run, action hash |
| `ExecutionSession` | Worktree/runner lifecycle metadata | run, artifacts |
| `PatchArtifact` | Diff and patch hash | snapshot, execution session |
| `VerificationCheck` | Deterministic check or review | run, evidence |
| `ExperimentRun` | Controlled run metadata | manifest, metrics, artifacts |
| `MetricObservation` | Raw/derived metric values | experiment, seed, metric definition |
| `EvidenceArtifact` | Logs, outputs, citations, manifests | run, source, claim, check |
| `ProjectMemory` | Durable knowledge and prior result | project, source/evidence |

## 16.4 Suggested minimum database tables

The coding agent should derive migrations from domain schemas, but the initial database should cover:

- `projects`
- `repositories`
- `repository_snapshots`
- `indexed_files`
- `symbols`
- `symbol_edges`
- `research_documents`
- `source_passages`
- `research_claims`
- `claim_dependencies`
- `claim_mappings`
- `tasks`
- `task_contract_versions`
- `workflow_runs`
- `workflow_events`
- `model_invocations`
- `tool_invocations`
- `approval_requests`
- `execution_sessions`
- `artifacts`
- `verification_checks`
- `experiment_runs`
- `metric_definitions`
- `metric_observations`
- `project_memories`

Do not store all domain relationships only as unvalidated JSON blobs. JSON can store versioned provider-specific metadata or flexible payloads, but core identities, relationships, lifecycle status, and queryable fields should be relational.

---

# 17. API and event contracts

## 17.1 API surface (initial)

### Projects

- `POST /projects` — create project.
- `GET /projects` — list accessible projects.
- `GET /projects/{project_id}` — project overview.
- `PATCH /projects/{project_id}` — update metadata/settings.

### Repositories

- `POST /projects/{project_id}/repositories` — attach local/Git repository.
- `POST /repositories/{repository_id}/snapshots` — create pinned snapshot.
- `POST /repositories/{repository_id}/index` — start indexing job.
- `GET /repositories/{repository_id}/snapshots/{snapshot_id}` — snapshot metadata.
- `GET /repositories/{repository_id}/map` — repository map.
- `GET /repositories/{repository_id}/search` — file/symbol search.
- `GET /repositories/{repository_id}/symbols/{symbol_id}` — symbol detail/source range.

### Research

- `POST /projects/{project_id}/documents` — upload/register paper/document.
- `POST /documents/{document_id}/extract` — extract text/sections/claims.
- `GET /documents/{document_id}` — source and extraction status.
- `GET /documents/{document_id}/claims` — claims and source references.
- `POST /tasks/{task_id}/claim-mappings` — create or revise a mapping.
- `GET /tasks/{task_id}/claim-matrix` — claim matrix/DAG.

### Tasks and runs

- `POST /projects/{project_id}/tasks` — create task.
- `GET /tasks/{task_id}` — task and current state.
- `POST /tasks/{task_id}/plan` — generate/refresh plan.
- `POST /tasks/{task_id}/runs` — start a workflow run.
- `GET /runs/{run_id}` — current run state.
- `GET /runs/{run_id}/events` — event stream.
- `POST /runs/{run_id}/approvals` — approve/deny a specific request.
- `POST /runs/{run_id}/pause` — pause when safe.
- `POST /runs/{run_id}/resume` — resume if state permits.
- `POST /runs/{run_id}/cancel` — cancel and terminate runner work.
- `GET /runs/{run_id}/diff` — patch/diff.
- `GET /runs/{run_id}/evidence` — evidence list/report.

### Experiments

- `POST /tasks/{task_id}/experiments` — create a proposed experiment.
- `POST /experiments/{experiment_id}/approve` — approve run budget/profile.
- `GET /experiments/{experiment_id}` — run status/manifest.
- `GET /experiments/{experiment_id}/metrics` — metric observations.
- `GET /experiments/{experiment_id}/artifacts` — logs/results/artifacts.

All endpoints must enforce project authorization, task/run state, and relevant policies server-side.

## 17.2 Event model

Emit durable structured events such as:

- `TASK_CREATED`
- `CONTRACT_CREATED`
- `SNAPSHOT_PINNED`
- `INDEX_STARTED`, `INDEX_COMPLETED`, `INDEX_FAILED`
- `SOURCE_INGESTED`, `CLAIMS_EXTRACTED`
- `CLAIM_MAPPING_CREATED`, `CLAIM_MAPPING_UPDATED`
- `PLAN_CREATED`, `PLAN_APPROVED`
- `APPROVAL_REQUESTED`, `APPROVAL_GRANTED`, `APPROVAL_DENIED`, `APPROVAL_EXPIRED`
- `WORKTREE_CREATED`, `PATCH_APPLIED`
- `COMMAND_STARTED`, `COMMAND_COMPLETED`, `COMMAND_TIMED_OUT`, `COMMAND_BLOCKED`
- `CHECK_STARTED`, `CHECK_COMPLETED`
- `EXPERIMENT_QUEUED`, `EXPERIMENT_STARTED`, `EXPERIMENT_COMPLETED`, `EXPERIMENT_FAILED`
- `REVIEW_COMPLETED`
- `ARTIFACT_CREATED`
- `TASK_COMPLETED`, `TASK_BLOCKED`, `TASK_FAILED`, `TASK_CANCELLED`

Events must include timestamp, project/task/run IDs, event schema version, actor/source, correlation ID, safe payload, and artifact references. Do not put secrets or full unredacted private source content in event payloads.

## 17.3 Streaming and recovery

Use Server-Sent Events or WebSocket only for live presentation; the database event log remains authoritative. A client reconnecting must be able to request events after a cursor and reconstruct current status. Do not rely on an in-memory websocket connection for task state.

Long-running jobs run in a worker process. FastAPI request handlers should enqueue jobs and return IDs rather than run training or shell commands inside request lifecycle.

---

# 18. Product reports and deliverables

## 18.1 Task completion report

Every finished task should produce:

1. task objective and scope;
2. repository snapshot and base commit;
3. research source identity, if applicable;
4. short executive result;
5. what changed and why;
6. files/symbols affected;
7. acceptance criteria and status;
8. checks/commands executed with raw result links;
9. test integrity and review findings;
10. experiment results, if run;
11. deviations and limitations;
12. unresolved risks/unknowns;
13. model/tool/resource usage;
14. patch and artifact links;
15. final decision and recommended next action.

## 18.2 Evidence bundle

Export a self-contained directory or archive containing:

```text
maya-evidence-bundle/
  bundle.json
  report.md
  task-contract.json
  repository-snapshot.json
  source-manifest.json
  claim-matrix.json
  implementation-plan.json
  patch.diff
  verification/
    checks.json
    review.json
    test-integrity.json
  experiments/
    manifests/
    metrics/
    logs/
  artifacts/
  hashes.sha256
```

Include only user-authorised files. Redact secrets and respect data licenses. `maya verify-bundle` should check hashes and schema consistency; it must not claim that the bundle proves scientific correctness merely because hashes match.

## 18.3 Evidence language

Prefer:

- “The repository contains a symbol named X at file Y, lines A–B.”
- “The model assessed this as likely implementing claim C; this remains unverified.”
- “Command Z exited with code 0 on commit SHA S.”
- “The recorded candidate run measured metric M under configuration C.”
- “The comparison is inconclusive because the sample is small / protocol differs.”

Avoid:

- “Guaranteed correct.”
- “Paper fully reproduced” without full protocol evidence.
- “No tests were gamed” based only on AST scan.
- “Statistically proven” without methodological qualification.
- “SOTA” without a fair, current, independently reproducible comparison.

---

# 19. Product evaluation and success metrics

MayaWorkbench must be evaluated as a complete system: model + retrieval + workflow + tool gateway + runner + verification + UI. A model's public benchmark score is not MayaWorkbench's score.

## 19.1 North-star metric

**Cost per accepted, verified task**, measured alongside human minutes required and task duration.

An accepted, verified task must meet task-specific acceptance criteria, preserve required invariants, contain reviewable evidence, and not conceal failed or skipped checks.

## 19.2 Product metrics

### Adoption and retention

- projects created;
- repositories indexed;
- time to first useful repository answer;
- claim matrices generated;
- tasks started/completed;
- repeat use per project;
- weekly active technical users;
- design partner retention;
- percentage of users who return for a second real workflow.

### Quality

- source-grounded repository answer accuracy;
- citation/path/line correctness;
- claim extraction precision and recall against expert-reviewed samples;
- claim-to-code mapping precision;
- accepted patch rate;
- regression rate;
- false-completion rate;
- verification evidence completeness;
- reviewer finding severity;
- unsafe action attempts blocked;
- task abandonment and human override rate.

### Efficiency

- median and p90 task duration;
- model tokens and cost per task;
- runner CPU/GPU time;
- retries per accepted task;
- artifact storage per task;
- human review minutes;
- total cost per accepted task.

### Experiment quality

- manifest completeness;
- baseline/candidate comparability status;
- artifact provenance completeness;
- rerun success rate;
- metric calculation correctness;
- percentage of improvement claims with raw observations and uncertainty.

## 19.3 Initial internal targets

These are provisional goals to validate, not product claims:

- Repository Q&A: target at least 90% citation-supported factual correctness on a curated 100-question suite before promoting the feature broadly.
- Citation integrity: no answer citation may point to a file/line range that was not retrieved from the indexed snapshot.
- Source integrity: no default write may affect the user's original checkout.
- Policy integrity: all write, network, dependency, and costly-run operations are subject to backend-enforced policy.
- Report honesty: every required check is represented as passed, failed, skipped, blocked, or not applicable with reason.
- Research fidelity: expert review of at least 10 curated methods before claiming reliable paper-to-code mapping.
- Engineering usefulness: compare MayaWorkbench against a same-model baseline on held-out tasks under comparable budgets; report accepted-task rate, review time, regression rate, and total cost.

Do not tune prompts and retrieval on the held-out evaluation set. Store exact system version, model identifier, prompt/schema version, tool versions, task input, and environment for every evaluation.

## 19.4 Benchmark families

Use external benchmarks only where they test a relevant capability. Possible references include SWE-bench for repository issue resolution, Terminal-Bench for tool/terminal work, MLE-bench for ML engineering, and paper reproduction evaluations such as PaperBench. They do not replace a MayaWorkbench-specific suite, and results are sensitive to task split, agent harness, tool permissions, model version, budget, and grading method.

Build `MayaBench` with:

- repository comprehension tasks;
- issue/engineering tasks;
- research fidelity and claim mapping tasks;
- ML invariants and numerical debugging tasks;
- experiment reproducibility tasks;
- policy/security compliance tasks.

Keep a private held-out set and report failures. Compare a baseline using the same model and equivalent tools/budgets wherever possible.

---

# 20. Security, privacy and licensing

Security is a product requirement from the first executable alpha because the system handles private code and can execute repository content.

## 20.1 Threat model

Threats include:

- malicious files/scripts in repositories;
- prompt injection in README, code comments, issues, papers, websites, or tool output;
- secrets placed in source files, logs, model contexts, or artifacts;
- malicious package install scripts and dependency confusion;
- filesystem traversal and symlink attacks;
- process escape and access to host resources;
- network exfiltration and cloud metadata access;
- resource exhaustion and fork bombs;
- forged experiment outputs;
- cross-project or cross-tenant data leakage;
- unauthorized PR, merge, deletion, or deployment;
- provider key leakage.

## 20.2 Minimum controls

- API/control plane never executes user repository code in-process.
- The runner has a separately controlled process boundary.
- Network is denied by default for code execution.
- Only scoped workspace paths are mounted writable.
- Do not inject model-provider keys or long-lived Git credentials into executed code.
- Keep secrets out of model context, project memory, logs, and experiment manifests.
- Apply least-privilege GitHub scopes and short-lived credentials.
- Require approval for dependency installation and outbound network.
- Treat all repository, paper, web, and tool content as untrusted instructions/data.
- Enforce resource and wall-clock limits.
- Use per-project authorization and audit trails.
- Redact sensitive information before logging or exporting.
- Allow the user to export and delete project data.
- State clearly which content is sent to which model provider or remote service.
- Do not train on customer repositories, documents, or data without explicit opt-in and clear terms.

## 20.3 Licensing and data rights

Before using a paper's code, dataset, model weights, or repository content, record the source and known license/terms. Do not assume public download means unrestricted redistribution or commercial reuse. Let the user know when licensing information is missing or incompatible.

## 20.4 Privacy modes

Support a clear mode distinction:

- `LOCAL_ONLY`: repository, documents, metadata, and artifacts remain local; no remote model calls unless the user changes the setting.
- `BYOK_REMOTE_MODEL`: only the context required for the selected model request is sent to the configured provider; disclose provider and data flow.
- `REMOTE_EXECUTION`: code/data are sent to the named runner; require explicit setup and approval.

Never describe BYOK as automatically private; the model provider still receives whatever content is sent to it.

---

# 21. Commercial strategy and go-to-market hypotheses

## 21.1 Start with technical users and a local-first core

Initial distribution should focus on:

- real demonstrations using public ML repositories;
- reproducible technical write-ups;
- GitHub releases and issue tracking;
- direct outreach to independent ML engineers and research builders;
- design partners who will supply real workflows and feedback;
- open-source integrations where licensing and maintenance fit.

Do not spend on paid marketing before repeat usage and a strong case study exist.

## 21.2 Monetisation hypotheses to test later

1. Free/local core + paid managed execution.
2. Pro plan for advanced workflows, integrations, history, and convenience.
3. Team/research plan for shared projects, governance, experiment history, and managed runners.
4. Transparent usage-based pricing for managed model/compute resources.

Do not hide model or compute provider costs. Do not advertise unlimited AI usage until unit economics support it.

## 21.3 Design-partner validation

Before expanding beyond alpha:

- interview at least 10 target users;
- observe at least 5 real repository/research workflows;
- recruit 3–5 design partners;
- ask users to provide an actual task and repository where possible;
- measure current time, failure points, manual effort, and tools used;
- observe whether they return and use the product again;
- test willingness to pay only after users have experienced a working workflow.

Ask about behavior and workflow costs, not whether the idea sounds exciting.

## 21.4 Credibility strategy

A credible case study includes:

- original task and pinned repository revision;
- research sources and claim matrix;
- exact model and tool configuration;
- budget and time;
- actual patch;
- commands/tests and raw outputs;
- experimental conditions/results;
- reviewer findings;
- human intervention;
- failures and limitations;
- baseline comparison.

One honest reproducible case study is more valuable than a polished demo that hides failures.

---

# 22. Implementation roadmap and exit criteria

Do not treat dates as commitments. Advance when the exit criteria are met.

## Phase 0 — Product and architecture freeze

**Build:**
- product/architecture documents;
- threat model;
- Task Contract and event schemas;
- evidence model;
- project/repository/task data model;
- a CLI/UI low-fidelity flow;
- initial user evaluation plan.

**Exit:** one narrow flagship task, acceptance criteria, and a sample repository/task are chosen; key product assumptions are documented.

## Phase 1 — Repository Intelligence alpha

**Build:**
- local project and repository registration;
- pinned snapshot manager;
- file discovery/exclusions;
- Python AST index;
- lexical and symbol search;
- source spans and line references;
- repository map;
- grounded Q&A with evidence;
- SQLite persistence and index metadata;
- `maya init`, `maya inspect`, `maya index`, `maya search`, `maya ask`.

**Exit:** on a curated repository question set, answers cite correct source locations and identify uncertainty/unsupported syntax.

## Phase 2 — Research Intelligence and Claim Mapping

**Build:**
- document ingestion and versioning;
- source passage model;
- explicit/inferred claim extraction;
- claim DAG;
- repository reconciliation and evidence mapping;
- reproduction readiness report;
- `maya research add`, `maya research extract`, `maya map`.

**Exit:** expert reviewers judge extracted claims traceable to source and mappings appropriately cautious on a curated sample.

## Phase 3 — Controlled Engineering Agent

**Build:**
- model adapter and structured output validation;
- Task Contract and implementation plan;
- approval records;
- isolated Git worktree manager;
- tool gateway;
- separate local worker;
- file editing/patch application;
- constrained command execution;
- timeout and output capture;
- diff review;
- pause/cancel and basic recovery.

**Exit:** complete one bounded task end-to-end without mutating the original checkout; the user can inspect and revert all changes.

## Phase 4 — Verification and Evidence

**Build:**
- deterministic check registry;
- baseline/post-change capture;
- test integrity warnings;
- ML-specific invariant templates;
- read-only independent reviewer;
- evidence graph and report builder;
- exportable evidence bundle.

**Exit:** final report faithfully reflects real execution; each success/failure claim links to source or run evidence.

## Phase 5 — Experiment Lab alpha

**Build:**
- immutable manifests;
- experiment profiles and budgets;
- local CPU smoke runner;
- metrics ingestion;
- baseline/candidate protocol comparison;
- uncertainty/statistical summaries;
- artifact capture;
- negative-result memory.

**Exit:** another technical user can reproduce a small run from the bundle or understand exactly why it cannot be reproduced.

## Phase 6 — Design partners and public alpha

**Build:**
- onboarding and documentation;
- demo repository and tutorial;
- evaluation harness;
- issue/feedback process;
- privacy/security documentation;
- 3–5 design-partner workflows.

**Exit:** users return for repeat use, and measured value is demonstrated against a fair same-model baseline.

## Phase 7 — Hosted product and advanced autonomy

Only after demand and security readiness:

- hosted control plane;
- remote isolated runners;
- user-owned Kaggle/Colab/cloud runner adapters;
- collaboration and permissions;
- optional integrations;
- bounded ablation exploration;
- commercial plans and metering.

**Exit:** security review, cost model, operational support, and a clear reason to operate hosted execution.

---

# 23. Detailed coding-agent execution plan

The coding agent should implement in this order. Do not attempt to deliver every phase in one giant change.

## Work Package 0 — Inspect and map the existing codebase

1. Read repository documentation, source tree, configuration, dependency files, Git status, and existing architecture.
2. Identify which parts of the reference engine already exist in the repository and whether they are integrated or standalone.
3. Produce an inventory: keep, refactor, replace, defer, remove.
4. Identify entry points and existing naming conventions.
5. Report current security boundaries and known gaps.
6. Propose a minimal architecture aligned with this brief.

**Deliverable:** `docs/implementation-gap-analysis.md` and a file-level implementation plan.

## Work Package 1 — Domain contracts and persistence

Implement:

- Project, Repository, RepositorySnapshot, Task, TaskContract, WorkflowRun, WorkflowEvent, ApprovalRequest, EvidenceArtifact domain types;
- versioned Pydantic/JSON schemas;
- SQLite database initialization and migrations;
- repository interfaces for storage;
- append-only event recording;
- local artifact store with SHA-256 content hashes;
- serialization and schema-version policy.

Do not couple domain types to FastAPI request objects or model-provider SDKs.

**Deliverable:** local database can create a project, pin a repository snapshot record, create a task contract, record events, and persist artifacts.

## Work Package 2 — Repository snapshot and index

Implement:

- Git repo validation and full commit resolution;
- dirty-tree policy and separate patch snapshot;
- safe file discovery and ignore rules;
- secret/binary/size exclusion with recorded reasons;
- Python AST parser;
- source-range/symbol extraction;
- FTS5/lexical search;
- symbol lookup;
- repository map;
- index metadata and content hashes.

Start with Python and explicitly report parser coverage. Do not claim all-language support.

**Deliverable:** `maya inspect` can produce a repository map and answer deterministic path/symbol search requests with correct line references.

## Work Package 3 — Retrieval and grounded repository Q&A

Implement:

- search planner: exact lexical, symbol, relation expansion, optional semantic retrieval;
- query-specific context packer;
- provider-aware token budget estimator;
- source citation objects;
- structured Q&A output;
- evidence validation that every cited source belongs to the pinned snapshot;
- uncertainty response when source evidence is insufficient.

PPR is an optional next improvement. First measure lexical+symbol retrieval. Do not use expensive embeddings before a baseline shows a need.

**Deliverable:** repository questions produce concise answers with verifiable file/line references.

## Work Package 4 — Research document and claim model

Implement:

- document registration, file hash, extraction metadata;
- PDF/Markdown/plain-text ingestion;
- page/section/source passage indexing;
- claim extraction schema;
- explicit/inferred/user-requirement categories;
- claim DAG and dependency validation;
- manual claim correction/review path;
- claim-to-symbol candidate mapping;
- evidence-backed mapping statuses;
- transparent risk/readiness explanation.

No symbol-name-only `PRESENT` classification. Do not auto-approve claim mappings without source and code evidence.

**Deliverable:** `maya map` or the Research UI produces an editable, evidence-linked Claim-to-Code matrix.

## Work Package 5 — Task workflow and model gateway

Implement:

- provider-neutral interface;
- one working model adapter;
- structured output validation;
- task templates: repository Q&A, research analysis, engineering change, paper-to-code;
- Task Contract creation;
- explicit workflow states and persisted events;
- plan generation and approval requests;
- usage/cost accounting;
- idempotent background job records.

Do not create many autonomous agents. Implement bounded role prompts/contracts inside one orchestrator.

**Deliverable:** user can create a task, inspect plan and acceptance criteria, and receive a persisted workflow timeline.

## Work Package 6 — Tool gateway and safe local execution

Implement:

- typed tool definitions;
- per-tool permission requirements;
- approval consumption and action-scope hash;
- path policy and symlink checks;
- worktree lifecycle and concurrency safety;
- separate local worker process;
- command execution without shell where practical;
- timeout/process termination/output size/redaction;
- resource budgets;
- command and tool audit events;
- patch capture including untracked files;
- revert/export behavior.

Do not treat regex shell deny-lists as a sandbox. Use allowlisted tool operations and runner isolation.

**Deliverable:** one user-approved bounded code change can be performed in an isolated worktree, with an inspectable patch and no writes to the source checkout.

## Work Package 7 — Verification and evidence report

Implement:

- check registry and check status enum;
- baseline and candidate check recording;
- test modification warnings;
- ML invariant plug-in interface;
- independent read-only reviewer input package;
- structured reviewer findings;
- claim-to-check/evidence links;
- final task decision rules;
- report and evidence bundle export/verification.

The report must distinguish implemented, source-grounded, deterministically checked, experimentally observed, model-assessed, human-confirmed, and unverified claims.

**Deliverable:** one full vertical-slice task ends with a truthful evidence report and reproducible patch bundle.

## Work Package 8 — Experiment Lab

Implement:

- experiment proposal schema;
- experiment approval and profile limits;
- manifest creation and content hash;
- runner adapter;
- structured metric definitions and raw observations;
- comparability report;
- appropriate statistical analysis with uncertainty/caveats;
- negative-result memory.

Do not declare “improved” on one run or solely by a p-value. Keep metric direction and primary metric defined before the experiment.

**Deliverable:** a small baseline/candidate CPU experiment can be recorded, compared, and exported reproducibly.

## Work Package 9 — UI and product polish

Build the UI against the stable API contracts:

- project overview;
- repository map/search;
- research source/claim matrix;
- task plan/progress/approval;
- diff/review;
- experiment view;
- evidence report;
- settings/provider/permissions.

Prioritise legibility and supervision over animation and visual effects.

**Deliverable:** a technical user can complete and supervise the flagship workflow without using internal developer commands.

---

# 24. Definition of done for first public alpha

A limited public alpha is ready only when:

- [ ] A user can create a project and attach a local Git repository.
- [ ] Every task is tied to an immutable repository snapshot/commit and explicit working-tree policy.
- [ ] Repository file and symbol maps are grounded in actual indexed source.
- [ ] Search and Q&A return verifiable source references.
- [ ] Unsupported language/parser coverage is visible.
- [ ] A user can attach a paper and inspect source-linked claims.
- [ ] Claim mappings distinguish present, partial, absent, uncertain, conflicting, and not applicable.
- [ ] The system does not mark a mapping present from symbol name alone.
- [ ] A user can inspect and approve a Task Contract and implementation plan.
- [ ] Model outputs are schema-validated.
- [ ] Writes occur only in the task-owned worktree.
- [ ] Tool/command execution occurs in a separate worker with explicit budgets and timeouts.
- [ ] Network and dependency installation are denied unless approved.
- [ ] The user can inspect, stop, export, and revert changes.
- [ ] Baseline and post-change results are separately recorded.
- [ ] Failed, skipped, blocked, and unavailable checks remain visible.
- [ ] Independent review is read-only and receives raw evidence.
- [ ] The final report distinguishes implementation from validation.
- [ ] Artifacts/manifests include hashes and version metadata.
- [ ] No public multi-tenant arbitrary code execution is enabled without a proper isolation and security review.
- [ ] A same-model baseline comparison has been performed on held-out tasks.
- [ ] At least three external technical users have provided actionable feedback, and repeat usage is being measured.
- [ ] The installation/demo can be reproduced by another technical person using the documentation.

---

# 25. Decisions: keep, refine, defer, or reject

| Idea from v1/v2 | Decision | Product reasoning |
|---|---|---|
| Research-to-Implementation as wedge | **KEEP — core** | Strongest coherent customer outcome and best product identity. |
| Repository Intelligence | **KEEP — foundational** | Needed for every serious codebase-aware workflow. |
| Engineering Task Execution | **KEEP — core** | Converts analysis into tangible user value. |
| Experiment Lab | **KEEP — staged** | Important for ML credibility; build provenance before full orchestration. |
| Paper-to-Code Claim DAG | **KEEP — core differentiator** | Makes research requirements explicit and traceable. |
| Personalized PageRank | **REFINE** | Useful ranking option; must beat simpler retrieval baselines. |
| Submodular context packing | **REFINE** | Context selection is essential; do not claim optimality without proof. |
| AST anti-gaming verifier | **KEEP — limited claim** | Useful structural warning; cannot prove semantic test integrity. |
| Paired t-test / Hedges' g | **REFINE** | Useful for suitable designs; add uncertainty, assumptions, and multiple-comparison handling. |
| HMAC approval tokens | **REFINE** | Useful for signed scope records; not a substitute for authenticated, scoped, expiring, server-enforced approvals. |
| Git worktrees | **KEEP — source isolation** | Valuable for branch/working-copy safety, not process sandboxing. |
| Local-first / BYOK | **KEEP — initial deployment** | Fits technical users and constrained infrastructure; explain external data flows. |
| Zero-cost Kaggle/Colab mesh | **DEFER** | External quotas/terms are not stable product infrastructure; start with exportable runner bundles. |
| Veritas/Vexx symbolic bridge | **DEFER / PLUGIN** | Interesting specialized capability; should not tightly couple the core. |
| Negative-result graveyard | **KEEP — staged** | Potentially valuable compounding project memory once provenance exists. |
| Signed proof bundles | **KEEP — later** | Useful distribution mechanism after evidence integrity is reliable. |
| Forkable research blueprints | **KEEP — later** | Potential ecosystem, but requires schema stability and licensing review. |
| Live AST diff HUD | **DEFER** | Useful polish but not a core proof of value. |
| Zero-jargon failure autopsy | **KEEP — simple version** | Clear user value; derive it from actual structured logs. |
| Bounded ablation tree search | **DEFER — advanced** | Depends on experiment quality, budgets, and valid promotion criteria. |
| Many specialist agents | **REJECT as architecture goal** | Role separation is useful; a swarm increases complexity without guaranteed intelligence. |
| Hosted enterprise platform | **DEFER** | Requires demand, security, operations, and economics. |

---

# 26. Key product risks and mitigations

| Risk | Why it matters | Mitigation |
|---|---|---|
| Product too broad | Solo/small team cannot build everything | One flagship workflow; strict P0/P1/P2. |
| Generic-agent competition | Code editing and terminal access are table stakes | Compete on research fidelity, claim mapping, experiment quality, memory, evidence. |
| Hallucinated paper details | Destroys research trust | Source-linked claims and explicit inference labels. |
| False repository mappings | Symbol matches can be misleading | Require code-span reasoning and validation; `UNCERTAIN` is acceptable. |
| Context retrieval misses key files | Agent may edit wrong subsystem | Hybrid retrieval, relation expansion, direct source reads, held-out evaluation. |
| Test gaming | Agent may weaken tests to pass | Protected baseline tests, diff auditing, external evaluator, AST warnings. |
| Unsafe execution | Repository code is untrusted | Separate runner, least privilege, no network by default, resource limits; microVM for hosted hostile workloads. |
| Experiment unfairness | False improvement claims | Immutable manifests, raw metrics, protocol comparison, design-aware statistics. |
| Free-tier dependency | Quotas/availability/terms change | BYOK and adapter-based user-owned compute; no “free forever” promise. |
| Weak market demand | Impressive tech may not be recurring pain | User interviews, observed workflows, repeat-use design partners. |
| Vendor lock-in | Provider changes and model access shifts | Adapter interfaces, exportable state, open schemas. |
| Privacy concerns | User repositories may be proprietary | Local-only mode, clear provider data flow, retention/deletion controls. |
| Over-engineering | Infrastructure absorbs effort before user value | Modular monolith; no distributed services until measured need. |

---

# 27. Non-negotiable engineering rules

1. No model can grant itself authority or bypass backend policy.
2. No user repository is modified in its original checkout by default.
3. No task is marked successful solely from model self-report.
4. No paper claim is marked implemented from symbol-name matching alone.
5. Every code claim links to a pinned source revision and location where possible.
6. Every consequential action is approved against its exact scope.
7. Every executed command records arguments, environment policy, exit code, duration, and redacted output.
8. Every experiment has a manifest or is explicitly labelled informal/uncontrolled.
9. Every result records the repository revision and model/provider configuration where relevant.
10. Failed, skipped, blocked, and inconclusive outcomes are first-class results.
11. No unbounded loops, tool calls, tokens, costs, runtime, or process resources.
12. No public arbitrary code execution before a dedicated security design and review.
13. No raw secrets in logs, model context, memory, or exported artifacts.
14. No benchmark claim without a reproducible protocol and exact system configuration.
15. User can inspect, stop, export, and revert.
16. Keep domain logic independent of provider SDKs and web framework.
17. Preserve migrations and compatibility; avoid destructive database changes.
18. Build vertical slices, not empty scaffolding.
19. Every capability has an acceptance criterion and a known limitation statement.
20. Build proof first; build scale second.

---

# 28. Final product direction

MayaWorkbench should become the research-engineering environment that connects the full lifecycle of serious AI/ML development:

**Understand the research. Understand the system. Connect the two. Plan the change. Implement it safely. Test the actual behavior. Run controlled experiments. Preserve the evidence. Learn from the outcome.**

The first release must not try to autonomously discover new science. It must prove that it can reliably help a technically demanding user move from an unfamiliar research or engineering objective to a grounded, reviewable implementation and honest evidence.

The product's long-term advantage should come from the connected system around the models: repository intelligence, source-linked research, claim-to-code mapping, controlled tools, domain-aware verification, experimental provenance, and durable project memory.

The system should become more autonomous only as each stage becomes measurable and reliable. More agents, more automation, and more compute are not substitutes for correctness.

> **MayaWorkbench: From research to implementation. From implementation to evidence.**

**Build proof first. Build autonomy second. Build scale when users and evidence justify it.**

---

# Appendix A — Technical references

These references support selected implementation choices. They do not establish that MayaWorkbench itself is correct or that any competitor lacks a capability.

1. **Tree-sitter documentation** — incremental parsing, concrete syntax trees, robustness to syntax errors, and supported bindings: https://tree-sitter.github.io/tree-sitter/ and https://github.com/tree-sitter/tree-sitter/blob/master/docs/src/index.md
2. **SQLite Write-Ahead Logging** — WAL concurrency behavior and limitations, including same-host requirements: https://sqlite.org/wal.html
3. **SQLite FTS5** — full-text search extension and ranking/snippet facilities: https://sqlite.org/fts5.html
4. **Firecracker design documentation** — microVM architecture, KVM requirement, and defense-in-depth host controls: https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md
5. **Firecracker production host setup** — hardening and operational recommendations: https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md
6. **OpenTelemetry overview** — vendor-neutral traces, metrics, logs, and semantic conventions: https://opentelemetry.io/docs/what-is-opentelemetry/
7. **Model Context Protocol specification** — protocol structure and explicit user-consent/security principles: https://modelcontextprotocol.io/specification/2026-07-28/

## Interpretation notes

- Tree-sitter provides syntax trees and incremental parsing, not complete runtime semantics or a perfect call graph.
- SQLite WAL is a local/same-host storage option, not a distributed database. Use PostgreSQL for multi-host hosted workflows.
- Firecracker is a possible hardened hosted-runner component, not a free or trivial sandbox. Its security depends on host configuration and defense in depth.
- MCP provides interoperability; MayaWorkbench must still enforce its own tool authorization, approvals, privacy, and execution policies.
- Product requirements, priorities, targets, and architecture proposals in this document are specifications—not claims that the application has already implemented or achieved them.

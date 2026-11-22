# MayaWorkbench

MayaWorkbench is a local-first repository intelligence workbench. It pins an
immutable snapshot of a Git repository, builds a deterministic index of what it
could and could not understand, and answers questions using only retrieved
evidence from that snapshot — with every claim traceable to a file, line range
and verified quote.

Phase 0 (product/architecture contracts), Phase 1 (repository intelligence),
Phase 2 (research intelligence and claim-to-code mapping), Phase 3 (task
contracts, approvals, isolated worktrees and the bounded agent loop), Phase 4
(verification and evidence: deterministic checks, baseline comparison, test
integrity, ML invariants, independent review, evidence graph and bundles) and
Phase 5 (pre-registered A/B experiments, fixed statistics, reproduction bundles
and scoped project memory) and Phase 6 (a deterministic finish gate with bounded
repair, retrieval-grounded orientation, a durable working ledger and robustness
hardening) are implemented and covered by tests. Hosted multi-user hardening and
labelled corpus scoring remain designed but deliberately not stubbed — see
[docs/phase-6-report.md](docs/phase-6-report.md) and
[docs/implementation-gap-analysis.md](docs/implementation-gap-analysis.md).

## The problem

Coding agents routinely claim more than they can prove: "I implemented X",
"tests pass", "this function is used here". The failure is structural. The model
is asked to both do the work and grade it, using context that was never pinned
and evidence that was never checked.

MayaWorkbench separates those jobs:

- **Snapshots are pinned.** Indexing reads a resolved commit (or an explicitly
  captured working tree with a patch artifact), never "whatever is on disk
  now". A stale worktree snapshot is refused rather than silently re-read.
- **Coverage is labelled.** Python files get AST-level symbols and edges;
  everything else is lexically searchable and marked `LEXICAL_ONLY`. Parse
  failures are stored as `PARSE_ERROR` issues, not swallowed.
- **Exclusions are visible.** Secret-looking files, binaries, oversized files,
  gitignored paths and budget overruns produce `IndexIssue` rows.
- **Citations are verified by code.** A model answer is accepted only if every
  citation's path is in the retrieved context, its line range lies inside a
  retrieved span, and its quote appears verbatim in those lines. Otherwise the
  answer is withheld and recorded as `CITATIONS_REJECTED`.
- **Authority stays with the user.** No Phase 1 operation modifies the
  repository, applies patches, or writes outside `$MAYA_HOME`.

## Install

Python 3.11+ and Git are required. The only runtime dependency is
`pydantic>=2.7`; SQLite (with FTS5) and Git are used through the standard
library and the `git` executable.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/maya --version
```

## Quick start

```bash
maya init --project research
maya repo add /path/to/repository
maya snapshot create           # pins the current commit
maya index                     # builds symbols, lines, edges, issues
maya inspect                   # repository map with coverage labels
maya search "attention mask"   # lexical, exact and regex search
maya symbol show CausalAttention.forward
maya ask "Where is the causal mask applied?"
```

`maya ask` without a configured model returns a deterministic evidence digest
(`NO_MODEL`) with verified retrieval citations. To let a model synthesise an
answer, opt in explicitly:

```bash
maya config set data_policy=BYOK_REMOTE_MODEL
maya config set model.provider=openai_compatible
maya config set model.model=gpt-4.1-mini
maya config set model.base_url=https://api.openai.com/v1
export MAYA_MODEL_API_KEY=...      # never stored in the config file
maya model status                  # shows whether calls are permitted
```

Native tool calling and streamed responses are available on the OpenAI-compatible
adapter. Two additional selectable adapters use their providers' native APIs:
`model.provider=anthropic` (with `MAYA_ANTHROPIC_API_KEY`) and
`model.provider=gemini` (with `MAYA_GEMINI_API_KEY`). Keys are always read from the
environment and are never stored, logged or sent to another provider. Model output
remains a proposal: the tool gateway, runner and evidence system are the authority.

Attach a four-tier reproducibility report to a run (method code found,
implemented, validated, result reproduced) with `maya research reproduction <run>
--json`; the report is computed from recorded evidence, not from a model's
self-assessment. `maya eval r2i` scores exported claim/matrix artifacts against
the bundled labelled seed corpus; its results are measurements, not a benchmark
claim about model quality. `maya release evidence --pytest-report …` assembles a
release record from real inputs (never guessed counts); see `docs/release-gates.md`
for what each gate licenses and what a skip means.

Turn a paper into reviewed, mapped claims:

```bash
maya research add paper.pdf --project research   # or .md/.txt, --url or --doi
maya research extract <document>                 # deterministic; --model is explicit
maya research claims --project research          # inspect extracted claims
maya repo add /path/to/repository && maya snapshot create && maya index
maya map run --project research                  # claim-to-code matrix with verified spans
maya map matrix --project research               # rebuild and inspect the matrix
maya research memo --project research --out memo.md
```

Every explicit claim quote is re-verified against the stored extracted text;
`PRESENT` requires the claim's declared signals in executable code inside a
definition-level span. A model, if enabled, may only adjudicate among discovered
spans, and invalid verdicts are refused. See
[docs/phase-2-report.md](docs/phase-2-report.md).

Run one bounded engineering task in an isolated worktree:

```bash
maya task create --goal "Change count_tokens to a single split" \
  --allow-path utils.py --network ALLOW_WITH_APPROVAL
maya task start <task>
maya task approve <approval>       # once per requested approval
maya task run <task>
maya task diff <run>
maya verify run <run>              # declared checks, baseline comparison, report
maya verify show <run>
maya verify bundle <run> --out evidence/
maya verify check-bundle evidence/
maya task finish <run> --accept --reason "reviewed by me"
```

Declare what must be executed when the task is created; verification never
invents a check or authorizes a command:

```bash
maya task create --goal "Add count_occurrences" --allow-path more_itertools/recipes.py \
  --acceptance "counts matching items" \
  --check 'python -m pytest tests/test_recipes.py -q' --check-criteria AC1 \
  --invariant 'forward_smoke:module=model,callable=Model,shape=1x4x32'
```

`--network ALLOW_WITH_APPROVAL` is an explicit opt-in. The default,
`DENY_BY_DEFAULT`, refuses to start a command on a host where a network
namespace cannot be established and verified. `task finish --accept` requires a
verification report and refuses a `REJECTED`, `BLOCKED` or `INCONCLUSIVE`
decision unless `--override` is passed with a reason; the override is recorded
as a limitation and an event, and the report is never rewritten.

### What the agent may and may not decide

`finish` is a request, not a verdict. When the task contract declares checks,
the application runs them against the candidate worktree *before* the run may
move to review:

- **all pass** → finish is accepted, and the report still says `maya verify run`
  re-executes them as the actual evidence;
- **a real failure, repair budget left** → the model receives a typed
  `REPAIR_REQUIRED` payload (check, exit code, failing test ids, bounded output)
  and the same run continues under the same budgets;
- **a broken checker** (pytest exit 3/4/5/6, blocked or cancelled command) → no
  repair prompt; the limitation names the check that produced no verdict;
- **failure with no repair budget** → finish is accepted and the failure is
  recorded in limitations; verification will fail it.

The model cannot add, skip or edit a check, and `run_check` only accepts keys
from the immutable contract. Each round is stored as an artifact and an
`AGENT_CHECK_COMPLETED` event; `maya task status` shows them.

Include uncommitted work on purpose:

```bash
maya snapshot create --include-working-tree
```

The snapshot records `patch_hash`, a patch artifact and an untracked-file
manifest; the user's checkout is never modified. If the working tree changes
again before indexing, `maya index` refuses the stale snapshot.

All state lives under `$MAYA_HOME` (default `~/.mayaworkbench`):

```
maya.db                  SQLite database (WAL, FTS5)
config.json              non-secret settings
artifacts/ab/cd/<sha>    content-addressed blobs (verified on read)
logs/                    optional structured logs
```

## Commands

| Command | Purpose |
| --- | --- |
| `maya init` | Create `$MAYA_HOME`, migrate the database, create a project |
| `maya doctor` | Check Python, Git, SQLite/FTS5, paths, policy, model config |
| `maya project create\|list\|show` | Manage projects |
| `maya repo add\|list` | Attach local Git repositories |
| `maya snapshot create\|list\|show` | Pin committed or worktree snapshots |
| `maya index` | Index the latest (or a named) snapshot |
| `maya inspect [--save]` | Deterministic repository map and coverage |
| `maya search` | `--mode auto\|lexical\|exact\|regex`, `--path`, `--symbols` |
| `maya symbol show` | Source, signature, docstring and relationships |
| `maya ask` | Grounded answer or evidence digest; `--no-model`, `--budget` |
| `maya qa list\|show` | Stored question/answer records and citations |
| `maya research add` | Register a file, URL or DOI as an immutable document version |
| `maya research extract` | Deterministic claim extraction; `--model`, `--section`, `--force` |
| `maya research documents\|show\|claims` | Inspect documents, passages and extracted claims |
| `maya research memo` | Source-linked memo and readiness; `--out`, `--deviations` |
| `maya map run\|matrix\|claim` | Map claims to code evidence and inspect the matrix |
| `maya task create\|list\|show` | Create a task contract; inspect tasks and runs |
| `maya task start\|approve\|deny\|approvals` | Plan and approve write/command execution |
| `maya task run\|status\|pause\|resume\|cancel` | Execute in the worker and control the run |
| `maya task diff\|review\|finish` | Capture the patch, structurally review it, accept or reject |
| `maya verify checks\|plan` | List built-in checks and inspect/suggest declared checks |
| `maya verify run\|show\|report` | Run an append-only attempt; inspect or write the report |
| `maya verify bundle\|check-bundle` | Export and independently re-verify an evidence bundle |
| `maya verify attest\|check-attestation` | Optionally sign a bundle (HMAC key from `MAYA_SIGNING_KEY`) and verify its signature |
| `maya workspace create\|list\|show\|attach\|environment\|graph` | Multi-resource engineering workspace with typed attachments and environment specs |
| `maya runner doctor\|profiles` | Report actual runtime enforcement and execution trust profiles |
| `maya api serve\|health` | Local loopback JSON control plane with durable event streaming |
| `maya skill list\|add\|enable\|disable\|promote\|export\|import\|seed\|run` | Versioned, user-governed reusable skills |
| `maya eval score <bundle...>` | Score verified task completion per supervision hour and cost |
| `maya env detect\|plan\|doctor` | Detect ecosystems and render argv-only provisioning plans |
| `maya semantic evaluate <cases.json>` | Measure optional reranker vs lexical baseline |
| `maya verify proof\|check-proof` | Write/verify `.maya-proof.json` attestation envelopes |
| `maya memory audit\|retention\|export-bundle\|import-bundle` | Conflict reports, retention, and portable memory bundles |
| `maya experience record\|suggest` | Record failed attempts and recall confirmed prior attempts as advice |
| `maya task cleanup\|export` | Remove the worktree or export a run bundle |
| `maya worker [--once] [--run-id]` | Run the durable task worker process |
| `maya issues` | Index issues filtered by kind or severity |
| `maya events` | Append-only workflow event log |
| `maya config show\|defaults\|set` | Inspect or change configuration |
| `maya model status` | Provider, policy, key presence, callability |
| `maya schemas export` | Versioned JSON Schemas for persisted contracts |
| `maya db migrate\|info` | Apply migrations; inspect schema and row counts |

Every command supports `--json` (global flag) for stable machine-readable
output. Errors go to stderr as `{"error", "code", "hint"}` with a stable
`code` (for example `USAGE_ERROR`, `NOT_FOUND`, `SNAPSHOT_ERROR`) and an exit
code: `2` for invalid input, `3` for a policy refusal, `1` for other typed
failures, `130` when interrupted.

## What "evidence" means here

A snapshot identity is either `"<commit[:12]>"` for committed snapshots or
`"<commit[:12]>+patch:<patch_hash[:12]>"` for worktree snapshots, where
`patch_hash = sha256(git diff bytes || 0x00 || untracked manifest JSON)`. The
same identity always describes the same bytes, which is why an equivalent
snapshot is reused instead of duplicated.

An answer record stores: the question, the snapshot, the retrieval context
pack (artifact), the model invocation (provider, model, tokens, latency, cost
basis — never the prompt or the API key), each citation with its validation
result, and the final status:

| Status | Meaning |
| --- | --- |
| `ANSWERED` | Model answer, every citation verified |
| `ANSWERED_WITH_UNCERTAINTY` | Verified citations, but the model declared uncertainty |
| `NO_MODEL` | No model call; deterministic evidence digest only |
| `INSUFFICIENT_EVIDENCE` | Retrieval found nothing worth answering from |
| `CITATIONS_REJECTED` | Model answer withheld because a citation failed verification |
| `MODEL_UNAVAILABLE` | A model was requested but could not be used |
| `FAILED` | Provider error or schema-invalid output; nothing presented as an answer |

## Execution trust and reported limits

Command execution uses an explicit trust profile. `LOCAL_TRUSTED` runs project
code with the user's OS privileges and is **not a sandbox**. `READ_ONLY_ANALYSIS`
is implemented in code: the tool gateway is masked to read-only tools and the
runner refuses every command before spawn — no OS sandbox is needed. The worker
routes `ROOTLESS_CONTAINER` through a populated task overlay shared by file
tools, commands, checks and artifact capture, but **only** when one canonical
capability report proves a rootless OCI runtime, writable cgroup delegation and
a passing smoke test; otherwise the task is blocked before the first write or
process spawn. `REMOTE_MICROVM` and `GPU_RUNNER` require user-provided
infrastructure and remain blocked. Inspect the single report with
`maya runner doctor` and list profiles with `maya runner profiles` (both support
`--json`).

Limit enforcement is reported per limit with the mechanism observed at runtime
(`wall_time`, `cpu`, `memory`, `process_count`, `file_size`, `open_files`). Each
rlimit is applied inside the child and read back with `getrlimit`; the aggregate
`limits_enforcement` is derived from those results
(`all_enforced`/`partially_enforced`/`not_enforced`). `RLIMIT_NPROC` is not used:
it is a per-UID ceiling Linux does not enforce for privileged processes and its
headroom calculation can block legitimate forks. The base runner therefore
reports `process_count: unsupported` unless a writable cgroup v2 scope is
discovered, created and read back (then `enforced`, mechanism `cgroup-v2`), or a
smoke-verified rootless container applies `pids.max`. Controller names alone are
never reported as enforcement. The wall-clock supervisor kills the whole
process group in every profile.

See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the feature→code→test
truth table and [docs/test-matrix.md](docs/test-matrix.md) for the declared test
environment and latest full-suite result.

## Known limitations

These are properties of the current implementation, not TODOs dressed up as
features:

- **Python AST coverage plus heuristic symbols.** Classes, functions, methods,
  constants, imports, calls, inheritance and decorators are extracted for `.py`
  files with `FULL` coverage. JavaScript/TypeScript/Go/Rust receive bounded
  regular-expression declaration/import/call discovery with `PARTIAL` coverage,
  LOW-confidence symbols and `HEURISTIC` relation provenance; the repository map
  names these languages separately. All other languages remain lexical-only.
  Re-indexing an unchanged snapshot reuses content-hash-cached prepared rows;
  a parser-version or policy change invalidates the affected cache keys.
- **Static analysis only.** Dynamic dispatch, `getattr`, string-based imports,
  monkeypatching and framework magic are not resolved; such edges are either
  unresolved or `LOW` confidence.
- **Heuristic token estimates.** Context budgets use `chars / 3.6 + 8` per span
  unless a provider tokenizer is available, and the estimate is recorded.
- **Line-level lexical index.** Files longer than 200,000 lines and symbols
  beyond 5,000 per file are truncated with explicit issues; individual lines
  over 8,000 characters are truncated with `LINE_TRUNCATED`.
- **Secret handling is best-effort.** Filename patterns and content scans catch
  common cases; they are not a substitute for proper secret management. Skipped
  files are reported, never silently dropped.
- **Single-user, local-only.** There is no hosted, multi-tenant or remotely
  exposed service and no authentication. A loopback-only JSON control plane and
  thin local UI are available on `127.0.0.1` (`maya api serve`); do not expose
  them to a network. The database is a local file.
- **PDF text depends on a text layer.** The builtin extractor handles classic and
  Flate content streams; scanned pages are reported as missing. An explicit
  opt-in Tesseract adapter can OCR them with separate `SOURCE_OCR` provenance;
  OCR text is never treated as an exact quotation without human confirmation.
  Install `pypdf` for better font/`ToUnicode` handling.
- **Mapping is scoped to one snapshot.** `ABSENT` means "not found in the
  inspected scope" and is downgraded to `UNCERTAIN` when parser coverage or
  discovery is incomplete.
- **Readiness is a heuristic, not a probability.** Factors with no recorded facts
  stay `UNKNOWN`, and an overall value is withheld below 75% known weight.
- **Verification proves only what a declared check exercised.** With no
  functional check declared, the decision is `INCONCLUSIVE`. Baseline
  comparison runs on the same host/interpreter; if the base revision cannot run
  the check, the report records `BASELINE_UNAVAILABLE` as a limitation.
- **Test integrity is structural, not semantic.** It detects common weakening
  patterns (deleted/skipped tests, reduced assertions, tautologies, inflated
  tolerances, config weakening), not every way a test can be gamed.
- **The independent reviewer is advisory and needs a model.** It can only read
  the captured diff and check metadata; without a model the check is `SKIPPED`
  and the report says so. It can never turn a failed check into a pass.
- **In-loop checks are advisory and pytest-aware.** They guide repair; only
  `maya verify run` produces evidence. pytest exit codes distinguish a test
  failure from a broken checker, while other runners are treated generically.
  A misclassification can cost a repair round but can never turn a failure into
  a pass.
- **Repair quality depends on the model.** The loop supplies structured evidence
  and a bounded budget (`max_repair_attempts`); it does not localise the defect
  or learn from earlier runs.
- **Lossy reads are labelled.** A non-UTF-8 file can be read with replacement
  characters for orientation, but `apply_patch` refuses it, so edits never
  silently corrupt an encoding. UTF-32 is refused rather than guessed at.
- **Byte-order marks are decoded, not preserved as content.** UTF-8/UTF-16
  sources with a BOM are indexed and parsed; UTF-16 Python is searchable but
  honestly reported as `PARSE_ERROR` because Python source must be UTF-8.
- **Some indexed paths are unreadable by the agent.** A filename containing
  control characters or bidirectional overrides exists in the index (the
  repository really contains it) but the gateway refuses to read or write it.
- **SQLite is single-host.** The database refuses known network filesystems
  (NFS/SMB/9p/…) instead of pretending WAL locking is safe there.

## Repository layout

```
src/mayaworkbench/
  config.py            strict settings, env overrides, no secrets on disk
  domain/              enums and persisted models
  storage/             SQLite, checksummed migrations, stores, artifact store
  repositories/        Git adapter, discovery/exclusions, snapshot capture
  indexing/            Python AST parser, edge resolution, indexer, map
  retrieval/           lexical/exact/regex/symbol search, context packing
  models/              provider-neutral gateway and OpenAI-compatible adapter
  qa/                  prompts, deterministic citation validation, answer pipeline
  research/            documents, passages, claims, DAG, readiness, memos
  mapping/             signals, evidence discovery, rule pack, adjudication
  tasks/               contracts, plans, approvals, bounded agent loop, in-loop checks
  workspace/           path policy, patches, command runner, tool gateway, worktrees
  verification/        check registry, test integrity, reviewer, report, bundle
  experiments/         protocols, manifests, runner, statistics, comparison, bundles
  memory/              scoped project statements with user confirmation
  app/container.py     composition root
  cli/                 argparse CLI and human/JSON renderers
tests/                 pytest suite (storage, snapshots, indexing, retrieval, QA, research, mapping, CLI, verification, experiments, memory)
docs/                  Phase 0 contracts and the Phase 1–5 reports
```

## Testing

```bash
.venv/bin/python -m pytest
```

The suite builds real temporary Git repositories (committed history, dirty
worktree, secrets, binaries, gitignored files, broken Python) and exercises the
full CLI. It asserts behaviour that matters: uncommitted edits are excluded
from committed snapshots, stale worktrees are refused, a failed reindex leaves
the previous index intact, invalid citations withhold the answer, and
`LOCAL_ONLY` refuses remote model calls.

## Documentation

- [Product freeze](docs/00-product-freeze.md)
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Data model](docs/data-model.md)
- [Events and contract schemas](docs/event-and-contract-schemas.md)
- [Evaluation plan](docs/evaluation-plan.md)
- [CLI and UI flow](docs/ui-flow.md)
- [Implementation gap analysis](docs/implementation-gap-analysis.md)
- [Implementation status (truth table)](IMPLEMENTATION_STATUS.md)
- [Test matrix and release summary](docs/test-matrix.md)
- [Rigorous end-to-end audit](docs/audit-end-to-end.md)
- [Phase 1 report](docs/phase-1-report.md)
- [Phase 2 report](docs/phase-2-report.md)
- [Phase 3 report](docs/phase-3-report.md)
- [Phase 4 report](docs/phase-4-report.md)
- [Phase 5 report](docs/phase-5-report.md)
- [ADR 0007 — claims, mapping and readiness](docs/adr/0007-claims-mapping-and-readiness.md)
- [ADR 0008 — controlled engineering agent](docs/adr/0008-controlled-engineering-agent.md)
- [ADR 0009 — verification and evidence](docs/adr/0009-verification-and-evidence.md)
- [Phase 6 report](docs/phase-6-report.md)
- [Adversarial audit of Phase 6](docs/audit-phase-6.md) — 82 probes, 2 defects found and fixed
- [ADR 0010 — experiment lab and memory](docs/adr/0010-experiment-lab-and-memory.md)
- [ADR 0011 — deterministic finish gate and repair](docs/adr/0011-deterministic-finish-gate-and-repair.md)
- [Local JSON control plane](docs/api-control-plane.md)
- [Architecture decision records](docs/adr/)
  - [ADR 0012 — Execution trust and limit evidence](docs/adr/0012-execution-trust-and-limit-evidence.md)
  - [ADR 0013 — Incremental index cache](docs/adr/0013-incremental-indexing-cache.md)
  - [ADR 0014 — Multi-language adapters](docs/adr/0014-multi-language-adapters.md)

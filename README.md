# MayaWorkbench

MayaWorkbench is a local-first repository intelligence workbench. It pins an
immutable snapshot of a Git repository, builds a deterministic index of what it
could and could not understand, and answers questions using only retrieved
evidence from that snapshot — with every claim traceable to a file, line range
and verified quote.

Phase 0 (product/architecture contracts), Phase 1 (repository intelligence),
Phase 2 (research intelligence and claim-to-code mapping), Phase 3 (task
contracts, approvals, isolated worktrees and the bounded agent loop) and Phase 4
(verification and evidence: deterministic checks, baseline comparison, test
integrity, ML invariants, independent review, evidence graph and bundles) are
implemented and covered by tests. Hosted multi-user hardening and labelled
corpus scoring remain designed but deliberately not stubbed — see
[docs/phase-4-report.md](docs/phase-4-report.md) and
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

## Known limitations

These are properties of the current implementation, not TODOs dressed up as
features:

- **Python-only AST coverage.** Classes, functions, methods, constants,
  imports, calls, inheritance and decorators are extracted for `.py` files.
  Other languages are indexed lexically (`LEXICAL_ONLY`) and labelled as such.
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
- **Single-user, local-only.** There is no authentication, multi-tenancy or
  network service; the database is a local file.
- **PDF text depends on a text layer.** The builtin extractor handles classic and
  Flate content streams; scanned pages are reported as missing, never OCR'd.
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
  verification/        check registry, test integrity, reviewer, report, bundle
  app/container.py     composition root
  cli/                 argparse CLI and human/JSON renderers
tests/                 pytest suite (storage, snapshots, indexing, retrieval, QA, research, mapping, CLI, verification)
docs/                  Phase 0 contracts and the Phase 1–4 reports
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
- [Phase 1 report](docs/phase-1-report.md)
- [Phase 2 report](docs/phase-2-report.md)
- [Phase 3 report](docs/phase-3-report.md)
- [Phase 4 report](docs/phase-4-report.md)
- [ADR 0007 — claims, mapping and readiness](docs/adr/0007-claims-mapping-and-readiness.md)
- [ADR 0008 — controlled engineering agent](docs/adr/0008-controlled-engineering-agent.md)
- [ADR 0009 — verification and evidence](docs/adr/0009-verification-and-evidence.md)
- [Architecture decision records](docs/adr/)

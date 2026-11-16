# CLI and UI flow (low fidelity)

Phase 1 exposes the product through a CLI. This document records the intended
flow, the real output shape (abbreviated), the states a future UI must render,
and the mapping from CLI commands to UI actions. Transcripts come from running
the tool against the fixture repository; values are illustrative of shape, not
fixed.

## 1. First run

```
$ maya doctor
python          ok      3.12.3
git             ok      git version 2.43.0
sqlite          3.45.1
fts5            ok      available in this SQLite build
data directory  ok      writable: ~/.mayaworkbench
configuration   ok      ~/.mayaworkbench/config.json (LOCAL_ONLY)
database        warn    ~/.mayaworkbench/maya.db (schema 0, journal wal)
model           warn    none/<unset>: no model provider configured

status: ok
```

Warnings are actionable, not failures: a missing database is created by `init`,
and a missing model is the privacy-preserving default.

```
$ maya init --project research
MayaWorkbench initialised
data directory  /home/user/.mayaworkbench
database        /home/user/.mayaworkbench/maya.db
artifacts       /home/user/.mayaworkbench/artifacts
schema version  1
project         research (research)
data policy     LOCAL_ONLY

Next
- maya repo add <path> --project research
- maya snapshot create
- maya index
- maya inspect
- maya ask "what does this repository do?"
```

## 2. Attach and pin

```
$ maya repo add ~/src/tinygpt
Attached: tinygpt
id              repo_01…
root            /home/user/src/tinygpt
remote          —
default branch  main

Next: maya snapshot create

$ maya snapshot create
Snapshot snap_01… (committed)
identity            f8cb7b0833fb
repository          tinygpt (~/src/tinygpt)
commit              f8cb7b0833fb…
branch              main
working tree dirty  yes
patch included      no
changed paths       0
status              CREATED

Warning
- The working tree has 3 uncommitted change(s). They are NOT part of this
  snapshot. Re-run with --include-working-tree to capture them explicitly.

Next: maya index --snapshot snap_01…
```

Dirty-tree warning is a first-class state. The alternative — silently indexing
the working tree — is exactly the failure the product exists to prevent.

```
$ maya snapshot create --include-working-tree
Snapshot snap_01… (worktree)
identity            f8cb7b0833fb+patch:36ac8dd02b84
working tree dirty  yes
patch included      yes
patch hash          36ac8dd02b84…
changed paths       2
untracked           1 file
patch artifact      art_01…
```

## 3. Index and map

```
$ maya index
Indexed snap_01… in 46 ms
files          10 (5 FULL, 4 LEXICAL_ONLY, 1 …)
lines          164
symbols        23
edges          79 (18 resolved)
issues         2
skipped        2
manifest       art_01…

Coverage
FULL          5
LEXICAL_ONLY  4
PARTIAL       2

Warning: 2 file(s) were skipped while reading; see `maya issues`.

$ maya inspect
Repository map — tinygpt
snapshot    snap_01…
revision    committed f8cb7b0833fb
dirty       yes
files       10
skipped     2
lines       164
symbols     23
edges       79 (18 resolved)

Languages
python    6  141
markdown  2   13
ignore    1    3
toml      1    7

Entry points
- tinygpt.cli:main (src/tinygpt/cli.py:44)
- python -m tinygpt (src/tinygpt/__main__.py)

Tests (1)
- tests/test_attention.py
```

The map is deterministic: every number comes from stored rows for that
snapshot, and coverage/skips are shown before any question is asked.

## 4. Search and symbol inspection

```
$ maya search "causal mask" --limit 3
location                     match    line
src/tinygpt/attention.py:33  lexical  mask = torch.triu(torch.ones(seq, seq…
src/tinygpt/attention.py:34  lexical  scores = scores.masked_fill(mask, …)
tests/test_attention.py:6    lexical  def test_causal_masking_blocks_future…

FTS expression used: causal AND mask (broadened to causal OR mask)
```

```
$ maya symbol show CausalAttention.forward
CausalAttention.forward (method)
location   src/tinygpt/attention.py:30-39
signature  def forward(self, x: torch.Tensor) -> torch.Tensor

Source
      def forward(...):
          ...

Relationships
direction  type   target                  location                  confidence
incoming   CALLS  build_attention         src/tinygpt/attention.py:43 HIGH
```

Unresolved calls remain in symbol detail with `LOW` confidence; they are not
silently omitted.

## 5. Asking a question

```
$ maya ask "Where is the causal mask applied?"
Answer (NO_MODEL)
**Retrieval evidence for snapshot `snap_01…`**
- repository: `tinygpt` (~/src/tinygpt)
- revision: `f8cb7b0833fb`
- budget: 1006/8000 estimated tokens (chars/3.6 plus 8 tokens per span)

### Symbols matched
- `CausalAttention.forward` (method) — src/tinygpt/attention.py:30-39

### Matching lines
- src/tinygpt/attention.py:33 — `mask = torch.triu(...)`
- src/tinygpt/attention.py:34 — `scores = scores.masked_fill(mask, ...)`

Citations (6)
path                        lines    validation
src/tinygpt/attention.py    30-39    not model-verified (retrieval evidence)

No model was called, so no synthesized answer was produced.
```

With a model configured and `BYOK_REMOTE_MODEL`:

```
$ maya ask "Where is the causal mask applied?"
Answer (ANSWERED)
The mask is built in `CausalAttention.forward` …

Citations (2)
path                      lines    validation
src/tinygpt/attention.py  30-39    verified
src/tinygpt/attention.py  33-34    verified
```

Failure states a UI must render distinctly:

| State | What the user sees |
| --- | --- |
| `CITATIONS_REJECTED` | "Model answer withheld: citation validation failed" plus each invalid citation and the reason |
| `INSUFFICIENT_EVIDENCE` | "No evidence in this snapshot matched the question" and the extracted identifiers |
| `MODEL_UNAVAILABLE` | "A model was requested but is not usable" with policy/key/config detail |
| `FAILED` | Provider/schema error class; no answer text |
| Empty search | "No matches" plus the modes tried; never a fabricated suggestion |

## 6. Audit trail

```
$ maya qa list
id          status    tokens     citations  question
qa_01…      NO_MODEL  1006/8000  6          Where is the causal mask applied?

$ maya qa show qa_01…
QA record qa_01… (NO_MODEL)
snapshot          snap_01…
context tokens    1006/8000
model invocation  —
artifact          art_01…
```

```
$ maya events --type INDEX_COMPLETED --limit 1
time                 type             entity     actor            payload
2026-11-16 09:11:04  INDEX_COMPLETED  snap_01…   SYSTEM:indexer   {"files_indexed": 10, …}

$ maya issues --kind SKIPPED_SECRET
kind            count
SKIPPED_SECRET  1
```

## 7. Mapping to a future GUI

| UI surface | CLI equivalent | Data source |
| --- | --- | --- |
| Onboarding card | `init`, `doctor` | checks, config |
| Repository list | `repo list` | `repositories` |
| Snapshot switcher | `snapshot list/show` | `repository_snapshots` |
| Coverage banner | `inspect` | `IndexReport`, issues |
| Search panel | `search` | `search_lines`, `symbols` |
| Symbol drawer | `symbol show` | `SymbolDetail` |
| Ask panel | `ask` | `qa_records`, context artifact |
| Citation viewer | `qa show` | citation JSON + `file_lines` |
| Activity log | `events` | `workflow_events` |
| Problems panel | `issues` | `index_issues` |

The UI must keep the same distinctions: snapshot identity is always visible on
an answer, coverage/skips are visible without opening a menu, and a withheld
answer is never styled as a successful one.

## 8. Error and empty states (contract)

- Every error prints a message and, when known, a hint; `--json` emits
  `{"error", "code", "hint"}` and a non-zero exit code.
- `--json` success output is a single JSON object on stdout; warnings go to
  stderr so pipelines stay clean.
- Long output never truncates silently: lists state when they are limited, and
  the artifact/database always holds the complete record.

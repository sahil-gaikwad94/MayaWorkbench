# Phase 6 report — a self-correcting agent, grounded orientation and robustness hardening

Phase 6 in the master specification is "design partners and public alpha". That
phase requires external users, which this environment cannot supply. What it
does require before strangers touch the product is a system that corrects itself
from real evidence instead of model confidence, and that survives repositories
which do not look like the fixtures. This phase delivers that: the engineering
half of the public-alpha exit criteria, with the human half explicitly open.

Status: implemented, tested (326 tests) and exercised on a real mid-sized Python
repository. Changes are uncommitted in the working tree.

## 1. What was wrong before this phase

Three structural weaknesses, each visible in the code rather than inferred:

1. **The implementer never saw its own failures.** `finish` was accepted as a
   request to review, and declared checks ran only afterwards in Phase 4. A model
   could believe it was done, produce a failing patch, and never get the signal.
2. **The implementer started blind.** The first prompt contained the task, the
   plan, and an inventory of up to 400 paths. Choosing which file matters was
   left to `list_files` and keyword guessing, burning steps and tool calls.
3. **Boundaries were brittle to inputs real repositories contain.** A latin-1
   log made a file unreadable; a UTF-8 BOM turned valid Python into a parse
   error; a UTF-16 config was classified as binary; rewriting a CRLF file
   produced a whole-file diff; a patch mismatch said only "stale or modified";
   a closed stdout pipe produced a traceback.

## 2. The finish gate and bounded repair

`finish` is now a request, not a verdict. When the contract declares checks and
command execution is authorized, `tasks/inloop.py` runs them against the
candidate workspace before the run may move to `REVIEWING`:

| Situation | Behaviour |
| --- | --- |
| All checks pass or skip | Finish accepted; limitation states that `maya verify run` re-executes them as evidence |
| A code-level check fails, repair budget remains | `REPAIR_REQUIRED` payload (key, gate, exit code, reason, failing test ids, bounded excerpt) and the loop continues in the same run |
| Only infrastructure failures (pytest 3/4/5/6, blocked, cancelled, no verdict) | No repair prompt; each check is named as unable to produce a verdict |
| Failure with no repair budget | Finish accepted and the failure recorded in limitations; verification will fail it |

Design constraints that make this trustworthy:

- **The model cannot grade itself.** Checks come from the immutable contract.
  `checks_run` remains labelled as model-reported and is never evidence.
- **The model cannot run arbitrary commands to "see if it works".** The new
  `run_check {key}` tool accepts only declared keys and still requires the
  consumed COMMAND_EXECUTION approval and the command allowlist.
- **A broken checker is not a code failure.** pytest exit 1 is a test failure;
  3–6 mean the checker could not produce a verdict, so the loop stops asking for
  repairs instead of inviting speculative edits. For non-pytest commands an
  absent exit code (blocked/cancelled) is infrastructure, a non-zero exit is a
  failure.
- **In-loop results are advisory.** They are stored as run details, an artifact
  per round and `AGENT_CHECK_COMPLETED`/`AGENT_REPAIR_REQUESTED` events, never as
  `VerificationCheck` rows. Phase 4 re-executes every check from scratch after
  patch capture; that report remains the evidence.
- **Budgets still bind.** Rounds are capped by `max_repair_attempts`, and each
  check's timeout is clamped to the remaining round budget so one slow command
  cannot defeat the wall clock.

## 3. Grounded orientation and durable working memory

The first two model steps now receive `repository_orientation`: the top
retrieval spans for the task goal, top-level symbols defined in the allowed
paths, relevant test files and the index issues that make a file unreliable
evidence. It is capped at ~5 kB, states that it describes the pinned base
revision, and is dropped after two steps so it does not become a tax on every
request. A missing index or failed retrieval is a labelled field, never a guess.

`workspace_ledger` is rebuilt from the audited `ToolCall` rows on every step:
files read with ranges, files changed, **files changed but not re-read**, commands
with exit codes, and declared checks already executed. Being derived, it survives
prompt truncation, pause/resume and process restarts, and cannot drift from the
audit log.

Progress is guarded separately from failure: repeating the same read-only action
returns a notice, and the fourth repetition stops the run as `BLOCKED` with
`repeated action`. Edits and commands are deliberately not treated this way,
because retrying them after new information is legitimate.

## 4. Robustness changes

| Finding | Fix | Covered by |
| --- | --- | --- |
| latin-1 text invisible to the agent | Lossy decode labelled `utf-8-replaced` with a replacement count; patches still refuse non-UTF-8 so edits stay lossless | `test_read_file_labels_lossy_utf8_instead_of_hiding_the_file` |
| UTF-16 text mistaken for binary | BOM-aware decode in the gateway and the indexer; UTF-32 refused instead of decoded as garbage | `test_read_file_handles_utf16_text...`, `test_a_byte_order_mark_is_decoded_not_treated_as_an_error` |
| UTF-8 BOM made Python a `PARSE_ERROR` | Index decodes with `utf-8-sig`, so U+FEFF never reaches `ast.parse` | same |
| Rewriting a CRLF/BOM file produced a whole-file diff | `write_file` preserves the existing dominant EOL and a UTF-8 BOM, and says so in the result | `test_write_file_preserves_crlf_and_a_utf8_bom` |
| Stale patch errors were uninformative | First differing line with expected/found, plus whitespace and CRLF causes; NUL bytes refused | `test_patch_mismatch_*` |
| Control characters and bidi overrides in paths | Rejected by `normalize_rel`, with a 255-byte component limit | `test_normalize_rel_refuses_control_and_bidi_characters` |
| Symbol search could name excluded paths | `find_symbol` filters excluded and credential-shaped paths | `test_find_symbol_does_not_leak_an_excluded_path` |
| `maya search \| head` traceback | Broken pipe is success and remaining output goes to devnull | `test_a_closed_stdout_pipe_is_not_an_error` |
| Non-encodable commit/path text aborted a table | stdout/stderr reconfigured with `errors="replace"` | exercised by the JSON/error test and the real-repo probe |
| Empty repository | Typed `MayaError` naming the missing commit | `test_a_repository_without_commits_fails_with_a_typed_error` |

## 5. Validation

### 5.1 Test suite

`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --tb=short -p no:cacheprovider`
→ **326 passed in 42.06 s** (279 before this phase; 47 new tests in
`tests/test_agent_intelligence.py` and `tests/test_robustness.py`, plus updates
to existing verification tests to state that they run with zero repair rounds).

### 5.2 Real repository, real worktree, real check

Repository: `more-itertools` at `b57aae98` (41 indexed files, 1,685 symbols,
698 KB), real Git snapshot and index, real isolated worktree, real command
runner, scripted model (no API key in this environment).

Scripted episode: create a probe module with a deliberately wrong
`count_matches`, ask to finish, receive the failing check, repair, finish again.

| Observation | Value |
| --- | --- |
| Run state | `REVIEWING` |
| In-loop rounds | round 1 `FAILED` (113 ms), round 2 `PASSED` (122 ms) |
| In-loop verdict | `PASSED` |
| Model calls | 5 (plan + 4 steps; the extra step is the repair) |
| Tool calls | `write_file` SUCCEEDED, `apply_patch` SUCCEEDED |
| `REPAIR_REQUIRED` delivered | yes |
| Patch | +6/−0 across 1 file, sha256 `7c856ba5…` |
| `maya verify run` decision | `ACCEPTED` |
| Declared check | PASSED; baseline `FAILED`; `improved: true` |
| Integrity gates | scope, approvals, budget, snapshot, diff, patch reproducibility, report — all PASSED |

`maya task status` renders the rounds:

```
In-loop declared checks (advisory)
round  verdict  duration_ms  checks
1      FAILED   113          command.3c1375d190=FAILED
2      PASSED   122          command.3c1375d190=PASSED
```

### 5.3 Unusual-input repository

A repository containing latin-1 source, a CRLF module, a UTF-16 module, a
UTF-8-BOM module, a space-containing filename, a 9,000-element single line, a
binary blob, a syntax error and a bidi-override filename indexed without a crash:
6 files indexed, 2 skipped (`ENCODING_ERROR`, `SKIPPED_BINARY`), one
`PARSE_ERROR` and one `LINE_TRUNCATED` issue, UTF-16 content searchable, and
`maya search | head` exiting 0.

## 6. What remains weak

These are properties of the current implementation, not future promises:

1. **In-loop checks are pytest-aware, not runner-aware.** Exit codes 3–6 are
   classified for pytest; unittest/cargo/jest are treated generically. A wrong
   classification can cost one repair round or stop one early; it cannot mark a
   failure as a pass.
2. **Repair is bounded, not smart.** The model receives structured evidence and
   a budget; it can still thrash. There is no learned repair policy, no
   cross-run memory of recurring failures, and no attempt to localise a failure
   to a symbol beyond providing the output.
3. **The model was scripted in validation.** No API key exists in this
   environment, so real-model repair quality is unmeasured. The loop's
   behaviour is deterministic and tested; the model's use of the feedback is not.
4. **In-loop checks add model spend.** A failing finish now triggers a repair
   step. `max_repair_attempts = 0` disables repair while keeping one honest
   check, and all existing budgets still apply.
5. **Orientation can be stale by design.** It describes the pinned base revision
   and spans may lie outside the allowed write scope. The payload says both; a
   model that ignores the warning can patch against old context, which the patch
   matcher then refuses.
6. **UTF-16 Python is indexed but not parseable.** Python source must be UTF-8;
   a UTF-16 `.py` file is searchable and honestly reported as `PARSE_ERROR`, not
   silently mis-parsed.
7. **Unreadable-by-policy paths are still indexed.** A bidi-override or
   control-character filename is indexed (the repository really contains it) but
   the tool gateway refuses to read or write it. The divergence is safe but is a
   divergence.
8. **No language coverage beyond Python.** Still lexical-only for everything
   else; nothing in this phase changed that.
9. **No hosted isolation, multi-user auth, or corpus evaluation.** Unchanged and
   deliberately out of scope.

## 7. Not built, and why

- **MCP interoperability, hosted runners, ablation search, collaboration** —
  specified for later phases; none can be honestly delivered without the
  security and tenancy work first.
- **A repair loop that edits the check** — refused by construction: checks are
  contract data, and the gateway rejects writes outside the allowed paths.
- **Automatic re-planning after a failure** — would change the approved plan and
  scope mid-run; repair stays inside the approved contract.

## 8. Reproduce

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# the agent loop without a model provider is exercised by the tests;
# a real run needs a provider:
maya config set data_policy=BYOK_REMOTE_MODEL
maya config set model.provider=openai_compatible
maya config set model.model=gpt-4.1-mini
maya config set model.base_url=https://api.openai.com/v1
export MAYA_MODEL_API_KEY=...
maya task create --goal "..." --allow-path path/to/file.py \
  --check 'python -m pytest tests/test_file.py -q'
maya task start <task> && maya task approve <approval> && maya task run <task>
maya task status <run>      # shows the in-loop rounds
maya verify run <run>       # authoritative evidence
```

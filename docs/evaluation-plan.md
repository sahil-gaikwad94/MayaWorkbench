# Evaluation plan

The claim under test is narrow and falsifiable: **given a pinned snapshot,
MayaWorkbench retrieves the code that answers a question, labels what it could
not analyse, and never presents an unverified citation as evidence.**

Phase 1 evaluates that claim on small, controlled repositories. Agent-task
evaluation (does a patch satisfy a task contract?) belongs to Phase 4 and is not
simulated here.

## 1. Metrics

### 1.1 Indexing fidelity

| Metric | Definition | Target |
| --- | --- | --- |
| File coverage | indexed files ÷ discoverable text files | 100% attempted, skips enumerated |
| Coverage honesty | files labelled `FULL`/`PARTIAL`/`LEXICAL_ONLY` consistently with a hand check | no mislabel in sample |
| Symbol precision | extracted symbols that correspond to a real definition | ≥ 0.98 on Python corpus |
| Symbol recall | real top-level/class members extracted | ≥ 0.95 (documented exclusions: `if TYPE_CHECKING`, dynamic `exec`) |
| Span accuracy | `start_line`/`end_line` match the definition including decorators | 100% in sample |
| Edge resolution precision | resolved edges whose target is the actual definition | ≥ 0.90 at `HIGH`, ≥ 0.75 at `MEDIUM` |
| Edge resolution recall | resolvable static edges that were resolved | reported, no target in Phase 1 |
| Issue completeness | every skipped/truncated/parse-failed file has an issue row | 100% |
| Index duration | wall time for a clean index | report p50/p95 per corpus size |

### 1.2 Retrieval

Datasets: 40–60 hand-written questions over 4–6 repositories, each labelled with
the file(s) and line range(s) that contain the answer.

| Metric | Definition | Target |
| --- | --- | --- |
| Hit@5 (file) | answer file appears in top 5 retrieved context items | ≥ 0.85 |
| Span recall | cited answer span overlaps a labelled span | ≥ 0.80 |
| MRR | mean reciprocal rank of the first correct item | report |
| Identifier-variant recall | queries using prose ("causal mask") find the identifier (`CausalMask`) | ≥ 0.80 |
| Exact/regex precision | returned lines actually contain the requested literal/pattern | 100% (mechanical) |
| Empty-result honesty | unanswerable questions return no evidence | 100% |
| Determinism | same snapshot + question ⇒ same context pack (no model) | 100% |

### 1.3 Question answering

| Metric | Definition | Target |
| --- | --- | --- |
| Citation validity | accepted citations that pass verification | 100% by construction; test asserts it |
| Rejection correctness | invalid citations cause withholding, not presentation | 100% |
| Unsupported-answer rate | answers containing claims with no supporting citation | measured by human review; target ≤ 0.10 |
| Refusal correctness | `INSUFFICIENT_EVIDENCE` on questions outside the snapshot | 100% on the negative set |
| No-model utility | digest answers a "where is X" question acceptably | ≥ 0.80 human yes/no |
| Latency | retrieval + model call wall time | report p50/p95 per budget |
| Cost | tokens and cost per answer from recorded invocation rows | report |

### 1.4 Robustness and safety

| Scenario | Expected result |
| --- | --- |
| Repo with `.env`, private key, AWS key in source | files skipped with `SKIPPED_SECRET`; no secret bytes in DB/artifacts |
| Binary, 2 GB file, 300k-line file | skipped or truncated with explicit issue; index completes |
| Symlink to `/etc/passwd`, submodule entry | skipped, recorded, never read |
| Python file with syntax error | `LEXICAL_ONLY` + `PARSE_ERROR`, still searchable |
| Non-UTF8 file | `ENCODING_ERROR`/`LEXICAL_ONLY`, index continues |
| Working tree modified after snapshot | index refused, snapshot `FAILED`, prior index intact |
| Repo `.gitattributes` with external diff driver | no helper executed; snapshot still captured |
| Model returns citation outside context | `CITATIONS_REJECTED`, answer withheld |
| Model returns invalid JSON | `FAILED`, no answer shown |
| `LOCAL_ONLY` with a provider configured | no network call, invocation `REFUSED` |

## 2. Corpora

1. **Fixture repository** (tests): committed history, dirty worktree, `.env`,
   binary, gitignore, broken Python, tests, README. Purpose: invariants, not
   ranking quality.
2. **Small real Python projects** (e.g. `requests`, `flask`, a transformer
   implementation): 50–500 files, familiar code, hand-labelled questions.
3. **Adversarial repository**: generated files for the robustness table.
4. **Large repository** (10k+ files): onboarding time, budget behaviour, FTS
   latency; not used for hand-labelled recall.

## 3. Procedure

1. Pin a snapshot; record its identity in the results file.
2. Run `maya index`; capture `IndexReport`, `INDEX_COMPLETED` payload and
   `maya issues` counts.
3. Run the labelled questions with `--no-model` first; score retrieval metrics
   from the context pack artifacts (paths and spans are machine-readable).
4. Run the same questions with a configured model; score citation validity,
   refusal correctness and answer quality from stored records.
5. Run the robustness table; every case must produce the expected status/issue,
   not merely "not crash".
6. Record results with snapshot identities, tool version, commit and commands
   so the run is reproducible.

## 4. Regression gates

For CI (and required before any release):

- `pytest` passes in full, including snapshot, citation and policy invariants.
- On the labelled corpus, retrieval Hit@5 and citation validity do not drop more
  than 2 points from the recorded baseline.
- No accepted citation fails verification in a generated fuzz set (random
  spans/quotes around context packs).
- No `LOCAL_ONLY` run performs network I/O (asserted by refusing adapter
  construction in tests; a socket-level check is a Phase 2 hardening).
- Indexing the adversarial repository produces no unhandled exception and no
  missing skip issue.

## 5. Reporting template

```
Version / commit:
Snapshot identities:
Corpus size (files, lines, bytes):
Index: duration, files, symbols, edges resolved/total, issues by kind
Retrieval: Hit@5, span recall, MRR, empty-result accuracy
QA (model): citation validity, rejection correctness, unsupported claims,
            refusal accuracy, latency p50/p95, tokens/cost
Robustness: table of scenario → observed status
Deviations from targets and why:
```

## 6. What Phase 1 does not evaluate

- Correctness of an agent's code change (no task execution yet).
- Patch application or test-run outcomes.
- Multi-repository or cross-snapshot reasoning.
- Learned semantic embedding / cross-encoder reranking quality. A deterministic
  hashing-vector embedder, a lexical-overlap reranker and damped PPR exist as
  optional baselines; they are off by default and are **not** a trained semantic
  model. Evaluate them as lexical/graph baselines, not as semantic search.
- Hosted/multi-user security properties.

Those require the Phase 3/4 artefacts (task contracts, isolated worktrees,
runner) and are evaluated only once those exist.

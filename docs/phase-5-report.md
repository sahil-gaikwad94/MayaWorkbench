# Phase 5 report: Experiment Lab and scoped memory

Phase 5 adds a local, pre-registered A/B experiment workflow and a scoped memory
store. It is implemented end to end: immutable definitions, manifest-hashed
execution in fresh workspaces, metric ingestion, a fixed statistical procedure,
fairness classification, reproduction bundles, a verification gate,
content-addressed memory and CLI surfaces. This report records what was built,
what the real-repository run measured, what it exposed, and what is still weak.

Implementation decisions are in
[ADR 0010](adr/0010-experiment-lab-and-memory.md). Schema is
`0006_experiments_and_memory.sql`; package version `0.4.0`; schema bundle `1.4`.

## 1. What runs

```bash
# define (content-hashed, DRAFT)
maya experiment create --snapshot snap_... --name "candidate" \
  --hypothesis "..." --role CANDIDATE \
  --metric 'throughput,direction=HIGHER,aggregation=LAST,primary=true' \
  --seed 1 --seed 2 --seed 3 --seed 4 --seed 5 \
  --dataset-id workload --dataset-version 1 \
  --evaluation-script bench.py --evaluation-script-hash <sha256> \
  --python-version 3.12.3 --cpu-count 8 --device cpu \
  --budget-runs 10 --budget-runtime 1800 \
  --command python bench.py --fast

maya experiment plan <id>          # blockers and unknown protocol fields
maya experiment approve <id>       # explicit user approval
maya experiment run <id>           # one run per declared seed
maya experiment compare <candidate> --baseline <baseline> --remember
maya experiment show <id>          # definition, runs or comparison report
maya experiment bundle <comparison> out/
maya experiment verify-bundle out/
```

`experiment show` accepts either an experiment id (`exp_...`) or a comparison id
(`xcmp_...`); the latter prints the same report that is bundled. Imported runs
(`experiment record`, `experiment ingest`) exist for results produced elsewhere;
they are labelled `IMPORTED` and cannot claim a locally verified environment.

## 2. Statistical procedure (implemented in `experiments/`)

| Concern | Implemented behavior |
| --- | --- |
| Design | paired t-test when both arms share a seed set; Welch otherwise |
| Multiple secondary metrics | Holm adjustment across declared secondaries |
| Effect size | Hedges' g with bootstrap interval |
| Constant differences | no t/p fabricated; reported as identical up to precision |
| Underpowered samples | `INCONCLUSIVE`, never a win |
| Primary metric | drives the verdict; secondaries cannot upgrade it |
| Protocol drift | classified field by field; undeclared drift is `NOT_COMPARABLE` |
| Correctness | secondary metric (e.g. checksum) can contradict a speed win |

The verdict vocabulary is `NOT_COMPARABLE`, `INCONCLUSIVE`,
`NO_DETECTABLE_DIFFERENCE`, `CANDIDATE_ADVANTAGE`, `BASELINE_ADVANTAGE` and
`CANDIDATE_NON_INFERIOR`. Every comparison carries limitations: unknown protocol
fields, imported arms, short seed sets and non-verifiable dataset fingerprints.

A comparison can be turned into an `UNCONFIRMED` memory with author `SYSTEM`
(`--remember`). Only the user can confirm it.

## 3. Verification Gate F

`task_contracts.require_experiment` makes an approved comparison mandatory.
Built-in check `experiment.evidence` (Gate F, mandatory evidence) verifies:

- the comparison is attached to the task;
- both definitions are approved, not revoked;
- the comparison is comparable;
- the candidate was evaluated at the same base commit as the verified run;
- the verdict is not `INCONCLUSIVE` for a required experiment.

The verification report gains an experiment section and a classification;
`NOT_COMPARABLE`/`INCONCLUSIVE` downgrade the decision to
`COMPLETED_WITH_LIMITATIONS` or `BLOCKED`, never `ACCEPTED`.

## 4. Real-repository run

Repository: [`more-itertools`](https://github.com/more-itertools/more-itertools),
HEAD `e3f79f4`, **16,834 Python lines**, 40 tracked files. A throwaway branch
added only a benchmark in commit `1d5de42` (baseline) and a second implementation
in commit `b57aae9` (candidate); the evaluation script is byte-identical on both
arms (`sha256 793281bc...`), so the only treatment is the command plus the
commit. The user checkout was not modified.

### Indexing

| Stage | Result |
| --- | --- |
| Baseline snapshot + index | 40 files, 20,532 lines, 1,683 symbols, 5 issues, **1,391 ms** |
| Candidate snapshot + index | 41 files, 20,546 lines, 1,685 symbols, 5 issues, **1,374 ms** |

### Experiment

| Field | Value |
| --- | --- |
| Design | 5 seeds, one run per seed per arm (10 runs), paired |
| Primary metric | `rows_per_second`, higher is better |
| Secondary | `checksum`, neutral, last value |
| Network enforcement | `network_namespace_user` (rootless user+network namespace) |
| Baseline mean | 3.797e6 rows/s (sd 7.28e5) |
| Candidate mean | 4.226e6 rows/s (sd 1.04e5) |
| Difference | +4.29e5 rows/s, 95% CI [-5.04e5, 1.36e6] |
| Test | paired t = 1.277, df = 4, p = 0.2708, Hedges g = 0.457 |
| Verdict | **NO_DETECTABLE_DIFFERENCE** (comparable, checksum identical) |
| Bundle | 28 files, root `95fd9626...`, `verify-bundle` **ok**, 2 gaps |
| Memory | `NEGATIVE_RESULT`, `UNCONFIRMED`, author `SYSTEM` |

This is the honest outcome the lab is designed to produce: the candidate's mean
was 11% higher, but one baseline seed was slow, the interval included zero and
five seeds do not establish a throughput advantage. The run also shows why
correctness must be a declared secondary: the checksum was identical across
every seed, so the candidate did not trade answers for speed.

Recorded gaps in that bundle:

- both arms leave 14 protocol fields unknown (`batch_size`, dataset fingerprint,
  environment image/lockfile, model identity/checkpoint, preprocessing hash,
  hardware memory, repository patch, sequence length, config hash);
- no dependency lockfile hash was declared.

Those are true gaps for a real reproducer, not cosmetic: the bundle can pin the
commits and commands but cannot prove the Python environment or dataset bytes.

## 5. What the real run exposed, and what was fixed

1. **Comparison bundles recorded one commit.** `reproduce.json` had a single
   top-level `commit_sha` taken from the baseline, so a reproducer would check out
   the wrong arm for the candidate. Fixed: `commits` and `patch_hashes` are
   per-experiment, every command carries `commit_sha`, the single field is set
   only when all arms match, and verification rejects disagreement. A dedicated
   cross-commit bundle test now covers this.
2. **Reproduction gaps were duplicated per arm.** A clean run produced 30 gap
   lines (14 shared unknowns × 2 arms plus lockfile lines), hiding the actual
   information. Fixed: identical unknown sets are reported once for the
   comparison; per-arm detail is kept only when the arms really differ.
3. **`experiment show <comparison>` returned `NOT_FOUND`.** The command only
   accepted definitions, despite comparisons being the primary object users
   paste from `compare`. Fixed: it detects `xcmp_` and renders the comparison
   report (JSON mode returns comparison + report).
4. **Imported arms had no command, but verification treated an empty argv as a
   bundle error.** That made honest imported comparisons unverifiable. Fixed:
   imported arms are a warning tied to the existing "environment not verified"
   gap; reproducible arms still require a non-empty command.
5. **`unshare -n` is unavailable in this sandbox, `unshare -Urn` works.** The
   runner probes both and records the enforcement (`network_namespace_user`);
   without either it fails closed instead of silently allowing network access.

Earlier fixes in this phase, found while building the tests: experiment plan no
longer treats `STATIC` metrics as an execution blocker; verification finding
rows include `id` so bundle verification can read them; the CLI imports
`ExperimentConfigRef`; comparison no longer divides by zero for constant
differences; a missing declared metric makes the run unusable rather than zero;
metric/seed/primary validation rejects ambiguous definitions.

## 6. Test coverage

- `tests/test_experiment_stats.py` (18): t distribution, paired/Welch, Hedges g,
  Holm, constant differences, enum validation.
- `tests/test_experiment_comparison.py` (12): verdict matrix, protocol
  classification, paired seed matching, non-inferiority, unpaired counts.
- `tests/test_experiments.py` (9): full CLI/service flow with real Git
  worktrees, approval, execution, ingestion failure, imported runs, bundle
  tamper detection, cross-commit bundle commits.
- `tests/test_memory.py` (6): confirmation authority, staleness, secrets,
  provenance, scope.
- Gate F tests in `tests/test_verification.py`; CLI tests in `tests/test_cli.py`.

Full suite after the Phase 5 changes: **279 passed** with
`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q --tb=short -p no:cacheprovider`.

## 7. Remaining weaknesses

- **The benchmark is synthetic and self-authored.** The real run proves the
  machinery end to end, not that the candidate is generally faster. A
  wall-clock throughput comparison on a shared 2-CPU VM is noisy; five seeds
  cannot resolve a 10% effect with that variance.
- **No power analysis or sequential stopping.** The lab reports the test it ran;
  it does not tell the user how many seeds would be needed before approving a
  definition. `budget_runs` is a hard cap, not a sample-size calculation.
- **Environment reproducibility is weaker than commit reproducibility.** No
  lockfile hash, image digest or dataset fingerprint was available in this run.
  The bundle records that as a gap, but a reproducer on another machine can still
  get different numbers for environmental reasons.
- **Network isolation is a user namespace, not a container.** It removes routes
  for the child, but it is not a kernel sandbox; a privileged or kernel exploit
  is out of scope, as in Phase 3/4.
- **One host, one user, one SQLite writer.** No distributed workers, no
  cross-machine scheduling, no authentication.
- **Comparison verdicts do not correct across experiments.** Holm applies within
  one comparison. Running many comparisons and reporting the significant one is
  still possible and is not detected.
- **Imported runs cannot be fully trusted.** They are deliberately labelled, but
  a user who imports fabricated metrics gets a comparison over fabricated data;
  only the environment limitation marks it.
- **Memory is not semantic retrieval.** It stores scoped statements and
  provenance; it does not decide relevance, deduplicate near-identical claims or
  resolve contradictions between confirmed memories.
- **Alpha surface.** No `experiment update` (by design), no partial rerun of one
  failed seed, no automatic metric plotting, no HTML report. The JSON/Markdown
  report and bundle are the deliverables.

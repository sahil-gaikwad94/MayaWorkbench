# ADR 0010: Pre-registered experiments and scoped memory

Status: accepted (Phase 5).

## Context

Phases 1–4 answer "what does this code do" and "did this change pass its
declared checks". They do not answer the next question an engineer asks:
*is the candidate actually better than the baseline, and is that comparison
trustworthy?* An agent's own benchmark is not evidence: the metric can be chosen
after seeing the numbers, the two arms can silently differ in something other
than the change, and a small sample can be presented as a win.

Phase 5 adds an Experiment Lab and a scoped memory store. Both are local-first
and use the existing snapshot, artifact, command and verification machinery.

## Decision

### 1. A definition is an immutable pre-registration

An `ExperimentDefinition` is content-addressed over its protocol, command,
metrics, seeds, snapshot and budgets. It starts `DRAFT`, becomes `APPROVED` only
through an explicit user action, and can only become `REVOKED`. There is no
update path: any change (a metric, a seed, a timeout) creates a new definition
with a new hash. The run manifest records both the definition hash and the Git
commit it actually executed, so a later edit cannot silently redefine what was
measured.

Approval is deliberately a separate act from creation. `experiment plan`
computes the blockers (draft status, unusable metrics, missing seeds, fairness
conflicts) without running anything.

### 2. Each run is one replicate, not a benchmark loop

A run executes one command, at one seed, in a fresh worktree of the pinned
snapshot, and ingests one metrics document. The seed is passed as
`MAYA_EXPERIMENT_SEED`. Repeating a run must be an explicit new run with a new
manifest; the workbench never "runs it ten times" inside one manifest. This
keeps every number attributable to one command, one environment and one
artifact.

Declared metrics are extracted from a JSON document on stdout or from a JSON/
CSV file. Undeclared metrics are recorded as observations but excluded from the
comparison, with a warning. A declared metric that is missing makes the run
unusable rather than zero.

Inputs are hashed before execution (snapshot identity, evaluation-script hash,
declared data files) and the observed hashes are stored on the run. A mismatch
blocks the run. The evaluation script must be inside the snapshot; an absolute
path or `..` is refused.

### 3. Fairness is a classification, not a promise

`protocol_values` produces the comparable protocol surface: repository commit,
declared patch, command, dataset id/version/fingerprint, preprocessing hash,
model identity/checkpoint, environment image/lockfile, Python version, hardware
class, CPU count, device, batch size and sequence length.

Each field is classified as `MATCH`, `EXPECTED_TREATMENT`, `UNEXPECTED_TREATMENT`,
`MISSING` or `UNKNOWN`. The default treatment dimensions are `command`,
`repository.commit` and `repository.patch`, because comparing two commits is the
normal case. A difference in any other field makes the comparison
`NOT_COMPARABLE`; an unknown field on both sides is a recorded limitation, not a
match.

A command difference is expected only when the declared treatment says so. This
is what stops "baseline ran with one batch size and candidate with another" from
being reported as a result.

### 4. The statistics are fixed before the verdict

The primary metric and its direction are declared in the definition. Secondary
metrics use Holm adjustment. The comparison chooses a paired t-test when both
arms used the same seed set and an unpaired Welch test otherwise; Hedges' g
(with a bootstrap interval) reports effect size. A difference with zero variance
produces no t statistic and no p-value rather than a divide-by-zero or a
fabricated `p = 0`.

Verdicts are deliberately conservative:

| Verdict | Meaning |
| --- | --- |
| `NOT_COMPARABLE` | protocol differs outside the declared treatment; no statistical claim |
| `INCONCLUSIVE` | too few usable paired runs, or an undefined/underpowered test |
| `NO_DETECTABLE_DIFFERENCE` | interval includes zero, or the test did not reject |
| `CANDIDATE_ADVANTAGE` / `BASELINE_ADVANTAGE` | significant, directionally consistent, primary metric |
| `CANDIDATE_NON_INFERIOR` | the candidate is not detectably worse within the declared non-inferiority margin |

Secondary metrics and correctness checks cannot upgrade a primary result. A
constant-difference metric is reported as "identical up to precision", never as
a significant win.

### 5. Reproduction bundles carry both arms

`experiment bundle` writes the definitions, comparison, manifests, observations,
run outputs and report, with a root hash over every file. Because a comparison
can span two commits, `reproduce.json` records `commits` and `patch_hashes` per
experiment and every command carries its own `commit_sha`; the single
`commit_sha` field is set only when all arms used the same revision. Verification
recomputes every hash and checks that commands, seeds and commits agree.

Unknown protocol fields are reported once for the comparison ("both arms leave
these fields unknown") instead of once per arm, so a reader can see the actual
gaps.

### 6. Verification Gate F consumes experiment evidence

`task_contracts.require_experiment` makes an approved experiment comparison
mandatory evidence. The verification registry adds `experiment.evidence`, which
checks that the comparison is attached to the task, is comparable, evaluates the
same base commit as the verified run, and has a verdict consistent with the
task's requirement. A missing or inconclusive experiment becomes a limitation or
a blocking check according to the task contract; it cannot be silently ignored.
The verification bundle embeds the `experiments/` evidence it cites.

### 7. Memory is scoped and never self-confirmed

`MemoryStore` stores three kinds of statement: `FACT`, `DECISION` and
`NEGATIVE_RESULT`, each tied to a project and optionally a snapshot. Only the
user can `CONFIRM` a memory; model-authored memories are `UNCONFIRMED`, and a
model cannot confirm its own statement. Comparison-derived memories are created
`UNCONFIRMED` with author `SYSTEM`, even when the verdict is a candidate
advantage. A memory tied to a commit becomes `STALE` when the project's current
snapshot moves past it, rather than being silently reused. Secret-looking values
are refused, and provenance references are validated against the store.

## Consequences

- The workbench can now say "no detectable difference" honestly, including when
  the observed mean moved 11% but the interval included zero.
- Pre-registration costs a definition per experiment. That is intentional; the
  CLI keeps the common case to one command.
- A user who changes a metric after seeing results must create a new definition.
  The old one remains visible with its verdict.
- The lab is not a distributed benchmark service: one host, one user, local
  SQLite, wall-clock measurements. Environmental drift between runs is a
  recorded limitation, not something the statistics can remove.
- Memory is advisory context. Gate F and verification never treat an
  `UNCONFIRMED` memory as evidence.

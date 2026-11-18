# ADR 0009: Verification and evidence are separate, append-only records

- Status: Accepted
- Date: 2026-11-18
- Supersedes: none
- Related: ADR 0008 (controlled engineering agent), ADR 0005 (no empty scaffolding)

## Context

Phase 3 records what an agent did: approvals, tool calls, a captured patch and a
structural diff review. None of that is evidence that the change works. A
reviewer (human or model) looking at a patch cannot tell whether the command
output came from the candidate tree, from the base tree, or from a different
checkout. Letting the same model that wrote the patch grade it reproduces the
failure the product exists to prevent.

The specification's Phase 4 requires deterministic checks, baseline comparison,
test-integrity warnings, ML-specific invariants, an independent read-only
reviewer, an evidence graph, a report and an exportable bundle.

## Decision

1. **Verification is a new append-only record set, not a mutation of a run.**
   `verification_runs` (attempts), `verification_checks`, `verification_findings`,
   `verification_reports` and `evidence_links` are written once. A re-run creates
   attempt *n+1*; earlier checks are never rewritten, so a report can always be
   reproduced from the rows it named.

2. **A contract authorizes checks; a verifier never invents authorization.**
   Built-in checks always run. Command and ML-invariant checks must be declared
   in the task contract, and command checks require a consumed
   `COMMAND_EXECUTION` approval whose `expected_command_sha` matches the
   contract and plan. A check added at verify time without that approval is
   `BLOCKED`, not silently executed.

3. **Baseline comparison runs the same argv on the base revision.** A fresh
   detached worktree at the pinned commit has the snapshot's worktree patch
   applied, so "the check failed before" and "the patch caused a regression" are
   distinguished by execution, not by assumption. A check that cannot run on
   either side is a warning (`BASELINE_UNAVAILABLE`), never a pass.

4. **A failure that predates the patch is not attributed to the patch.**
   `preexisting_failure` is recorded and becomes a limitation. It still cannot
   verify the criterion.

5. **The reviewer is advisory and structurally unable to overrule evidence.**
   It receives a bounded, read-only view and returns structured findings.
   Findings naming paths outside the captured diff are rejected and recorded as
   `MODEL_REVIEW_UNGROUNDED`; a model finding never turns a failed check into a
   pass. A reviewer BLOCKER produces `NEEDS_HUMAN_DECISION`.

6. **The decision follows fixed rules over recorded rows.**
   Hard required failure → `REJECTED`; blocked → `BLOCKED`; no passing required
   functional check / no declared functional check → `INCONCLUSIVE`; unverified
   criteria, warnings or pre-existing failures → `COMPLETED_WITH_LIMITATIONS`;
   everything verified with no warnings → `ACCEPTED`. `task finish --override`
   records a human override as an event and limitation without changing the
   report.

7. **An evidence bundle is self-verifying.** It contains the report, checks,
   criteria, patch and cited artifacts plus a manifest of hashes. The verifier
   recomputes every hash, rejects missing, extra or unlisted files, and checks
   that the report names the patch and checks the bundle actually contains.

8. **ML invariants are generated from validated parameters, never string
   interpolation.** The template name and typed parameters are stored in the
   contract; the script is rendered under the run home and executed through the
   same argv policy, timeout, process-group cleanup and rlimits as any command.
   A missing dependency is an explicit `SKIP` marker, not a failure and not a
   pass.

## Consequences

- A verification report is only as strong as the checks declared. The product
  says `INCONCLUSIVE` rather than pretending a source inspection is a test.
- Running a check requires explicit command authorization. Users who want
  automatic test execution must declare it in the contract.
- Local-only use works without a model: the reviewer is `SKIPPED`, the report
  records that limitation, and the decision is never inflated to `ACCEPTED`
  without saying so on the run.
- Baseline execution costs one extra run per declared check and requires the
  base revision to be executable locally. When it is not, the report records a
  limitation rather than a regression claim.
- The evidence bundle leaks nothing that was not already recorded; it is an
  export, not a redaction mechanism.

## Alternatives considered

- **Mutating a run with a `verified` flag.** Rejected: no audit trail, no way to
  reproduce a past decision.
- **Letting the agent self-report `checks_run`.** Rejected: claims would be
  unverifiable and the same model would grade itself.
- **Always running all tests automatically.** Rejected: execution is arbitrary
  code; it needs an explicit, scoped, single-use approval.
- **Treating reviewer silence as approval.** Rejected: a skipped or failed
  review is recorded as such and can never be mistaken for "no concerns".

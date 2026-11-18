# ADR 0007 — Claims, deterministic mapping and honest readiness

Status: accepted (Phase 2)

## Context

Phase 2 must answer "what does this paper require, and how much of it does this
repository actually implement?" without turning the answer into an LLM opinion.
The failure modes that matter are concrete:

- a paper sentence and an engineering guess get merged into one "requirement";
- a symbol named `Attention` is treated as proof that attention is implemented;
- "absent" is claimed while the parser only saw 40% of the repository;
- a readiness number looks like a probability and hides missing information;
- a later memo rewrites what the earlier memo said the paper meant.

## Decision

### Claim categories are separate, and provenance is mandatory

Four types, never merged: `EXPLICIT_SOURCE_CLAIM` (a verified quote),
`INFERRED_REQUIREMENT` (a derivation note plus basis passages),
`USER_REQUIREMENT` (a local requirement) and `IMPLEMENTATION_DECISION` (a local
decision, explicitly marked as not the paper). An explicit claim must carry a
quote that appears verbatim in a stored passage; a paraphrase is refused with
`CitationError`. Non-explicit claims must carry a derivation note and must not
present a paper quote as their own statement. The memo labels every row by
category, so "the paper says" and "we decided" cannot be confused.

### The claim graph is a DAG, validated on read

Claims form a hierarchy (`parent_claim_id`) plus ordering edges
(`claim_dependencies`). Validation reports missing dependencies, self-edges,
cycles, duplicate ids, and cross-document/cross-project edges. A cycle is
reported and the affected claims become `unorderable`; nothing is silently
dropped or reordered to make the graph look valid.

### `PRESENT` is a mechanical verdict, not a name match

A deterministic `PRESENT` requires **all** of:

1. the claim declares machine-checkable acceptance signals;
2. every declared signal occurs in executable code (not comments, docstrings or
   strings) found during discovery;
3. at least one occurrence is inside a definition-level span (function, method
   or class) with `FULL` parser coverage;
4. no forbidden signal occurs in the candidate evidence.

Derived signals (identifiers lifted from the claim text) can never yield
`PRESENT`; they can yield `PARTIAL` or `UNCERTAIN`. `ABSENT` is only reported
when declared signals were searched exhaustively and no coverage or discovery
limit applies. `NOT_APPLICABLE` is manual-only. `CONFLICTING` requires a
forbidden signal to actually appear in code, and the evidence is stored.

Every mapping cites exact lines; verification re-reads the pinned snapshot and
requires exact equality with the stored excerpt and SHA-256. A mapping whose
evidence no longer matches is reported as unverified and is a readiness blocker.

### Model adjudication is bounded and refusable

The model only sees a numbered list of discovered spans (`S1`…`S6`) and returns
a status, rationale, gaps and span references. The service then:

- rejects any reference to a span that was not supplied;
- re-reads each cited span, recomputes signal coverage and definitionality;
- rejects `PRESENT` without code evidence for every declared signal;
- rejects `ABSENT` if a declared signal was actually found;
- rejects `CONFLICTING` without a cited contradicting span.

A rejected adjudication is recorded as an event (`CLAIM_MAPPING_REJECTED`) and
the deterministic verdict is kept. If a model is requested and none is
callable, the CLI fails instead of silently producing a deterministic answer.

### Readiness is a weighted heuristic with explicit unknowns

Six factors have published weights: paper detail (0.22), code mapping (0.30),
data access (0.14), compute (0.10), licensing (0.08) and evaluation (0.16).
Each factor is `KNOWN` with a risk value and evidence, or `UNKNOWN` with the
reason it cannot be assessed. `UNKNOWN` never contributes zero risk. An overall
value is reported only when at least 75% of the weight is known; otherwise the
label is `INSUFFICIENT_INFORMATION` and `overall_risk` is `None`. The result
carries `is_probability = false`, the score version, assumptions, limitations
and blockers. The code-mapping factor uses accepted claims when any exist, so
unreviewed machine claims cannot dominate the score.

### Memos are versioned; the interpretation is immutable

A memo's source interpretation is delimited by markers and hashed. Appending a
version reuses that block verbatim after verifying the hash, then appends
observed deviations. A tampered interpretation is refused rather than silently
rewritten. Memos are stored as artifacts and rows, and they cite passages (id +
quote hash) rather than restating evidence from memory.

## Alternatives rejected

- **LLM writes the memo and the verdicts.** Rejected: the memo must be
  reproducible from stored evidence; models may only adjudicate among supplied
  spans, and only where deterministic rules cannot decide.
- **Single `requirement` field with a category tag.** Rejected: it invites
  merging a paper sentence with an engineering decision.
- **`PRESENT` from symbol/identifier similarity.** Rejected: this is exactly the
  failure the product exists to prevent.
- **A numeric "reproducibility score" without unknowns.** Rejected: it hides
  missing data and invites overconfidence.
- **Editing the memo body in place.** Rejected: it destroys the audit trail.

## Consequences

- More storage per claim (quotes, revisions, evidence, risks) and more steps for
  the user (extract, review, map, memo). This is deliberate: the artefacts are
  the product.
- Mapping can honestly say "uncertain" more often than a demo would. That is
  the correct trade: a false `PRESENT` is worse than an uncertain one.
- Readiness frequently reports `INSUFFICIENT_INFORMATION` until data, compute,
  licensing and evaluation facts are recorded. The factor table shows exactly
  what is missing.
- Model costs are bounded: at most six spans are sent per claim, only when
  `--model` is passed.

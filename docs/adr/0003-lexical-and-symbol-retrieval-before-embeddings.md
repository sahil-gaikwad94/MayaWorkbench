# ADR 0003 — Lexical and symbol retrieval before embeddings

Status: accepted (Phase 1)

## Context

Repository search must handle two query shapes:

- identifiers the user half-remembers (`CausalAttention`, `masked_fill`);
- concepts expressed in prose ("how is the causal mask applied?").

Embedding search is attractive for the second shape but introduces opaque
ranking, a model dependency, and a failure mode where plausible but wrong code
is retrieved with high confidence. The specification requires exact identifiers
and symbols to be the baseline, with labelled uncertainty.

## Decision

Phase 1 implements:

1. **Symbol index** from the Python AST, searched by name/qualified name with
   deterministic ranking (exact name first).
2. **FTS5 lexical index** over identifier-expanded tokens: split camelCase and
   snake_case, keep the raw identifier, and index both. BM25 ranks matches.
3. **AND-first, OR-broadened queries** — strict co-occurrence first; when it
   yields fewer than five hits, OR matches are merged at reduced weight.
4. **Exact and regex scan** over raw `file_lines`, so literal and pattern
   queries never depend on tokenisation.
5. **Compound identifier generation** for prose pairs (`causal masking` →
   `CausalMasking`, `causal_masking`, `causalMasking`).
6. **One-hop relationship expansion** through resolved symbol edges only.

Embeddings are explicitly deferred. Relevance is never invented: an empty result
is an empty result, reported as such.

## Consequences

- Positives: deterministic, inspectable ranking; no extra dependency; works
  offline; FTS expressions are recorded in diagnostics.
- Negatives: paraphrase recall is limited (synonyms that share no token will
  miss); exact/regex scans are linear in indexed lines; non-English prose gets
  little help from compound generation.
- Revisit when: the labelled evaluation corpus shows Hit@5 below target and
  lexical improvements are exhausted. Any embedding layer must preserve the
  citation contract and report its own uncertainty, not replace exact search.

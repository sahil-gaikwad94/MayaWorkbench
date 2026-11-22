# Spec — research evidence quality: disagreement, versions, citations (WP-RESEARCH-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — records, append-only migration 0011, stores, services, CLI and tests.
**Vision source:** v2.1 §9.2–9.6; Audit A MW-09; Audit B §4 "R2I quality".

## Problem

Three evidence-quality gaps remain in the flagship R2I workflow:

1. When the paper text, the implementation and the experiment result disagree,
   the only machine-readable signal is `ClaimMappingStatus.CONFLICTING` from the
   paper↔code axis. There is no typed record co-locating paper↔code,
   paper↔experiment and code↔experiment disagreements, and no resolution state.
2. Paper version, correction and retraction metadata are barely modelled:
   `paper_version` is a free string on `SourceMetadata`; there is no status, no
   correction/retraction relation, and no way to alert which claims cite a
   superseded version.
3. Citations exist only as embedded `SourceLocation` JSON on a claim — no
   standalone, version-bound, granular citation record with verification
   provenance.

## Product outcome

Typed, append-only records for citations and disagreements, version/correction
status on source metadata, and CLI/service surfaces that can record, list and
resolve them. Readiness and reproduction remain authoritative; these are inputs,
not new verdict authorities.

## Scope (in)

- Enums: `PaperStatus` (ACTIVE/CORRECTED/RETRACTED/SUPERSEDED), `DisagreementKind`
  (PAPER_VS_CODE/PAPER_VS_EXPERIMENT/CODE_VS_EXPERIMENT/SOURCE_VERSION_CONFLICT),
  `CitationGranularity` (PASSAGE/SENTENCE/SUBCLAUSE/EQUATION/TABLE_CELL),
  `CitationRelation` (SUPPORTS/QUALIFIES/CONTRADICTS).
- Records: `ClaimCitation` (claim + document version + passage + quote hash +
  sub-passage offsets + granularity + relation + verification provenance +
  supersedes link) and `EvidenceDisagreement` (claim, optional
  document/passage/mapping/run, kind, detail, status, resolution, actor/time).
- `SourceMetadata` gains status/version/correction/retraction fields.
- Append-only migration `0011_research_evidence_quality.sql`.
- Store/JSON mappings for both records and the new metadata columns.
- Service methods on `ClaimService` (cite, verify_citations, record_disagreement,
  list_disagreements, resolve_disagreement) and version alert projection in
  `DiscoveryService.versions`.
- CLI: `research claim cite`, `research claim disagreements`,
  `research claim disagree`, `research claim resolve-disagreement`,
  `research versions --alerts`.
- Tests through all layers; status docs updated.

## Explicitly out

- Auto-retraction of claims (no new authority).
- External provider APIs for correction feeds; this consumes recorded metadata.
- New model calls.

## Design constraints

- Migration is append-only; never edit 0001–0010.
- Every citation quote is verified verbatim against the pinned passage via the
  existing `verify_source_location`/`_verified_location` authority before it is
  stored. A failed quote is rejected, never stored as "unverified citation".
- A disagreement must name at least two of: verified paper quote, mapping
  evidence span, experiment observation. A free-text-only disagreement is
  refused.
- Resolution is append-only: resolve/dismiss updates status and records actor,
  time and reason; history is preserved.
- A retracted/superseded source metadata status downgrades affected claims in
  readiness/reproduction inputs but never fabricates a new claim verdict.

## Acceptance criteria

1. A citation with a verbatim quote and a real passage is stored with its
   document version, passage id, quote hash and granularity; a paraphrase is
   rejected with a typed error.
2. A disagreement with a paper quote + mapping evidence is recorded and listed;
   a text-only disagreement is refused.
3. Resolving a disagreement preserves the original record and records actor/time.
4. `research versions --alerts` lists claims affected by a RETRACTED or
   SUPERSEDED source with the exact document/version identifiers.
5. Full suite green; status docs and test matrix updated; no leaked artifacts.

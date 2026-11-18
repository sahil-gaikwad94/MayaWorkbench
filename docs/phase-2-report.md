# Phase 2 report — research intelligence and claim mapping

Status: complete for the scope in
[docs/00-product-freeze.md](00-product-freeze.md) and
[ADR 0007](adr/0007-claims-mapping-and-readiness.md). Phase 3–4 remain deferred
and are not stubbed.

## 1. What was built

A new `research` package (~6.3k lines) and `mapping` package (~2.2k lines), plus
a schema migration, storage, composition-root wiring, CLI commands, exported
contracts and 70 new tests.

### 1.1 Documents and passages

- `research/pdf.py` — two extraction paths. `pypdf` is used when the optional
  extra is installed; otherwise a conservative builtin parser reads uncompressed
  and Flate content streams, reports Type0 fonts without `ToUnicode`, and marks
  pages with no text layer instead of inventing prose.
- `research/fetch.py` — http(s)-only retrieval with credential-URL refusal,
  DNS/redirect SSRF checks against public addresses only, byte/time/redirect
  caps, document content-type checks and an injectable opener.
- `research/metadata.py` — front-matter, first-heading and PDF-document-info
  metadata. Every field records provenance (`front_matter`, `body`, `filename`,
  `cli`). A filename title is always flagged `TITLE_NOT_IN_SOURCE`; a PDF
  creation date is kept as `extra`, never as a publication date.
- `research/passages.py` — structure-aware segmentation for Markdown
  (headings/fences/tables/lists/display maths), heuristics for text and PDF
  (numbered/caps headings, equations by symbols/labels/numbering), oversized
  chunks split at sentence then word boundaries.
- `research/ingestion.py` — immutable versions keyed by `document_key`,
  duplicate-content reuse, explicit `--version-of` superseding, artifacts for
  source bytes / text / extraction report, a `FAILED` version row when text
  cannot be extracted, and `reextract` that refuses once claims reference the
  passages.

Invariant enforced at ingestion: `stored_text[char_start:char_end] == text`,
`text_sha256 = sha256(text)`, `quote_hash = sha256(normalise(text))`.

### 1.2 Claims

- `research/claims.py` — deterministic extraction from normative sentences
  (confidence by marker strength, reference/appendix sections skipped), a model
  extraction path whose payload can only contain explicit or inferred claims,
  manual authoring for all four categories, verified quote-only explicit claims,
  review, append-only revisions, risk factors, and re-extraction that preserves
  every reviewed claim.
- `research/dag.py` — cycle detection (strongly connected components), missing
  dependencies, self-edges, missing/cyclic parents, duplicate ids,
  cross-document and cross-project edges, topological order and depth.
- `research/models.py` — computed contracts (extraction report, claim extraction
  payload/schema, DAG validation, matrix, readiness, memo build) and the strict
  JSON schema used for model extraction.

### 1.3 Claim-to-code mapping

- `mapping/signals.py` — whole-token signal matching with code/comment/docstring
  /string classification, identifier derivation, requirement terms.
- `mapping/evidence.py` — candidate discovery from target symbols, identifiers,
  exact signals, target paths and requirement-term fallback; every candidate is
  analysed for signal coverage, forbidden hits, constructs and strength, with
  parser-coverage and discovery caps recorded.
- `mapping/rules.py` — rule pack `mapping/v1`. `PRESENT` requires all declared
  signals in executable code, at least one in a definition-level span with
  `FULL` coverage, and no forbidden signal. Derived signals cap at `PARTIAL`.
  `ABSENT` is withheld when coverage or discovery is limited.
- `mapping/service.py` — persistence, matrix generation, evidence verification
  against the pinned index, manual overrides (`PRESENT` needs evidence and all
  declared signals; `NOT_APPLICABLE` is manual-only) and bounded model
  adjudication over `S1…S6` with refusal of unsupplied refs, unsupported
  `PRESENT`, false `ABSENT` and evidence-free `CONFLICTING`.
- `mapping/prompts.py` / `mapping/models.py` — the span-only adjudication prompt
  and payload schema.

### 1.4 Readiness and memo

- `research/readiness.py` — six weighted factors (paper detail 0.22, code
  mapping 0.30, data access 0.14, compute 0.10, licensing 0.08, evaluation
  0.16), `KNOWN`/`UNKNOWN`, blockers, assumptions and limitations. No overall
  value below 75% known weight; `overall_risk` is `None` and the label is
  `INSUFFICIENT_INFORMATION`. `is_probability = false`, score version recorded.
- `research/memo.py` — deterministic 14-section memo assembled from stored
  evidence, with citation verification marks, matrix table, readiness table and
  verbatim source references. The interpretation block is marker-delimited and
  hashed; appending reuses it byte for byte (hash-verified) and appends
  deviations.

### 1.5 Persistence, events, CLI, contracts

- `storage/migrations/0002_research_and_claims.sql` — ten additive tables.
- `storage/stores.py` — document, passage, claim, dependency, revision, risk,
  mapping, evidence, memo and extraction stores.
- New event types `SOURCE_INGESTED`, `SOURCE_EXTRACTION_FAILED`,
  `CLAIMS_EXTRACTED`, `CLAIM_CREATED/UPDATED/REVIEWED`, `CLAIM_DAG_VALIDATED`,
  `CLAIM_MAPPING_CREATED/UPDATED/REJECTED`, `RESEARCH_MEMO_CREATED`.
- Artifact kinds `SOURCE_DOCUMENT`, `SOURCE_TEXT`, `EXTRACTION_REPORT`,
  `CLAIM_SET`, `CLAIM_MATRIX`, `RESEARCH_MEMO`.
- CLI:
  `research add|list|show|extract|dag|readiness|memo`,
  `research claim add|list|show|edit|review`,
  `map [run|set|show]`, all with `--json`.
- Schema bundle `1.1` exports the new domain and computed contracts.

## 2. Verification environment

| Item | Value |
| --- | --- |
| OS | Linux (container) |
| Python | 3.12.3 |
| Git | 2.43.0 |
| Install | `uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'` |
| Runtime dependency | `pydantic>=2.7` (only) |
| Optional | `pypdf` (not installed; the builtin PDF path is what the suite exercises) |

```
$ .venv/bin/python -m compileall -q src tests && echo COMPILE_OK
COMPILE_OK

$ .venv/bin/pytest
154 passed in 9.65 s   # original Phase 2 run
# Current, measured suite result: see the post-audit section at the end.
```

New tests:

| File | Tests | Focus |
| --- | --- | --- |
| `test_research_documents.py` | 15 | metadata provenance, exact passage spans, duplicates/versioning, PDF pages and missing text, fetch guards, re-extraction refusals, paraphrase rejection |
| `test_claims.py` | 19 | deterministic extraction, reference-section skip, forced re-runs, reviewed-claim preservation, quote/basis invariants, review and revisions, DAG cycles/missing deps/cross-document parents, risk factors |
| `test_mapping.py` | 19 | strict signal matching, `PRESENT` conditions, derived-signal caps, `CONFLICTING`/`ABSENT`, rejected claims, matrix/unmapped, evidence tampering, manual overrides, span parsing, model accept/refuse paths, unavailable model |
| `test_readiness_memo.py` | 14 | unknown factors, blockers, accepted-claim weighting, licensing, all memo sections, artifacts, snapshot matrix, verbatim append, tamper refusal, memo listing |
| `test_research_cli.py` | 3 | full CLI workflow (add → extract → review → map → readiness → memo → db), usage errors, schema export |

The existing 84 Phase 0/1 tests still pass unchanged except
`test_migrations_apply_once_and_record_checksums`, which asserted exactly one
migration and now asserts one result per migration file and the recorded
checksums.

## 3. End-to-end run (fresh workbench)

The fixture paper is a Markdown document with front matter (title, two authors,
organisation, date, CC-BY-4.0, DOI), an abstract, a masking method section, a
complexity section, an experiments section and a limitations paragraph. The
fixture repository contains `sparse_router.py` with `TOP_K`, `routing_scores`,
`top_k_blocks`, `load_balancing_loss` and `sparse_attention`.

```
$ maya init --project final-paper
$ maya research add paper.md
Document doc_01M7KX8JJNCQ1ESJX5C3GC9YHN (version 1)
title      Scaling Attention with Sparse Routing
media      MARKDOWN

$ maya research extract <doc>
method       DETERMINISTIC
created      4
dropped      0
eligible sentences 9

$ maya research claim add --type USER_REQUIREMENT \
    --title "Sparse top-k routing" \
    --statement "The router must select top-k key blocks and apply the load-balancing loss." \
    --expected-signal top_k_blocks --expected-signal load_balancing_loss --accept
$ maya repo add ./repo && maya snapshot create && maya index
$ maya map
Mapped 5 claim(s) — 0ba22bf34496
rule pack   mapping/v1
counts      {"ABSENT": 2, "CONFLICTING": 0, "NOT_APPLICABLE": 0,
             "PARTIAL": 0, "PRESENT": 1, "UNCERTAIN": 2, "UNMAPPED": 0}
```

The user requirement is `PRESENT` because both declared signals occur in
executable code, one in `top_k_blocks`/`load_balancing_loss` definitions. The
two explicit paper claims that declare no acceptance signals are reported as
`UNCERTAIN`/`ABSENT` with reasons (never a symbol-name-only `PRESENT`).

```
$ maya research readiness --code
result         INSUFFICIENT_INFORMATION
overall risk   insufficient information
known weight   0.60 of 1.00
score version  readiness/v1
snapshot       0ba22bf34496

factor                    weight  state    risk
Missing paper detail      0.22    KNOWN    0.00
Code mapping uncertainty  0.30    KNOWN    0.00
Data access               0.14    UNKNOWN  —
Compute availability      0.10    UNKNOWN  —
Licensing and reuse       0.08    KNOWN    0.10
Evaluation gaps           0.16    UNKNOWN  —
```

This is the intended behaviour: mapping is complete and the licence is known,
but no data, compute or evaluation facts were recorded, so no overall score is
reported. Recording them through `--risk` (or claims with validation methods)
moves the same document to a numeric label.

```
$ maya research memo create --code --out memo.md
Memo memo_… (version 1)
artifact               art_…
readiness              INSUFFICIENT_INFORMATION
interpretation sha256  9c084637edcff752…

$ maya research memo create --code --append \
    --deviation "Reproduced top-k routing; accuracy not reproduced."
Memo memo_… (version 2)
supersedes             memo_…
interpretation sha256  9c084637edcff752…   # identical to version 1
```

The memo contains the 14 sections, a verified citation per explicit claim
(`[verified]`, passage id, page/section, quote hash), the claim-to-code matrix,
the readiness factor table with its assumptions/limitations, and the append-only
deviations list.

## 4. Deliberate limitations

- **PDF quality depends on a text layer.** The builtin parser handles classic
  and Flate streams and object streams; scanned pages are reported as missing,
  never OCR'd. Installing `pypdf` improves encodings and `ToUnicode` handling.
  A PDF whose every page has no text is stored as a `FAILED` version with an
  extraction report, not as a successful empty document.
- **Retrieval is local and guarded, not a web crawler.** http(s) only, public
  addresses only, no credentials, bounded bytes/time/redirects, document content
  types only. DOI resolution goes through `doi.org` and is subject to the same
  guard.
- **Mapping evidence is scoped to the pinned snapshot.** `ABSENT` means "not
  found in the inspected snapshot and candidate scope", and is downgraded to
  `UNCERTAIN` when parser coverage or discovery is truncated. It never means
  "nowhere".
- **`PRESENT` is not validation.** It records that the required behaviour
  appears implemented, not that it was tested or reproduces the reported numbers.
  The readiness and memo state this explicitly.
- **Model adjudication is optional and bounded.** No model was called in the
  suite (a provider double is injected); at most six spans are sent per claim;
  invalid verdicts are refused and the deterministic verdict is kept. The CLI
  fails if `--model` is requested with no callable model instead of silently
  falling back.
- **The memo is generated deterministically.** No model writes prose in this
  phase; every statement is either a stored claim, a quoted passage, a matrix
  row or an explicit "not recorded" statement. A future phase may add an
  editorial layer, but it would have to cite the same evidence.
- **Claim hierarchy is per document.** Cross-document relationships are modelled
  as dependencies, not parents; cross-document parents are refused.
- **Readiness weights are a product judgement, not a measurement.** They are
  published in the output, versioned (`readiness/v1`) and applied only to
  recorded facts. Changing them requires a new score version and an ADR update.

## Post-audit remediation (18 November 2026)

An adversarial audit of Phases 1–3 (MWB-001 … MWB-014) found that this report
cited test files that were not present in the shipped archive. Both files now
exist. This section records the measured build result, not a narrative claim.

**Environment:** Python 3.12.3, Git 2.43.0, SQLite 3.45.1.
**Command:** `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --tb=no -p no:cacheprovider`
**Result:** `191 passed in 17.69s` (whole suite, all phases; the per-file counts
in the table above describe the original report and are not re-asserted here).

Phase 2 fixes delivered in this change:

- **MWB-004** — incremental Flate decompression with a per-stream byte budget and
  page/object/character caps; the optional `pypdf` path is bounded too.
- **MWB-005** — Python signals are classified with `tokenize` plus AST docstring
  spans, so inline comments, strings, f-strings and docstrings are never
  executable evidence. `PRESENT` now requires every declared signal to co-occur
  in one definition-level span; scattered signals are `PARTIAL`.
- **MWB-008** — remote fetch resolves once per redirect hop, connects to the
  validated address, verifies the connected peer, and revalidates every redirect
  before use.
- **MWB-012** — the `mayaworkbench[pdf]` optional extra now exists.
- **MWB-013** — local document size is checked with `stat` before reading, then
  read with a hard byte bound.
- **MWB-001 (Phase 2 part)** — `maya research …` and `maya map …` are registered;
  `tests/test_research_cli.py` and `tests/test_research_documents.py` now exist.

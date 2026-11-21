# ADR 0014 — Multi-language adapters with explicit heuristic provenance

- Status: Accepted
- Date: 2026-11-21
- Applies to: `indexing/language_adapters.py`, `indexing/languages.py`, `indexing/indexer.py`,
  `indexing/map.py`, `domain/enums.py`

## Context

The audit (MW-P1-01) requires cross-language call chains, build graphs and type references
for an engineering workbench. The implementation was Python-AST only; other languages were
lexical-only and searchable but had no symbols. A full Tree-sitter adapter set is a larger
effort and the audit requires measured benefit before claiming semantic understanding.

## Decision

- Add a `ParserAdapter` protocol and a `HeuristicAdapter` that discovers declarations,
  imports and call sites in JavaScript/TypeScript/Go/Rust with bounded regular expressions.
- Every produced symbol has `LOW` confidence and every edge has `HEURISTIC` provenance;
  file coverage is `PARTIAL`, never `FULL`, and file status stays `LEXICAL_ONLY`. The
  repository map reports `heuristic_languages` separately from AST and lexical-only sets.
- Add `RelationSource` (`AST_IMPORT`, `TYPE_REFERENCE`, `CALL_SITE`, `BUILD_DEPENDENCY`,
  `CONFIG_REFERENCE`, `HEURISTIC`) as the vocabulary for future adapters; only AST and
  HEURISTIC are currently produced.
- Python's AST path is unchanged. A future Tree-sitter adapter can replace one language
  without changing the indexer or retrieval layers.

## Consequences

- Multi-language fixture tests prove symbols are searchable through `maya search
  --symbols` and renderable through `maya symbol show`, with honest provenance.
- No claim of semantic understanding is made for non-Python languages; a measured gold
  set must justify replacing heuristics with Tree-sitter.

# Spec — optional LSP client adapter (WP-LSP-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — opt-in stdio LSP adapter, three CLI queries, fail-closed unsupported path.
**Vision source:** Audit B FIX-06 item 2; Audit A MW-08.

## Problem

Non-Python languages are now parsed by an optional Tree-sitter adapter (real CST,
HIGH confidence) with a deterministic regex fallback. Syntax trees still cannot
resolve definitions/references across files or provide diagnostics. The audit's
FIX-06 calls for an optional LSP client adapter using stdio JSON-RPC to
`pyright-langserver`, `typescript-language-server`, `rust-analyzer` and `gopls`
for on-demand `go-to-definition`, `find-references` and `hover`. This is the last
concrete code-intelligence upgrade not yet implemented.

## Product outcome

An opt-in, fail-closed LSP adapter that can answer definition/reference/hover
queries for a pinned snapshot when an operator has configured an allowlisted
language-server binary. Without that configuration it returns an explicit
`unsupported` status; it never fabricates semantic edges.

## Scope (in)

- `indexing/lsp_client.py`: strict LSP JSON-RPC 2.0 stdio transport (`initialize`,
  `initialized`, `textDocument/didOpen`, `definition`, `references`, `hover`,
  `shutdown`, `exit`), bounded line reads/timeouts, sanitized environment, no
  shell, child termination and pipe draining in `finally`.
- Allowlisted server profiles keyed by language, each naming an exact binary and
  stdio argv; a factory returns `None` when the binary is absent or not explicitly
  enabled (`MAYA_LSP_ENABLE=1`), so module import never spawns a process.
- `LspQuery` results carry provenance `LSP_DEFINITION`/`LSP_REFERENCE`/`LSP_HOVER`
  and `HIGH` confidence, plus the server name/version and the pinned snapshot id.
- CLI `maya inspect lsp-definition/references/hover <file>:<line>:<column>
  [--language ...] [--snapshot ...]` with `--json`; typed `unsupported` when no
  server is configured.
- Tests with a fake stdio LSP server (deterministic newline JSON-RPC): handshake,
  definition/reference/hover round trips, id matching, timeout/crash fail-closed,
  sanitized env (secret canary must not reach child), child killed, and disabled
  factory returning None.

## Explicitly out

- Installing language servers, network transports, auto-indexing every file,
  treating LSP output as an authority for verification or state transitions.

## Design constraints

- Every query path is validated against the pinned snapshot (no absolute host
  paths, symlink escapes or paths outside the snapshot).
- No I/O at module import. No secrets forwarded to the child.
- LSP results are advisory index evidence, never a verification pass.

## Acceptance criteria

1. With no server enabled, the factory returns None and CLI reports `unsupported`.
2. With a fake server, definition/reference/hover return real, provenance-tagged
   results in the pinned snapshot.
3. Path traversal/symlink escapes are refused before spawning.
4. Full suite green; status docs updated.

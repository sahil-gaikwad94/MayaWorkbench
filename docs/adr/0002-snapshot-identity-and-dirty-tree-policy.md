# ADR 0002 — Snapshot identity and the dirty-tree policy

Status: accepted (Phase 1)

## Context

Every index and answer must describe exactly one set of bytes. Two failure modes
must be impossible:

1. Reading a working tree that changed after the snapshot was "pinned", then
   presenting the result as that snapshot.
2. Silently including (or excluding) uncommitted work, so the user cannot tell
   what was actually analysed.

Commits alone are insufficient: users routinely ask about uncommitted work, and
there is no commit object for it.

## Decision

Two snapshot kinds:

- `COMMITTED` — read from Git objects at a resolved commit (`ls-tree` +
  `cat-file --batch`). Later edits cannot affect it. Uncommitted changes are
  excluded, and the exclusion is stated in the CLI and stored as `dirty`.
- `WORKTREE` — explicitly requested with `--include-working-tree`. Captures
  `git diff --binary` plus a manifest of untracked files and pins every
  included file by SHA-256.

Identity:

```
committed:  <commit[:12]>
worktree:   <commit[:12]>+patch:<patch_hash[:12]>
patch_hash = sha256(diff_bytes || 0x00 || canonical_manifest_json)
```

The manifest is `{path: {sha256, size}}` with sorted keys; timestamps are not
part of the identity. Equivalent snapshots are reused rather than duplicated.

Safety rules:

- Capture verifies `HEAD` and `git status --porcelain` are unchanged from start
  to finish and refuses otherwise.
- Indexing re-verifies `HEAD`, status and patch hash for worktree snapshots; a
  moved tree is refused with `SnapshotError`, the snapshot is marked `FAILED`,
  and any previous index is preserved.
- The checkout is never modified: only read-only Git commands are issued.

## Consequences

- Positives: reproducible evidence; an explicit, visible choice about dirty
  content; deduplication makes repeated snapshots cheap.
- Negatives: worktree snapshots are only as stable as the re-verification
  window (TOCTOU remains possible but narrow); committed snapshots of very large
  repositories read through Git rather than the filesystem, which is slower for
  cold caches but correct.
- Rejected alternative: snapshotting the filesystem plus a commit hash. It
  cannot distinguish "the tree that was captured" from "the tree now", which is
  the exact ambiguity this product exists to remove.

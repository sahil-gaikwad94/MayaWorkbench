"""Snapshot content sources.

A committed snapshot is read from Git objects at a resolved commit, so indexing
cannot be affected by later edits in the user's checkout. A worktree snapshot is
read from disk but every file is checked against the size observed during
discovery, and the indexer records its SHA-256; any change mid-index aborts the
run rather than producing a mixed-revision index.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from mayaworkbench.errors import SnapshotError
from mayaworkbench.repositories.discovery import Candidate
from mayaworkbench.repositories.git import GitClient


class SnapshotSource(Protocol):
    def read_many(self, candidates: Sequence[Candidate]) -> dict[str, bytes]:
        """Return ``{path: bytes}`` for the requested candidates."""


class GitTreeSource:
    def __init__(self, git: GitClient, candidates: Sequence[Candidate]):
        self.git = git
        self._sha_by_path = {
            candidate.path: candidate.blob_sha
            for candidate in candidates
            if candidate.blob_sha is not None
        }

    def read_many(self, candidates: Sequence[Candidate]) -> dict[str, bytes]:
        wanted = [candidate for candidate in candidates if candidate.path in self._sha_by_path]
        shas = [self._sha_by_path[candidate.path] for candidate in wanted]
        objects = self.git.cat_file_batch(shas)
        contents: dict[str, bytes] = {}
        for candidate in wanted:
            sha = self._sha_by_path[candidate.path]
            entry = objects.get(sha)
            if entry is None:
                raise SnapshotError(
                    f"Git object {sha} for {candidate.path} could not be read",
                    hint="The repository object store may be corrupted or pruned.",
                )
            object_type, data = entry
            if object_type != "blob":
                raise SnapshotError(
                    f"Expected a blob for {candidate.path}, found {object_type}"
                )
            contents[candidate.path] = data
        return contents


class WorktreeSource:
    def __init__(self, root: Path, candidates: Sequence[Candidate]):
        self.root = root.resolve()
        self._expected = {candidate.path: candidate.size_bytes for candidate in candidates}

    def read_many(self, candidates: Sequence[Candidate]) -> dict[str, bytes]:
        contents: dict[str, bytes] = {}
        for candidate in candidates:
            absolute = (self.root / candidate.path).resolve()
            if not absolute.is_relative_to(self.root):
                raise SnapshotError(
                    f"Path {candidate.path} escapes the repository root",
                    hint="Refusing to read outside the repository.",
                )
            try:
                data = absolute.read_bytes()
            except OSError as exc:
                raise SnapshotError(f"Cannot read {candidate.path}: {exc}") from exc
            expected = self._expected.get(candidate.path)
            if expected is not None and len(data) != expected:
                raise SnapshotError(
                    f"{candidate.path} changed while it was being captured "
                    f"({expected} -> {len(data)} bytes)",
                    hint="Re-create the snapshot from a stable revision.",
                )
            contents[candidate.path] = data
        return contents


__all__ = ["SnapshotSource", "GitTreeSource", "WorktreeSource"]

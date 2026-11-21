"""Materialise a pinned snapshot into an isolated checkout.

Both the task workspace (:mod:`mayaworkbench.workspace.worktree`) and the
experiment runner need the same guarantee: the directory a command runs in
contains exactly the bytes the snapshot recorded, including a captured
working-tree patch and reconstructed untracked files.

Duplicating that logic would let the two paths drift, so it lives here. The
functions are deliberately strict: a file that does not match its recorded
SHA-256 aborts materialisation instead of being silently used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mayaworkbench.domain.enums import SnapshotKind
from mayaworkbench.domain.models import RepositorySnapshot
from mayaworkbench.errors import WorkspaceError
from mayaworkbench.repositories.git import GitClient
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import FileStore
from mayaworkbench.workspace.policy import normalize_rel


def apply_snapshot_state(
    git: GitClient,
    worktree_git: GitClient,
    snapshot: RepositorySnapshot,
    *,
    artifacts: ArtifactStore,
    files: FileStore,
) -> None:
    """Apply the snapshot's captured patch and untracked files to a worktree.

    The patch bytes are verified against the snapshot's recorded ``patch_hash``
    (which covers both the diff and the untracked manifest) before ``git apply``.
    Untracked files are written from content-addressed artifacts after checking
    their hashes. Nothing is fetched from the user's live working tree.
    """
    del git  # kept for symmetry with the caller's repository client
    if snapshot.patch_artifact_id:
        patch = artifacts.read_bytes(snapshot.patch_artifact_id)
        if snapshot.patch_hash:
            if not snapshot.untracked_manifest_artifact_id:
                raise WorkspaceError(
                    "The snapshot records a patch hash but no untracked manifest"
                )
            manifest_bytes = artifacts.read_bytes(
                snapshot.untracked_manifest_artifact_id
            )
            expected = hashlib.sha256(patch + b"\x00" + manifest_bytes).hexdigest()
            if expected != snapshot.patch_hash:
                raise WorkspaceError(
                    "The stored snapshot patch does not match its recorded hash"
                )
        if patch.strip():
            result = worktree_git.run(
                "apply",
                "--binary",
                "--whitespace=nowarn",
                input_bytes=patch,
                check=False,
            )
            if result.returncode != 0:
                raise WorkspaceError(
                    "Could not apply the snapshot's working-tree patch in the workspace",
                    hint=result.stderr[:800],
                )
    if snapshot.untracked_manifest_artifact_id:
        manifest = json.loads(
            artifacts.read_text(snapshot.untracked_manifest_artifact_id)
        )
        workdir = Path(worktree_git.workdir).resolve()
        for raw_path, entry in sorted((manifest.get("files") or {}).items()):
            rel = normalize_rel(raw_path)
            expected_sha = entry.get("sha256")
            if not expected_sha:
                raise WorkspaceError(
                    f"Untracked snapshot file cannot be reconstructed: {rel}",
                    hint=entry.get("note") or "The file was not stored with a hash.",
                )
            indexed = files.get_by_path(snapshot.id, rel)
            if indexed is None or indexed.content_artifact_id is None:
                raise WorkspaceError(
                    f"Untracked snapshot file was not indexed and cannot be reconstructed: {rel}"
                )
            data = artifacts.read_bytes(indexed.content_artifact_id)
            if hashlib.sha256(data).hexdigest() != expected_sha:
                raise WorkspaceError(
                    f"Stored content for untracked snapshot file does not match: {rel}"
                )
            target = (workdir / rel).resolve()
            if not target.is_relative_to(workdir):
                raise WorkspaceError(f"Untracked path escapes the workspace: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def verify_snapshot_content(
    snapshot: RepositorySnapshot,
    path: Path,
    *,
    files: FileStore,
) -> int:
    """Check every indexed snapshot file against its recorded content hash.

    Returns the number of verified files. ``SKIPPED`` files were never indexed,
    so they cannot be verified here and are not counted.
    """
    verified = 0
    for indexed in files.list_by_snapshot(snapshot.id, limit=100_000):
        if indexed.status.value == "SKIPPED":
            continue
        target = path / indexed.path
        if not target.exists() or not target.is_file():
            raise WorkspaceError(
                f"Workspace is missing indexed snapshot file: {indexed.path}"
            )
        if target.is_symlink():
            raise WorkspaceError(
                f"Workspace file is a symlink, which the snapshot cannot represent: "
                f"{indexed.path}"
            )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != indexed.content_sha256:
            raise WorkspaceError(
                f"Workspace file does not match the pinned snapshot: {indexed.path}",
                hint="The snapshot is inconsistent or the repository was modified.",
            )
        verified += 1
    return verified


def ignored_paths(path: Path) -> set[str]:
    """Git-ignored paths present in ``path``, normalised for comparison."""
    result = GitClient(path).run(
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        check=False,
    )
    raw = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    return {normalize_rel(item) for item in raw.split("\x00") if item.strip()}


def needs_patch_application(snapshot: RepositorySnapshot) -> bool:
    return snapshot.kind == SnapshotKind.WORKTREE or snapshot.includes_worktree_patch


__all__ = [
    "apply_snapshot_state",
    "verify_snapshot_content",
    "ignored_paths",
    "needs_patch_application",
]

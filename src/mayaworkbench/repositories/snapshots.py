"""Repository attachment and snapshot pinning.

Snapshot semantics (§8.1):

* The full resolved commit SHA is always recorded.
* ``dirty`` records whether the working tree had uncommitted changes *at capture
  time*, even when those changes are excluded.
* Uncommitted changes are never silently mixed in. A user must ask for
  ``WORKTREE`` capture, which records a patch artifact, an untracked-file
  manifest, the affected paths, and a combined hash.
* The user's checkout is only ever read. HEAD and status are re-checked after
  capture to detect concurrent modification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
    SnapshotKind,
    SnapshotStatus,
)
from mayaworkbench.domain.models import (
    Project,
    Repository,
    RepositorySnapshot,
    WorkflowEvent,
)
from mayaworkbench.errors import RepositoryError, SnapshotError
from mayaworkbench.ids import new_ulid
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.repositories.discovery import (
    DiscoveryPolicy,
    DiscoveryResult,
    discover_tree,
    discover_worktree,
)
from mayaworkbench.repositories.git import GitClient, redact_remote_url
from mayaworkbench.repositories.source import GitTreeSource, SnapshotSource, WorktreeSource
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    EventStore,
    ProjectStore,
    RepositoryStore,
    SnapshotStore,
)
from mayaworkbench.version import INDEXER_VERSION

log = get_logger("repositories.snapshots")

WorkingTreePolicy = Literal["committed", "include"]


@dataclass
class AttachResult:
    repository: Repository
    created: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class SnapshotOutcome:
    snapshot: RepositorySnapshot
    repository: Repository
    reused: bool
    warnings: list[str] = field(default_factory=list)
    discovery: DiscoveryResult | None = None


class RepositoryService:
    def __init__(
        self,
        db: Database,
        projects: ProjectStore,
        repositories: RepositoryStore,
        snapshots: SnapshotStore,
        artifacts: ArtifactStore,
        settings: Settings,
        events: EventStore,
    ):
        self.db = db
        self.projects = projects
        self.repositories = repositories
        self.snapshots = snapshots
        self.artifacts = artifacts
        self.settings = settings
        self.events = events
        self.policy = DiscoveryPolicy.from_settings(settings.indexing)

    # ------------------------------------------------------------------ attach
    def attach(self, project: Project, path: str | Path, name: str | None = None) -> AttachResult:
        requested = Path(path).expanduser()
        if not requested.exists():
            raise RepositoryError(
                f"Path does not exist: {requested}",
                hint="Attach an existing local Git repository.",
            )
        if not requested.is_dir():
            raise RepositoryError(f"Path is not a directory: {requested}")

        git = GitClient(requested)
        root = git.resolve_root()
        warnings: list[str] = []
        if root != requested.resolve():
            warnings.append(
                f"Attached the repository root {root} instead of the nested path {requested}."
            )

        existing = self.repositories.find_by_path(project.id, str(root))
        if existing is not None:
            return AttachResult(existing, created=False, warnings=warnings)

        head = git.head_commit()
        remote_url, remote_had_credentials = redact_remote_url(git.remote_url())
        if remote_had_credentials:
            warnings.append(
                "The configured remote URL embedded credentials; they were removed "
                "before the URL was stored. Rotate the token if this workbench is shared."
            )
        if head is None:
            warnings.append(
                "The repository has no commits yet. Create a commit before pinning snapshots."
            )

        repository = Repository(
            id="repo_" + new_ulid(),
            project_id=project.id,
            name=name or root.name or "repository",
            root_path=str(root),
            vcs="git",
            remote_url=remote_url,
            default_branch=git.default_branch(),
            created_at=utcnow(),
            updated_at=utcnow(),
            details={
                "attached_path": str(requested.resolve()),
                "git_version": git.version(),
                "head_at_attach": head,
                "remote_url_credentials_removed": remote_had_credentials,
            },
        )
        with self.db.transaction():
            self.repositories.add(repository)
            self.projects.touch(project.id)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.REPOSITORY_ATTACHED,
                    project_id=project.id,
                    repository_id=repository.id,
                    entity_type="repository",
                    entity_id=repository.id,
                    actor_kind=ActorKind.USER,
                    actor_name="local-user",
                    payload={
                        "root_path": str(root),
                        "remote_url": repository.remote_url,
                        "head": head,
                    },
                )
            )
        return AttachResult(repository, created=True, warnings=warnings)

    # ---------------------------------------------------------------- snapshot
    def create_snapshot(
        self,
        repository: Repository,
        *,
        working_tree: WorkingTreePolicy = "committed",
    ) -> SnapshotOutcome:
        root = Path(repository.root_path)
        if not root.exists():
            raise RepositoryError(
                f"Repository path no longer exists: {root}",
                hint="Re-attach the repository if it moved.",
            )
        git = GitClient(root)
        if not git.is_inside_work_tree():
            raise RepositoryError(f"{root} is no longer a Git working tree")

        warnings: list[str] = []
        head_before = git.head_commit()
        if head_before is None:
            raise SnapshotError(
                "Cannot pin a snapshot: the repository has no commits",
                hint="Commit your work, then create the snapshot again.",
            )
        status_before = git.status_porcelain()
        dirty = bool(status_before.strip())
        branch = git.current_branch()
        tree_sha = git.tree_sha(head_before)
        meta = git.commit_metadata(head_before)
        if branch == "HEAD":
            warnings.append("HEAD is detached; the snapshot is pinned to the commit, not a branch.")

        kind = SnapshotKind.COMMITTED if working_tree == "committed" else SnapshotKind.WORKTREE
        if kind == SnapshotKind.WORKTREE and not dirty:
            # A clean worktree is byte-identical to the commit. Pinning it as a
            # worktree snapshot would claim disk content without any evidence
            # binding it to the commit, so collapse it to a committed snapshot.
            kind = SnapshotKind.COMMITTED
            warnings.append(
                "The working tree is clean, so this is a committed snapshot of "
                f"{head_before[:12]} (no patch was captured)."
            )
        patch_hash: str | None = None
        patch_artifact_id: str | None = None
        untracked_artifact_id: str | None = None
        included_paths: list[str] = []
        included_patch = False

        if dirty and working_tree == "committed":
            changed = len([line for line in status_before.splitlines() if line.strip()])
            warnings.append(
                f"The working tree has {changed} uncommitted change(s). They are NOT part of this "
                "snapshot. Use --include-working-tree to capture them as a separate patch snapshot."
            )

        if kind == SnapshotKind.WORKTREE:
            discovery = discover_worktree(git, root, self.policy)
            if dirty:
                patch_bytes, manifest_bytes, manifest = self._capture_patch(git, head_before)
                patch_artifact = self.artifacts.put_bytes(
                    patch_bytes,
                    kind=ArtifactKind.SNAPSHOT_PATCH,
                    media_type="text/x-diff; charset=utf-8",
                    created_by="snapshot",
                )
                untracked_artifact = self.artifacts.put_bytes(
                    manifest_bytes,
                    kind=ArtifactKind.UNTRACKED_MANIFEST,
                    media_type="application/json",
                    created_by="snapshot",
                )
                patch_hash = hashlib.sha256(
                    patch_bytes + b"\x00" + manifest_bytes
                ).hexdigest()
                patch_artifact_id = patch_artifact.id
                untracked_artifact_id = untracked_artifact.id
                included_patch = True
                included_paths = sorted(
                    set(git.diff_name_only(head_before)) | set(manifest.keys())
                )
                warnings.append(
                    "Uncommitted changes are included. The snapshot identity is "
                    f"commit {head_before[:12]} + patch {patch_hash[:12]}."
                )
        else:
            discovery = discover_tree(git, head_before, self.policy)

        source: SnapshotSource
        if kind == SnapshotKind.WORKTREE:
            source = WorktreeSource(root, discovery.candidates)
        else:
            source = GitTreeSource(git, discovery.candidates)

        snapshot = RepositorySnapshot(
            id="snap_" + new_ulid(),
            repository_id=repository.id,
            kind=kind,
            status=SnapshotStatus.CREATED,
            commit_sha=head_before,
            tree_sha=tree_sha,
            branch=branch,
            base_commit_sha=head_before,
            dirty=dirty,
            includes_worktree_patch=included_patch,
            patch_hash=patch_hash,
            patch_artifact_id=patch_artifact_id,
            untracked_manifest_artifact_id=untracked_artifact_id,
            included_paths=included_paths,
            submodules=git.submodules(head_before),
            indexer_version=INDEXER_VERSION,
            created_at=utcnow(),
            index_metadata={
                "commit_subject": meta.get("subject", ""),
                "committed_at": meta.get("committed_at", ""),
                "candidate_files": len(discovery.candidates),
                "candidate_bytes": discovery.total_bytes,
                "skipped_files": len(discovery.skipped),
                "budget_truncated": discovery.truncated_by_budget,
                "policy_hash": self.policy_hash(),
                "repository_root": str(root),
            },
        )

        # Detect concurrent modification of the checkout during capture.
        head_after = git.head_commit()
        status_after = git.status_porcelain()
        if head_after != head_before:
            raise SnapshotError(
                "HEAD moved while the snapshot was being created",
                hint="Re-run the snapshot on a stable revision.",
            )
        if kind == SnapshotKind.COMMITTED and status_after != status_before:
            warnings.append(
                "The working tree changed during capture. The snapshot still reads only the "
                "committed tree, so its content is unaffected."
            )
        if kind == SnapshotKind.WORKTREE and status_after != status_before:
            raise SnapshotError(
                "The working tree changed while the snapshot was being created",
                hint="Stop other processes writing to the repository, then retry.",
            )

        equivalent = self._find_equivalent(repository.id, kind, head_before, patch_hash)
        if equivalent is not None:
            log.info("reusing equivalent snapshot", extra={"snapshot_id": equivalent.id})
            return SnapshotOutcome(
                snapshot=equivalent,
                repository=repository,
                reused=True,
                warnings=warnings,
                discovery=discovery,
            )

        with self.db.transaction():
            self.snapshots.create(snapshot)
            self.repositories.touch(repository.id)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.SNAPSHOT_PINNED,
                    project_id=repository.project_id,
                    repository_id=repository.id,
                    snapshot_id=snapshot.id,
                    entity_type="snapshot",
                    entity_id=snapshot.id,
                    actor_kind=ActorKind.USER,
                    actor_name="local-user",
                    payload={
                        "kind": snapshot.kind.value,
                        "commit_sha": snapshot.commit_sha,
                        "tree_sha": snapshot.tree_sha,
                        "branch": snapshot.branch,
                        "dirty": snapshot.dirty,
                        "includes_worktree_patch": snapshot.includes_worktree_patch,
                        "patch_hash": snapshot.patch_hash,
                        "included_paths": snapshot.included_paths[:200],
                        "candidate_files": len(discovery.candidates),
                        "skipped_files": len(discovery.skipped),
                    },
                )
            )
        return SnapshotOutcome(
            snapshot=snapshot,
            repository=repository,
            reused=False,
            warnings=warnings,
            discovery=discovery,
        )

    # ------------------------------------------------------------------ helpers
    def discover(
        self, snapshot: RepositorySnapshot, repository: Repository
    ) -> tuple[DiscoveryResult, SnapshotSource]:
        """Re-discover the snapshot's file set and build its content source.

        Worktree snapshots are re-discovered from disk; the indexer additionally
        verifies each file's size and records its SHA-256, so any drift between
        capture and indexing aborts the run.
        """
        root = Path(repository.root_path)
        git = GitClient(root)
        if snapshot.kind == SnapshotKind.COMMITTED:
            if not snapshot.commit_sha:
                raise SnapshotError("Committed snapshot is missing its commit SHA")
            discovery = discover_tree(git, snapshot.commit_sha, self.policy)
            return discovery, GitTreeSource(git, discovery.candidates)

        head = git.head_commit()
        if head != snapshot.commit_sha:
            raise SnapshotError(
                "HEAD changed since this worktree snapshot was pinned "
                f"({(snapshot.commit_sha or 'unknown')[:12]} -> {(head or 'none')[:12]})",
                hint="Create a new snapshot for the current revision.",
            )
        if snapshot.includes_worktree_patch:
            patch_bytes, manifest_bytes, _manifest = self._capture_patch(git, head or "HEAD")
            current_hash = hashlib.sha256(patch_bytes + b"\x00" + manifest_bytes).hexdigest()
            if current_hash != snapshot.patch_hash:
                raise SnapshotError(
                    "The working tree changed since this snapshot was pinned",
                    hint=(
                        "Create a new snapshot so the index and the recorded patch refer to "
                        "the same content."
                    ),
                )
        discovery = discover_worktree(git, root, self.policy)
        return discovery, WorktreeSource(root, discovery.candidates)

    def policy_hash(self) -> str:
        payload = json.dumps(
            {
                "max_file_bytes": self.policy.max_file_bytes,
                "max_total_bytes": self.policy.max_total_bytes,
                "max_files": self.policy.max_files,
                "excluded_dirs": sorted(self.policy.excluded_dirs),
                "excluded_globs": sorted(self.policy.excluded_globs),
                "extra_included_globs": sorted(self.policy.extra_included_globs),
                "secret_filename_patterns": sorted(self.policy.secret_filename_patterns),
                "scan_content_for_secrets": self.policy.scan_content_for_secrets,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _capture_patch(
        self, git: GitClient, commit: str
    ) -> tuple[bytes, bytes, dict[str, dict]]:
        """Capture the diff plus a hashed manifest of untracked files."""
        patch_bytes = git.diff_binary(commit)
        manifest: dict[str, dict] = {}
        root = Path(git.workdir)
        limit = self.policy.max_file_bytes
        for path in git.untracked_files():
            absolute = (root / path).resolve()
            if not absolute.is_relative_to(root.resolve()) or not absolute.is_file():
                manifest[path] = {"size_bytes": 0, "sha256": None, "note": "not a regular file"}
                continue
            size = absolute.stat().st_size
            if size > limit:
                manifest[path] = {
                    "size_bytes": size,
                    "sha256": None,
                    "note": f"not hashed: exceeds max_file_bytes={limit}",
                }
                continue
            try:
                data = absolute.read_bytes()
            except OSError as exc:
                manifest[path] = {"size_bytes": size, "sha256": None, "note": f"unreadable: {exc}"}
                continue
            manifest[path] = {
                "size_bytes": size,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        manifest_bytes = json.dumps(
            {"version": 1, "files": manifest}, indent=2, sort_keys=True
        ).encode("utf-8")
        return patch_bytes, manifest_bytes, manifest

    def _find_equivalent(
        self,
        repository_id: str,
        kind: SnapshotKind,
        commit_sha: str,
        patch_hash: str | None,
    ) -> RepositorySnapshot | None:
        for candidate in self.snapshots.list_by_repository(repository_id, limit=25):
            if candidate.status == SnapshotStatus.FAILED:
                continue
            if (
                candidate.kind == kind
                and candidate.commit_sha == commit_sha
                and candidate.patch_hash == patch_hash
            ):
                return candidate
        return None


__all__ = [
    "RepositoryService",
    "SnapshotOutcome",
    "AttachResult",
    "WorkingTreePolicy",
]

"""Isolated Git worktrees for one workflow run.

The user's checkout is never the write target. Each run gets a unique
detached worktree under the Maya home directory. Creation verifies that the
worktree content matches the pinned snapshot (commit plus, where present,
working-tree patch and reconstructed untracked files), and records that the
original checkout's HEAD and status did not change.

A worktree is not a security boundary. It prevents accidental writes to the
user's checkout and makes cleanup possible; command execution limitations are
enforced separately by :mod:`mayaworkbench.workspace.runner`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
    SnapshotKind,
    WorkspaceStatus,
)
from mayaworkbench.domain.models import (
    DiffFile,
    ExecutionWorkspace,
    Repository,
    RepositorySnapshot,
    Task,
    TaskContract,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import NotFoundError, WorkspaceError
from mayaworkbench.ids import new_ulid
from mayaworkbench.observability.logging import get_logger
from mayaworkbench.repositories.git import GitClient
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    EventStore,
    FileStore,
    RepositoryStore,
    SnapshotStore,
    WorkspaceStore,
)
from mayaworkbench.workspace.policy import normalize_rel

log = get_logger("workspace.worktree")


class WorktreeManager:
    def __init__(
        self,
        *,
        home: Path,
        db: Database,
        repositories: RepositoryStore,
        snapshots: SnapshotStore,
        files: FileStore,
        artifacts: ArtifactStore,
        workspaces: WorkspaceStore,
        events: EventStore,
    ):
        self.home = Path(home).resolve()
        self.db = db
        self.repositories = repositories
        self.snapshots = snapshots
        self.files = files
        self.artifacts = artifacts
        self.workspaces = workspaces
        self.events = events
        self.workspace_root = self.home / "workspaces"
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --------------------------------------------------------------- lifecycle
    def create(
        self,
        *,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        repository: Repository,
        snapshot: RepositorySnapshot,
    ) -> ExecutionWorkspace:
        if contract.scope.base_commit_sha != snapshot.commit_sha:
            raise WorkspaceError(
                "The contract base commit does not match the pinned snapshot commit",
                hint="Create a new contract from the snapshot, or pin a new snapshot.",
            )
        if snapshot.commit_sha is None:
            raise WorkspaceError("The snapshot has no resolved commit SHA")
        existing = self.workspaces.get_by_run(run.id)
        if existing is not None and existing.status in {
            WorkspaceStatus.CREATING,
            WorkspaceStatus.READY,
            WorkspaceStatus.FINALIZED,
        }:
            return existing

        safe_run = normalize_rel(run.id)
        path = (self.workspace_root / safe_run / "repo").resolve()
        if not path.is_relative_to(self.workspace_root):
            raise WorkspaceError("Workspace path escaped the Maya home directory")
        if path.exists():
            raise WorkspaceError(
                f"Workspace path already exists and is not owned by this run: {path}",
                hint="Remove the stale directory only after confirming no other run uses it.",
            )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        repo_root = Path(repository.root_path).resolve()
        git = GitClient(repo_root)
        if not git.is_inside_work_tree():
            raise WorkspaceError(f"Repository is no longer a Git worktree: {repo_root}")
        head_before = git.head_commit()
        status_before = git.status_porcelain()

        workspace = ExecutionWorkspace(
            id="ws_" + new_ulid(),
            run_id=run.id,
            task_id=task.id,
            project_id=run.project_id,
            path=str(path),
            base_commit_sha=snapshot.commit_sha,
            status=WorkspaceStatus.CREATING,
            details={
                "repository_root": str(repo_root),
                "snapshot_kind": snapshot.kind.value,
                "original_head_before": head_before,
                "original_status_before": status_before,
            },
        )
        self.workspaces.create(workspace)
        created_worktree = False
        try:
            git.run("worktree", "add", "--detach", str(path), snapshot.commit_sha)
            created_worktree = True
            worktree_git = GitClient(path)
            actual_head = worktree_git.head_commit()
            if actual_head != snapshot.commit_sha:
                raise WorkspaceError(
                    f"Worktree HEAD is {actual_head}, expected {snapshot.commit_sha}"
                )
            if snapshot.kind == SnapshotKind.WORKTREE or snapshot.includes_worktree_patch:
                self._apply_snapshot_state(git, worktree_git, snapshot)
            verified = self._verify_snapshot_content(snapshot, path)
            # Baseline ignored paths are needed so a task-created ignored file
            # cannot disappear from the candidate patch (MWB-007).
            ignored_baseline = self._ignored_paths(path)

            head_after = git.head_commit()
            status_after = git.status_porcelain()
            if head_after != head_before or status_after != status_before:
                raise WorkspaceError(
                    "The original checkout changed while the worktree was created; "
                    "refusing to continue",
                    hint="Stop other processes writing to the repository, then retry.",
                )
            self.workspaces.update(
                workspace.id,
                status=WorkspaceStatus.READY,
                details={
                    **workspace.details,
                    "original_head_after": head_after,
                    "original_status_after": status_after,
                    "original_checkout_unchanged": True,
                    "verified_files": verified,
                    "ignored_baseline": sorted(ignored_baseline),
                },
            )
            self._event(
                EventType.WORKTREE_CREATED,
                run=run,
                payload={
                    "workspace_id": workspace.id,
                    "path": str(path),
                    "base_commit_sha": snapshot.commit_sha,
                    "snapshot_kind": snapshot.kind.value,
                    "verified_files": verified,
                    "original_checkout_unchanged": True,
                },
            )
            return self.workspaces.get(workspace.id)
        except Exception as exc:
            self.workspaces.update(
                workspace.id,
                status=WorkspaceStatus.FAILED,
                details={**workspace.details, "error": str(exc)[:2000]},
            )
            if created_worktree:
                self._remove_worktree(git, path, strict=False)
            shutil.rmtree(path.parent, ignore_errors=True)
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError(
                f"Could not create the isolated workspace: {exc}",
                hint="Inspect the repository and workspace directory, then retry.",
            ) from exc

    def _apply_snapshot_state(
        self, git: GitClient, worktree_git: GitClient, snapshot: RepositorySnapshot
    ) -> None:
        if snapshot.patch_artifact_id:
            patch = self.artifacts.read_bytes(snapshot.patch_artifact_id)
            if snapshot.patch_hash:
                if not snapshot.untracked_manifest_artifact_id:
                    raise WorkspaceError(
                        "The snapshot records a patch hash but no untracked manifest"
                    )
                manifest_bytes = self.artifacts.read_bytes(
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
                self.artifacts.read_text(snapshot.untracked_manifest_artifact_id)
            )
            for raw_path, entry in sorted((manifest.get("files") or {}).items()):
                rel = normalize_rel(raw_path)
                expected_sha = entry.get("sha256")
                if not expected_sha:
                    raise WorkspaceError(
                        f"Untracked snapshot file cannot be reconstructed: {rel}",
                        hint=entry.get("note") or "The file was not stored with a hash.",
                    )
                indexed = self.files.get_by_path(snapshot.id, rel)
                if indexed is None or indexed.content_artifact_id is None:
                    raise WorkspaceError(
                        f"Untracked snapshot file was not indexed and cannot be reconstructed: {rel}"
                    )
                data = self.artifacts.read_bytes(indexed.content_artifact_id)
                if hashlib.sha256(data).hexdigest() != expected_sha:
                    raise WorkspaceError(
                        f"Stored content for untracked snapshot file does not match: {rel}"
                    )
                target = (Path(worktree_git.workdir) / rel).resolve()
                if not target.is_relative_to(Path(worktree_git.workdir).resolve()):
                    raise WorkspaceError(f"Untracked path escapes the workspace: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    def _verify_snapshot_content(self, snapshot: RepositorySnapshot, path: Path) -> int:
        files = self.files.list_by_snapshot(snapshot.id, limit=100_000)
        verified = 0
        for indexed in files:
            if indexed.status.value == "SKIPPED":
                continue
            target = path / indexed.path
            if not target.exists() or not target.is_file():
                raise WorkspaceError(
                    f"Workspace is missing indexed snapshot file: {indexed.path}"
                )
            if target.is_symlink():
                raise WorkspaceError(
                    f"Workspace file is a symlink, which the snapshot cannot represent: {indexed.path}"
                )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != indexed.content_sha256:
                raise WorkspaceError(
                    f"Workspace file does not match the pinned snapshot: {indexed.path}",
                    hint="The snapshot is inconsistent or the repository was modified.",
                )
            verified += 1
        return verified

    def _ignored_paths(self, path: Path) -> set[str]:
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

    # -------------------------------------------------------------------- diff
    def capture_diff(self, workspace: ExecutionWorkspace) -> tuple[bytes, list[DiffFile], list[str]]:
        path = self._owned_path(workspace)
        git = GitClient(path)
        warnings: list[str] = []
        git.run("add", "-A")
        # Git silently ignores new ignored files. A task can still create one
        # (a generated module, a build artifact) and it must be visible in the
        # patch and review instead of vanishing from both.
        baseline = set(workspace.details.get("ignored_baseline") or [])
        current_ignored = self._ignored_paths(path)
        for rel in sorted(current_ignored - baseline):
            added = git.run("add", "-f", "--", rel, check=False)
            if added.returncode != 0:
                warnings.append(
                    f"A new ignored path could not be represented in the patch: {rel} "
                    f"({added.stderr[:200]})"
                )
            else:
                warnings.append(
                    f"A new ignored path was created during the run and force-added to the "
                    f"candidate patch: {rel}"
                )
        for rel in sorted(baseline - current_ignored):
            warnings.append(
                "A previously ignored path was removed during the run; a deletion of an "
                f"ignored path cannot be represented in the patch: {rel}"
            )
        patch = git.run(
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            workspace.base_commit_sha,
        ).stdout
        numstat = git.run(
            "diff",
            "--cached",
            "--numstat",
            "--no-ext-diff",
            "--no-textconv",
            workspace.base_commit_sha,
        ).stdout_text
        name_status = git.run(
            "diff",
            "--cached",
            "--name-status",
            "--no-ext-diff",
            "--no-textconv",
            workspace.base_commit_sha,
        ).stdout_text
        files: list[DiffFile] = []
        stats: dict[str, tuple[int, int, bool]] = {}
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, deleted, file_path = parts[0], parts[1], parts[2]
            binary = added == "-" or deleted == "-"
            stats[file_path] = (
                0 if binary else int(added),
                0 if binary else int(deleted),
                binary,
            )
        status_map: dict[str, str] = {}
        for line in name_status.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                code = parts[0][:1]
                status_map[parts[-1]] = {
                    "A": "added",
                    "M": "modified",
                    "D": "deleted",
                    "T": "type_changed",
                }.get(code, "modified")
        for file_path, (added, deleted, binary) in sorted(stats.items()):
            files.append(
                DiffFile(
                    path=file_path,
                    status=status_map.get(file_path, "modified"),
                    additions=added,
                    deletions=deleted,
                    binary=binary,
                )
            )
            if binary:
                warnings.append(f"Binary change is not reviewable as text: {file_path}")
        status = git.run("status", "--porcelain", "--untracked-files=all").stdout_text
        unrepresented = [
            line
            for line in status.splitlines()
            if not line.startswith("!!")
            and (line.startswith("??") or (len(line) > 1 and line[1] != " "))
        ]
        if unrepresented:
            warnings.append(
                "The workspace still has changes not represented in the staged patch; "
                "inspect `git status` inside the workspace."
            )
        if len(patch) > 10_000_000:
            warnings.append("Patch exceeds 10 MB; review it in the workspace before use.")
        return patch, files, warnings

    def verify_original_checkout(
        self, workspace: ExecutionWorkspace, repository: Repository
    ) -> tuple[bool, str | None, str]:
        """Re-read the user's checkout and compare it with the creation record.

        A command can still write outside the worktree because a worktree is not
        an OS sandbox. This check detects that after the fact instead of
        pretending the boundary was airtight.
        """
        git = GitClient(Path(repository.root_path))
        head = git.head_commit()
        status = git.status_porcelain()
        before_head = workspace.details.get("original_head_before")
        before_status = workspace.details.get("original_status_before", "")
        return head == before_head and status == before_status, head, status

    # ----------------------------------------------------------------- cleanup
    def remove(self, workspace: ExecutionWorkspace) -> ExecutionWorkspace:
        path = self._owned_path(workspace)
        repository = self._repository_for_workspace(workspace)
        git = GitClient(repository.root_path)
        self._remove_worktree(git, path, strict=True)
        shutil.rmtree(path.parent, ignore_errors=True)
        self.workspaces.update(
            workspace.id, status=WorkspaceStatus.REMOVED, removed=True
        )
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.WORKTREE_REMOVED,
                project_id=workspace.project_id,
                task_id=workspace.task_id,
                run_id=workspace.run_id,
                entity_type="workspace",
                entity_id=workspace.id,
                actor_kind=ActorKind.USER,
                actor_name="local-user",
                payload={"path": workspace.path},
            )
        )
        return self.workspaces.get(workspace.id)

    def _remove_worktree(self, git: GitClient, path: Path, *, strict: bool) -> None:
        if not path.exists():
            if strict:
                raise WorkspaceError(f"Workspace path is already gone: {path}")
            return
        result = git.run("worktree", "remove", "--force", str(path), check=False)
        if result.returncode != 0:
            git.run("worktree", "prune", check=False)
            result = git.run("worktree", "remove", "--force", str(path), check=False)
        if result.returncode != 0:
            if strict:
                raise WorkspaceError(
                    f"Could not remove the isolated worktree: {result.stderr[:500]}"
                )
            shutil.rmtree(path, ignore_errors=True)

    def _owned_path(self, workspace: ExecutionWorkspace) -> Path:
        path = Path(workspace.path).resolve()
        if not path.is_relative_to(self.workspace_root):
            raise WorkspaceError("Workspace path is outside the Maya workspace root")
        if path.parent != self.workspace_root / workspace.run_id:
            raise WorkspaceError("Workspace path does not belong to this run")
        return path

    def _repository_for_workspace(self, workspace: ExecutionWorkspace) -> Repository:
        run = self.db.fetchone(
            "SELECT repository_id FROM workflow_runs WHERE id = ?", (workspace.run_id,)
        )
        if run is None:
            raise NotFoundError(f"Run not found: {workspace.run_id}")
        return self.repositories.get(str(run["repository_id"]))

    def _event(self, event_type: EventType, *, run: WorkflowRun, payload: dict[str, Any]) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=run.project_id,
                task_id=run.task_id,
                run_id=run.id,
                snapshot_id=run.snapshot_id,
                entity_type="workspace",
                entity_id=run.workspace_id or run.id,
                actor_kind=ActorKind.TOOL,
                actor_name="workspace",
                payload=payload,
            )
        )


__all__ = ["WorktreeManager"]

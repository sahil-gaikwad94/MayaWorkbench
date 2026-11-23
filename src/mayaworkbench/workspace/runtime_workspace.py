"""One task, one workspace: the single file view used by tools and commands.

The control plane must not have two competing notions of the task workspace.
For ``LOCAL_TRUSTED`` the active view is the host worktree (the historical
behaviour). For an isolated backend it is a populated host-side overlay that is
mounted writable at the container's ``/work``, so a file written by a tool is
the same file a command reads, and a file changed by a command is in the same
tree the artifact/diff collector sees.

The overlay is *never* populated from, and never shares a mount with, the
user's worktree: it is derived from an immutable sanitized snapshot
(:mod:`mayaworkbench.workspace.sanitize`) that the container cannot modify.
The source manifest recorded here is the manifest of that sanitized snapshot,
so "the tree the tools edited" and "the tree the evidence describes" are the
same tree by construction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mayaworkbench.workspace import sanitize
from mayaworkbench.workspace.policy import contains_secret_material
from mayaworkbench.workspace.sanitize import (
    EXCLUDED_DIRS,
    MAX_OVERLAY_BYTES,
    MAX_OVERLAY_FILE_BYTES,
    MAX_OVERLAY_FILES,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mayaworkbench.workspace.backends import ExecutionBackend, RuntimeRef

__all__ = [
    "EXCLUDED_DIRS",
    "MAX_OVERLAY_BYTES",
    "MAX_OVERLAY_FILES",
    "MAX_OVERLAY_FILE_BYTES",
    "RuntimeWorkspace",
    "content_diff",
    "import_diff_to_worktree",
    "manifest",
    "populate_overlay",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest(path: Path) -> dict[str, str]:
    """Content manifest of a task-visible tree (relative path -> sha256).

    Delegates to the canonical sanitizer so the original snapshot, the
    sanitized reference and the writable overlay are directly comparable.
    """

    return sanitize.tree_manifest(path)


def populate_overlay(
    source: Path,
    overlay: Path,
    *,
    max_files: int = MAX_OVERLAY_FILES,
    max_bytes: int = MAX_OVERLAY_BYTES,
) -> dict[str, str]:
    """Copy a clean, secret-free snapshot into a writable task overlay.

    Returns the manifest of copied files. Raising here blocks the task before
    the first command: an incomplete overlay must never be presented as the
    exact tree that will be verified.
    """

    tree = _bounded_copy(Path(source), Path(overlay), max_files=max_files, max_bytes=max_bytes)
    return tree.manifest


def _bounded_copy(
    source: Path, overlay: Path, *, max_files: int, max_bytes: int
) -> sanitize.SanitizedTree:
    overlay.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied: dict[str, str] = {}
    total = 0
    for item in sanitize.iter_source_files(
        source, max_files=max_files, max_bytes=max_bytes, max_file_bytes=MAX_OVERLAY_FILE_BYTES
    ):
        target = overlay / item.path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(item.data)
        copied[item.path] = _sha256(item.data)
        total += item.size
    return sanitize.SanitizedTree(
        root=overlay,
        manifest=copied,
        policy_version=sanitize.POLICY_VERSION,
        policy_sha256=sanitize.policy_sha256(),
        files=len(copied),
        bytes=total,
    )


def content_diff(base: dict[str, str], candidate: dict[str, str]) -> list[dict[str, str]]:
    """Content-aware diff between two manifests, independent of Git metadata."""

    changes: list[dict[str, str]] = []
    for rel in sorted(set(base) | set(candidate)):
        before, after = base.get(rel), candidate.get(rel)
        if before == after:
            continue
        if before is None:
            status = "added"
        elif after is None:
            status = "deleted"
        else:
            status = "modified"
        changes.append(
            {
                "path": rel,
                "status": status,
                "base_sha256": before or "",
                "candidate_sha256": after or "",
            }
        )
    return changes


@dataclass(frozen=True)
class RuntimeWorkspace:
    """The resolved file view for one task run."""

    kind: str  # "host" or "container"
    base_path: Path  # immutable pinned source (the user's worktree)
    path: Path  # the active view every file tool and command uses
    source_manifest: dict[str, str]
    runtime: "RuntimeRef | None" = None
    backend: "ExecutionBackend | None" = None
    #: Immutable sanitized reference tree for an isolated run, when one exists.
    snapshot_path: Path | None = None
    sanitize_policy: str = ""

    @classmethod
    def for_host(cls, path: Path) -> "RuntimeWorkspace":
        base = Path(path).resolve()
        return cls("host", base, base, manifest(base), sanitize_policy=sanitize.policy_sha256())

    @classmethod
    def for_container(
        cls,
        backend: "ExecutionBackend",
        source: Path,
        *,
        snapshot_id: str = "",
    ) -> "RuntimeWorkspace":
        runtime = backend.provision()
        sanitized_manifest_of = getattr(backend, "sanitized_manifest", None)
        if callable(sanitized_manifest_of):
            source_manifest = sanitized_manifest_of(runtime)
        else:  # pragma: no cover - compatibility with deliberately simple fakes
            source_manifest = manifest(runtime.snapshot or source)
        # The backend provisions the sanitized reference and populates the
        # overlay from it; verify the task-visible tree matches before any tool
        # or command is allowed to use it.
        provided = manifest(runtime.overlay)
        if provided != source_manifest:
            raise ValueError(
                "the task overlay does not match the sanitized source manifest; "
                "refusing to run against a partial workspace"
            )
        return cls(
            "container",
            Path(source).resolve(),
            runtime.overlay,
            source_manifest,
            runtime,
            backend,
            snapshot_path=runtime.snapshot,
            sanitize_policy=sanitize.policy_sha256(),
        )

    @property
    def overlay_hash(self) -> str:
        """Digest of the current candidate tree, for evidence records."""

        return _sha256(self._joined_manifest(manifest(self.path)))

    @staticmethod
    def _joined_manifest(values: dict[str, str]) -> bytes:
        return "".join(f"{name}:{digest}\n" for name, digest in sorted(values.items())).encode(
            "utf-8"
        )

    def source_hash(self) -> str:
        return _sha256(self._joined_manifest(self.source_manifest))

    def candidate_manifest(self) -> dict[str, str]:
        return manifest(self.path)

    def content_diff(self) -> list[dict[str, str]]:
        return content_diff(self.source_manifest, self.candidate_manifest())

    def collect_artifacts(self) -> list[Path]:
        if self.backend is None or self.runtime is None:
            return []
        return list(self.backend.collect_artifacts(self.runtime))

    @classmethod
    def resume(
        cls,
        backend: "ExecutionBackend",
        runtime: "RuntimeRef",
        source: Path,
        *,
        source_manifest: dict[str, str] | None = None,
    ) -> "RuntimeWorkspace":
        """Reuse a retained overlay for verification of the exact candidate."""

        base = Path(source).resolve()
        return cls(
            "container",
            base,
            runtime.overlay,
            source_manifest if source_manifest is not None else manifest(base),
            runtime,
            backend,
            snapshot_path=runtime.snapshot,
            sanitize_policy=sanitize.policy_sha256(),
        )

    def stop(self, *, reason: str, keep_overlay: bool = False) -> None:
        if self.backend is not None and self.runtime is not None:
            self.backend.stop(self.runtime, reason=reason, keep_overlay=keep_overlay)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "base_path": str(self.base_path),
            "active_path": str(self.path),
            "snapshot_path": str(self.snapshot_path) if self.snapshot_path else None,
            "source_files": len(self.source_manifest),
            "source_manifest_sha256": self.source_hash(),
            "runtime_id": self.runtime.id if self.runtime else None,
            "backend": self.backend.backend_id if self.backend else None,
            "image": self.runtime.image if self.runtime else None,
            "mount_policy_sha256": self.runtime.mount_policy if self.runtime else None,
            "limit_policy_sha256": (
                self.backend.limit_policy_sha256()
                if self.runtime is not None
                and hasattr(self.backend, "limit_policy_sha256")
                else None
            ),
            "sanitize_policy_sha256": self.sanitize_policy,
            "overlay_hash": self.overlay_hash,
        }


def import_diff_to_worktree(workspace: RuntimeWorkspace, target: Path) -> list[dict[str, str]]:
    """Import the exact candidate overlay contents into a Git worktree.

    Container overlays are not Git repositories, so the durable patch must be
    captured through a controlled boundary: apply only the content-aware diff,
    validate every path stays under the worktree, then let the existing
    capture_diff path hash and store the patch. Never copy secrets or symlinks.
    """

    target = Path(target).resolve()
    base = Path(workspace.base_path).resolve()
    if target != base:
        # Baseline and candidate trees must be the same pinned snapshot.
        source = manifest(base)
        if source != workspace.source_manifest:
            raise ValueError("the base worktree changed since the runtime was provisioned")
    changes = workspace.content_diff()
    for change in changes:
        rel = change["path"]
        destination = (target / rel).resolve()
        if not destination.is_relative_to(target):
            raise ValueError(f"candidate path escapes the worktree: {rel}")
        if change["status"] == "deleted":
            if destination.exists():
                destination.unlink()
            continue
        source_file = workspace.path / rel
        if not source_file.is_file() or source_file.is_symlink():
            raise ValueError(f"candidate path is not a regular file: {rel}")
        data = source_file.read_bytes()
        if contains_secret_material(data.decode("utf-8", errors="replace")):
            raise ValueError(f"candidate file contains credential material: {rel}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return changes

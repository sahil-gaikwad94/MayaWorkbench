"""One task, one workspace: the single file view used by tools and commands.

The control plane must not have two competing notions of the task workspace.
For ``LOCAL_TRUSTED`` the active view is the host worktree (the historical
behaviour). For an isolated backend it is a populated host-side overlay that is
mounted writable at the container's ``/work``, so a file written by a tool is
the same file a command reads, and a file changed by a command is in the same
tree the artifact/diff collector sees.

An isolated task NEVER bind-mounts the original user worktree. A separate,
immutable, secret-free *sanitized source snapshot* is created by the same
canonical filter as the overlay and mounted read-only at ``/src`` for baseline
reference. A process inside the container cannot read ``.env``, credential
files, ``.git`` internals or any excluded path from either mount.

Secrets, symlinks and large generated trees are never copied into either tree.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mayaworkbench.workspace.policy import contains_secret_material, is_secret_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mayaworkbench.workspace.backends import ExecutionBackend, RuntimeRef

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".maya",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".idea",
        ".vscode",
        ".tox",
        ".nox",
    }
)
MAX_OVERLAY_FILES = 20_000
MAX_OVERLAY_BYTES = 200_000_000
MAX_OVERLAY_FILE_BYTES = 20_000_000

#: Bump when the source-sanitization policy changes so a recorded manifest can
#: state which filter produced it. A policy change invalidates a cached mount.
SANITIZED_SOURCE_POLICY_VERSION = "2"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excluded(rel: str) -> bool:
    """The single canonical "may this path enter a task tree?" predicate."""
    parts = rel.split("/")
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if is_secret_path(rel) or parts[-1].lower().endswith(".env"):
        return True
    return False


def manifest(path: Path) -> dict[str, str]:
    """Content manifest of a *task-visible* workspace tree (path -> sha256).

    This applies the same filter as :func:`populate_overlay`, including
    credential-shaped *content*. Otherwise a normally-named file containing an
    API key would appear in the reference manifest yet be absent from the
    overlay, which would either fail a legitimate provisioning check or tempt a
    caller to weaken the filter to make the numbers agree.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        rel = item.relative_to(path).as_posix()
        if _excluded(rel):
            continue
        data = item.read_bytes()
        if contains_secret_material(data.decode("utf-8", errors="replace")):
            continue
        result[rel] = _sha256(data)
    return result


def populate_overlay(
    source: Path,
    overlay: Path,
    *,
    max_files: int = MAX_OVERLAY_FILES,
    max_bytes: int = MAX_OVERLAY_BYTES,
    freeze: bool = False,
) -> dict[str, str]:
    """Copy a clean, secret-free snapshot into a task tree.

    With ``freeze=False`` the result is a writable task overlay (``/work``).
    With ``freeze=True`` the result is an immutable sanitized source snapshot
    (``/src``): every file is mode 0444 and every directory 0555, so even a
    misconfigured bind mount cannot grant writes to the reference tree.

    Both modes use the *same* canonical filter, so their manifests are equal by
    construction. Raising here blocks the task before the first command: an
    incomplete tree must never be presented as the exact tree that will be
    verified.
    """
    source = Path(source).resolve()
    overlay = Path(overlay).resolve()
    overlay.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied: dict[str, str] = {}
    total = 0
    frozen_dirs: set[Path] = set()
    for item in sorted(source.rglob("*")):
        if len(copied) >= max_files:
            raise ValueError(f"task tree exceeds {max_files} files")
        relative = item.relative_to(source)
        rel = relative.as_posix()
        if _excluded(rel):
            continue
        if item.is_symlink():
            # Never follow a host symlink out of the snapshot.
            continue
        if item.is_dir():
            target = overlay / relative
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            if freeze:
                frozen_dirs.add(target)
            continue
        if not item.is_file():
            continue
        size = item.stat().st_size
        if size > MAX_OVERLAY_FILE_BYTES or total + size > max_bytes:
            raise ValueError(f"task tree exceeds its byte budget while copying {rel}")
        data = item.read_bytes()
        if contains_secret_material(data.decode("utf-8", errors="replace")):
            continue
        target = overlay / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        frozen_dirs.add(target.parent)
        target.write_bytes(data)
        mode = stat.S_IMODE(item.stat().st_mode)
        # A writable overlay keeps the source mode but stays owner-writable so
        # an agent can actually edit the project it was asked to change. A
        # frozen reference is chmod-ed only after every file is written.
        if not freeze:
            os.chmod(target, (mode & 0o777) | 0o600)
        copied[rel] = _sha256(data)
        total += size
    if freeze:
        # Freeze bottom-up so a parent never blocks writing its children.
        for directory in sorted(frozen_dirs, key=lambda p: len(p.parts), reverse=True):
            os.chmod(directory, 0o555)
        for item in overlay.rglob("*"):
            if item.is_file() and not item.is_symlink():
                os.chmod(item, 0o444)
        os.chmod(overlay, 0o555)
    return copied


def sanitize_source_tree(
    source: Path,
    destination: Path,
    *,
    max_files: int = MAX_OVERLAY_FILES,
    max_bytes: int = MAX_OVERLAY_BYTES,
) -> dict[str, str]:
    """Create an immutable, secret-free source snapshot for an untrusted task.

    This is the ONLY way original project content may enter a task container.
    The original worktree is never bind-mounted: doing so would let a project
    process read files the overlay filter deliberately excluded (``/src/.env``,
    ``/src/.git/config``, credential-shaped source files).
    """
    return populate_overlay(
        source, destination, max_files=max_files, max_bytes=max_bytes, freeze=True
    )


def sanitized_policy_fingerprint(manifest_data: dict[str, str]) -> str:
    """Hash a sanitized manifest together with the policy that produced it."""
    joined = "".join(f"{name}:{digest}\n" for name, digest in sorted(manifest_data.items()))
    return _sha256(f"{SANITIZED_SOURCE_POLICY_VERSION}\n{joined}".encode("utf-8"))


def unfreeze_tree(path: Path) -> None:
    """Restore owner-write bits so a frozen snapshot can be removed.

    A task's sanitized `/src` is deliberately 0555/0444. Unlinking files inside
    a non-writable directory needs write permission on that directory, which a
    capability-dropped host process may not have (CAP_DAC_OVERRIDE is absent in
    many CI containers). Cleanup must therefore restore permissions bottom-up
    before ``shutil.rmtree`` — otherwise "stop()" silently leaks the container
    tree, defeating the cleanup attestation.
    """
    path = Path(path)
    if not path.exists():
        return
    for item in path.rglob("*"):
        try:
            os.chmod(item, 0o600 if item.is_file() and not item.is_dir() else 0o700)
        except OSError:  # pragma: no cover - best effort before rmtree
            pass
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover
        pass


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
    base_path: Path  # immutable pinned source
    path: Path  # the active view every file tool and command uses
    source_manifest: dict[str, str]
    runtime: "RuntimeRef | None" = None
    backend: "ExecutionBackend | None" = None

    @classmethod
    def for_host(cls, path: Path) -> "RuntimeWorkspace":
        base = Path(path).resolve()
        return cls("host", base, base, manifest(base))

    @classmethod
    def for_container(
        cls,
        backend: "ExecutionBackend",
        source: Path,
        *,
        snapshot_id: str = "",
    ) -> "RuntimeWorkspace":
        runtime = backend.provision()
        # The authoritative reference is the sanitized snapshot the backend
        # actually mounted at /src, never the original user worktree. Both the
        # overlay and the reference are produced by the same filter, so their
        # manifests must be identical before any tool or command may run.
        reference = runtime.sanitized_source or Path(source).resolve()
        source_manifest = manifest(reference)
        provided = manifest(runtime.overlay)
        if provided != source_manifest:
            raise ValueError(
                "the task overlay does not match the sanitized source manifest; "
                "refusing to run against a partial workspace"
            )
        return cls("container", Path(source).resolve(), runtime.overlay, source_manifest, runtime, backend)

    @property
    def overlay_hash(self) -> str:
        """Digest of the current candidate tree, for evidence records."""
        joined = "".join(f"{name}:{digest}\n" for name, digest in sorted(manifest(self.path).items()))
        return _sha256(joined.encode("utf-8"))

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
        )

    def stop(self, *, reason: str, keep_overlay: bool = False) -> None:
        if self.backend is not None and self.runtime is not None:
            self.backend.stop(self.runtime, reason=reason, keep_overlay=keep_overlay)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "base_path": str(self.base_path),
            "active_path": str(self.path),
            "source_files": len(self.source_manifest),
            "runtime_id": self.runtime.id if self.runtime else None,
            "backend": self.backend.backend_id if self.backend else None,
            "image": self.runtime.image if self.runtime else None,
            "overlay_hash": self.overlay_hash,
            "sanitized_source": str(self.runtime.sanitized_source) if self.runtime and getattr(self.runtime, "sanitized_source", None) else None,
            "mount_policy_sha256": getattr(self.runtime, "mount_policy_sha256", "") if self.runtime else "",
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


__all__ = [
    "import_diff_to_worktree",
    "RuntimeWorkspace",
    "populate_overlay",
    "sanitize_source_tree",
    "sanitized_policy_fingerprint",
    "unfreeze_tree",
    "manifest",
    "content_diff",
    "EXCLUDED_DIRS",
    "SANITIZED_SOURCE_POLICY_VERSION",
]

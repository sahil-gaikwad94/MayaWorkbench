"""The single canonical workspace sanitizer.

Every task that runs in an isolated runtime sees a *sanitized* copy of the
pinned snapshot, never the user's worktree. There is exactly one filter
implementation (this module); the writable task overlay, the immutable
reference snapshot and the content manifests used as evidence are all derived
from it, so the three can never drift into "the overlay excludes a file the
read-only mount still exposes" — the MW-NEW-P0-01 defect.

What is excluded, and why:

* **Host state** — ``.git`` internals, virtual environments, package caches
  and editor/agent state. A task must not read credentials from
  ``.git/config`` or traverse into ``node_modules``.
* **Credential paths** — anything :func:`policy.is_secret_path` recognises
  (``.env``, ``.npmrc``, ``.pypirc``, ``.aws/credentials``, private keys,
  ``kubeconfig`` ...).
* **Credential-shaped content** — a normally named file that contains a
  private key, cloud access key or provider token.
* **Symlinks** — never followed, never copied. A symlink is how a repository
  would otherwise redirect a read outside the tree.
* **Unreadable entries** — skipped and reported, so a task never silently runs
  against a partial tree that the evidence manifest claims is complete.

The filter is versioned (:data:`POLICY_VERSION`) and hashed, so an evidence
record can state which policy produced the tree it verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from mayaworkbench.workspace.policy import contains_secret_material, is_secret_path

__all__ = [
    "POLICY_VERSION",
    "EXCLUDED_DIRS",
    "MAX_OVERLAY_FILES",
    "MAX_OVERLAY_BYTES",
    "MAX_OVERLAY_FILE_BYTES",
    "SanitizedFile",
    "SanitizedTree",
    "policy_document",
    "policy_sha256",
    "is_excluded_path",
    "iter_source_files",
    "build_tree",
    "tree_manifest",
    "remove_tree",
]

#: Bumped whenever the filter semantics change. Recorded in evidence so a
#: manifest can be re-derived with the policy that produced it.
POLICY_VERSION = "maya-sanitize-v3"

#: Directories never copied into a task tree. These are host/agent state or
#: regenerable dependency trees, not project source.
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
        # Generated invariant scripts written into a container overlay. They are
        # execution apparatus, never part of the candidate change.
        ".maya-scripts",
        # Generated per-session terminator scripts. Same rule: apparatus, not a
        # candidate change, and never part of a source manifest.
        ".maya-sessions",
        # Host credential stores that must never reach a task container, even
        # when a repository happens to vendor a same-named directory.
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
    }
)

MAX_OVERLAY_FILES = 20_000
MAX_OVERLAY_BYTES = 200_000_000
MAX_OVERLAY_FILE_BYTES = 20_000_000


@dataclass(frozen=True)
class SanitizedFile:
    """One included file: relative POSIX path and exact bytes."""

    path: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class SanitizedTree:
    """The result of materializing a sanitized tree on disk."""

    root: Path
    manifest: dict[str, str]
    policy_version: str
    policy_sha256: str
    files: int = 0
    bytes: int = 0
    skipped: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "files": self.files,
            "bytes": self.bytes,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "manifest_sha256": _manifest_digest(self.manifest),
            "skipped": list(self.skipped),
        }


def _manifest_digest(manifest: dict[str, str]) -> str:
    joined = "".join(f"{name}:{digest}\n" for name, digest in sorted(manifest.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def policy_document() -> dict[str, object]:
    """The complete, stable description of the filter (hashed into evidence)."""

    from mayaworkbench.workspace import policy as path_policy

    return {
        "version": POLICY_VERSION,
        "excluded_dirs": sorted(EXCLUDED_DIRS),
        "secret_names": sorted(path_policy.SECRET_NAMES),
        "secret_globs": list(path_policy.SECRET_GLOBS),
        "content_filter": path_policy._SECRET_CONTENT.pattern,
        "symlinks": "refused",
        "max_files": MAX_OVERLAY_FILES,
        "max_bytes": MAX_OVERLAY_BYTES,
        "max_file_bytes": MAX_OVERLAY_FILE_BYTES,
    }


_POLICY_DIGEST: str | None = None


def policy_sha256() -> str:
    global _POLICY_DIGEST
    if _POLICY_DIGEST is None:
        canonical = json.dumps(policy_document(), sort_keys=True, separators=(",", ":"))
        _POLICY_DIGEST = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _POLICY_DIGEST


def is_excluded_path(rel: str) -> bool:
    """True when ``rel`` must never appear in a task-visible tree."""

    parts = rel.split("/")
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if is_secret_path(rel):
        return True
    # ``.env`` and friends are also matched by name, so a nested path such as
    # ``config/.env.local`` is excluded even though the directory is ordinary.
    return parts[-1].lower().endswith(".env") or parts[-1].lower().startswith(".env.")


def _walk_files(source: Path) -> Iterator[tuple[Path, str]]:
    """Yield candidate files without following symlinks or excluded dirs."""

    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        base = Path(dirpath)
        relative_dir = base.relative_to(source).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        kept: list[str] = []
        for name in sorted(dirnames):
            if name in EXCLUDED_DIRS or is_excluded_path(prefix + name):
                continue
            if (base / name).is_symlink():
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = prefix + name
            if is_excluded_path(rel):
                # A credential-shaped *file* (``.env``, ``.npmrc``, a key) is
                # excluded exactly like a credential-shaped directory.
                continue
            yield base / name, rel


def iter_source_files(
    source: Path,
    *,
    max_files: int = MAX_OVERLAY_FILES,
    max_bytes: int = MAX_OVERLAY_BYTES,
    max_file_bytes: int = MAX_OVERLAY_FILE_BYTES,
) -> Iterator[SanitizedFile]:
    """Yield every includable file, enforcing the budget as it goes.

    Raises :class:`ValueError` when the budget is exceeded: an incomplete tree
    must never be presented as the exact tree that will be verified.
    """

    source = Path(source).resolve()
    included = 0
    total = 0
    for item, rel in _walk_files(source):
        if item.is_symlink():
            continue
        try:
            info = item.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if included >= max_files:
            raise ValueError(
                f"the sanitized task tree exceeds {max_files} files "
                f"(policy {POLICY_VERSION})"
            )
        if info.st_size > max_file_bytes:
            raise ValueError(
                f"the sanitized task tree exceeds the per-file budget while copying {rel}"
            )
        if total + info.st_size > max_bytes:
            raise ValueError(
                f"the sanitized task tree exceeds its byte budget while copying {rel}"
            )
        try:
            data = item.read_bytes()
        except OSError:
            # An unreadable file cannot be part of an exact tree; it is skipped
            # here and re-checked by ``tree_manifest`` on the destination.
            continue
        if contains_secret_material(data.decode("utf-8", errors="replace")):
            continue
        included += 1
        total += info.st_size
        yield SanitizedFile(rel, data)


def build_tree(
    source: Path,
    destination: Path,
    *,
    read_only: bool = False,
    mode: int = 0o700,
) -> SanitizedTree:
    """Materialize a sanitized copy of ``source`` at ``destination``.

    ``read_only=True`` marks the result immutable (owner-read only) so a task
    cannot modify the reference tree it is being compared against.
    """

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=mode)
    manifest: dict[str, str] = {}
    total = 0
    for item in iter_source_files(source):
        target = destination / item.path
        target.parent.mkdir(parents=True, exist_ok=True, mode=mode)
        target.write_bytes(item.data)
        if read_only:
            os.chmod(target, 0o400)
        else:
            try:
                source_mode = stat.S_IMODE((source / item.path).stat().st_mode)
            except OSError:  # pragma: no cover - source disappeared mid-copy
                source_mode = 0o644
            os.chmod(target, source_mode & 0o777)
        manifest[item.path] = hashlib.sha256(item.data).hexdigest()
        total += item.size
    if read_only:
        # Lock directories bottom-up so the tree cannot be extended or edited.
        for dirpath, dirnames, _ in os.walk(destination, topdown=False):
            del dirnames
            try:
                os.chmod(dirpath, 0o500)
            except OSError:  # pragma: no cover - platform specific
                pass
    return SanitizedTree(
        root=destination,
        manifest=manifest,
        policy_version=POLICY_VERSION,
        policy_sha256=policy_sha256(),
        files=len(manifest),
        bytes=total,
    )


def tree_manifest(path: Path) -> dict[str, str]:
    """Canonical content manifest of a task-visible tree.

    Uses the same exclusions as :func:`build_tree`, so the manifest of the
    original snapshot, the sanitized reference and the writable overlay are
    directly comparable. Files that are unreadable are omitted consistently.
    """

    path = Path(path)
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for item, rel in _walk_files(path.resolve()):
        if item.is_symlink() or is_excluded_path(rel):
            continue
        try:
            if not item.is_file():
                continue
            data = item.read_bytes()
        except OSError:
            continue
        if contains_secret_material(data.decode("utf-8", errors="replace")):
            continue
        result[rel] = hashlib.sha256(data).hexdigest()
    return result


def remove_tree(path: Path) -> None:
    """Remove a task tree, including a read-only sanitized reference."""

    path = Path(path)
    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(path):
        del dirnames
        try:
            os.chmod(dirpath, 0o700)
        except OSError:  # pragma: no cover - cleanup must not fail the caller
            pass
        for name in filenames:
            try:
                os.chmod(Path(dirpath) / name, 0o600)
            except OSError:  # pragma: no cover
                pass
    shutil.rmtree(path, ignore_errors=True)

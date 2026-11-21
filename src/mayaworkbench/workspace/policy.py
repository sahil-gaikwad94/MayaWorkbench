"""Path policy for the isolated workspace.

This module is deliberately independent of Git and SQLite so it can be tested
as a pure boundary. Every read and write made by the tool gateway passes through
it. The policy enforces four separate ideas:

* **containment** — the resolved path is inside the workspace root;
* **scope** — writes are only inside ``allowed_paths`` and never inside
  ``excluded_paths``;
* **symlink refusal** — no path component may be a symlink, so a repository
  cannot redirect a write outside the workspace;
* **secret refusal** — credential-shaped filenames are never readable or
  writable by the agent.

Nothing here trusts a caller-provided path. A model can ask for ``../../etc``
or a path with a symlinked parent and receives a typed refusal.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterable

SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    ".pgpass",
    ".htpasswd",
    "kubeconfig",
}

SECRET_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.p8",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    ".env.*",
    "**/credentials*.json",
    "**/secrets*.yaml",
    "**/secrets*.yml",
    "**/.docker/config.json",
    "**/service-account*.json",
)

# High-confidence credential markers. This is used on file *writes* (not reads)
# and on candidate patches, so an agent cannot copy a private key into a file.
_SECRET_CONTENT = re.compile(
    r"(-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    r"|AKIA[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|sk-[A-Za-z0-9]{32,})"
)

# A path this long is not a legitimate repository path and is likely an attack
# or a runaway generator.
MAX_PATH_LENGTH = 512
MAX_PATH_COMPONENTS = 64
MAX_COMPONENT_LENGTH = 255

#: Characters that must never reach a path, a log line or a diff header.
#: C0/C1 controls break line-oriented output, and the bidi overrides and
#: line/paragraph separators can make a path render as a different path than
#: the one checked here (Trojan Source, CVE-2021-42574 class of spoofing).
_DISALLOWED_PATH_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u2028\u2029]"
)


class PolicyViolation(Exception):
    """A path or content request is outside the task's allowed boundary."""

    def __init__(self, message: str, *, path: str | None = None):
        super().__init__(message)
        self.path = path


def normalize_rel(path: str) -> str:
    """Return a canonical repository-relative POSIX path or raise."""
    if not isinstance(path, str) or not path:
        raise PolicyViolation("path must be a non-empty string", path=path)
    if "\x00" in path:
        raise PolicyViolation("path contains a null byte", path=path)
    if _DISALLOWED_PATH_CHARS.search(path):
        raise PolicyViolation(
            "path contains control characters or bidirectional overrides", path=path
        )
    if len(path) > MAX_PATH_LENGTH:
        raise PolicyViolation(f"path exceeds {MAX_PATH_LENGTH} characters", path=path)
    candidate = path.replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        raise PolicyViolation("absolute paths are not allowed", path=path)
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyViolation("path must be normalised and may not contain '..'", path=path)
    if len(parts) > MAX_PATH_COMPONENTS:
        raise PolicyViolation("path has too many components", path=path)
    for part in parts:
        if len(part.encode("utf-8")) > MAX_COMPONENT_LENGTH:
            raise PolicyViolation(
                f"a path component exceeds {MAX_COMPONENT_LENGTH} bytes", path=path
            )
    if parts[0] == ".git":
        raise PolicyViolation("the .git directory is never accessible", path=path)
    return "/".join(parts)


def is_secret_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in SECRET_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path, pattern) for pattern in SECRET_GLOBS)


def contains_secret_material(text: str) -> bool:
    return bool(_SECRET_CONTENT.search(text))


class PathPolicy:
    """Resolve workspace paths with containment, scope and symlink checks."""

    def __init__(
        self,
        root: str | Path,
        *,
        allowed_paths: Iterable[str],
        excluded_paths: Iterable[str] = (),
        allow_file_deletion: bool = False,
    ):
        self.root = Path(root).resolve()
        self.allowed_paths = tuple(normalize_rel(p) for p in allowed_paths)
        self.excluded_paths = tuple(normalize_rel(p) for p in excluded_paths)
        self.allow_file_deletion = allow_file_deletion
        if not self.allowed_paths:
            raise PolicyViolation("at least one allowed path is required")
        overlap = set(self.allowed_paths) & set(self.excluded_paths)
        if overlap:
            raise PolicyViolation(
                f"paths cannot be both allowed and excluded: {sorted(overlap)}"
            )

    # ------------------------------------------------------------------ scope
    def _matches(self, path: str, patterns: Iterable[str]) -> bool:
        for pattern in patterns:
            if path == pattern or path.startswith(pattern + "/"):
                return True
            if any(char in pattern for char in "*?[") and fnmatch.fnmatch(path, pattern):
                return True
            # A directory-shaped pattern also matches descendants.
            if fnmatch.fnmatch(path, pattern + "/*"):
                return True
        return False

    def is_allowed(self, path: str) -> bool:
        return self._matches(path, self.allowed_paths)

    def is_excluded(self, path: str) -> bool:
        return self._matches(path, self.excluded_paths)

    def check_write(self, path: str) -> str:
        rel = normalize_rel(path)
        if not self.is_allowed(rel):
            raise PolicyViolation(
                f"path is outside the task's allowed scope: {rel}", path=rel
            )
        if self.is_excluded(rel):
            raise PolicyViolation(f"path is excluded by the task contract: {rel}", path=rel)
        if is_secret_path(rel):
            raise PolicyViolation(f"credential-shaped paths are not writable: {rel}", path=rel)
        return rel

    def check_read(self, path: str) -> str:
        rel = normalize_rel(path)
        if self.is_excluded(rel):
            raise PolicyViolation(f"path is excluded by the task contract: {rel}", path=rel)
        if is_secret_path(rel):
            raise PolicyViolation(f"credential-shaped paths are not readable: {rel}", path=rel)
        return rel

    # -------------------------------------------------------------- resolution
    def _refuse_symlink_components(self, rel: str) -> None:
        current = self.root
        for part in rel.split("/"):
            current = current / part
            try:
                if current.is_symlink():
                    raise PolicyViolation(
                        f"symlink components are not allowed: {rel}", path=rel
                    )
            except OSError as exc:  # pragma: no cover - filesystem failure
                raise PolicyViolation(f"cannot inspect path: {rel}: {exc}", path=rel) from exc

    def resolve(self, rel: str, *, for_write: bool) -> Path:
        normalized = self.check_write(rel) if for_write else self.check_read(rel)
        self._refuse_symlink_components(normalized)
        target = (self.root / normalized).resolve(strict=False)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise PolicyViolation(
                f"resolved path escapes the workspace: {rel}", path=normalized
            ) from exc
        return target

    def existing_file(self, rel: str, *, for_write: bool = False) -> Path:
        target = self.resolve(rel, for_write=for_write)
        if not target.exists():
            raise PolicyViolation(f"file does not exist: {rel}", path=rel)
        if target.is_symlink() or not target.is_file():
            raise PolicyViolation(f"not a regular file: {rel}", path=rel)
        return target

    def writable_file(self, rel: str) -> Path:
        target = self.resolve(rel, for_write=True)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise PolicyViolation(f"not a regular file: {rel}", path=rel)
        parent = target.parent
        try:
            parent.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - defensive
            raise PolicyViolation(f"parent escapes workspace: {rel}", path=rel) from exc
        return target

    def deletion_allowed(self, rel: str) -> bool:
        if not self.allow_file_deletion:
            raise PolicyViolation(
                "file deletion is not enabled by this task contract", path=rel
            )
        target = self.existing_file(rel, for_write=True)
        if target.name == ".git" or ".git" in target.parts:
            raise PolicyViolation("refusing to delete Git metadata", path=rel)
        return True

    # ------------------------------------------------------------------ listing
    def visible_paths(self, *, limit: int = 500) -> list[str]:
        """List readable files for prompts and the list tool.

        Excluded and secret paths are omitted entirely so the agent never sees
        their names or content. Symlinks are skipped, not followed.
        """
        results: list[str] = []
        if not self.root.is_dir():
            return results
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            base = Path(dirpath)
            relative_dir = base.relative_to(self.root).as_posix()
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name != ".git"
                and not self._relative_hidden(relative_dir, name)
            )
            for name in sorted(filenames):
                rel = name if relative_dir == "." else f"{relative_dir}/{name}"
                try:
                    normalized = normalize_rel(rel)
                except PolicyViolation:
                    continue
                if self.is_excluded(normalized) or is_secret_path(normalized):
                    continue
                candidate = base / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                results.append(normalized)
                if len(results) >= limit:
                    return results
        return results

    def _relative_hidden(self, directory: str, name: str) -> bool:
        rel = name if directory == "." else f"{directory}/{name}"
        try:
            normalized = normalize_rel(rel)
        except PolicyViolation:
            return True
        return self.is_excluded(normalized) or is_secret_path(normalized)


__all__ = [
    "PathPolicy",
    "PolicyViolation",
    "normalize_rel",
    "is_secret_path",
    "contains_secret_material",
]

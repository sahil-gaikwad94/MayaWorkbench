"""Repository file discovery.

Every exclusion is recorded with a reason. Nothing is silently dropped: the
index reports what it skipped and why (§8.2), because an evidence-first tool
must not imply it read files it never opened.

Git's own ignore machinery is authoritative for worktree discovery, so the
index respects ``.gitignore`` exactly as the user's build does.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from mayaworkbench.config import IndexingSettings
from mayaworkbench.domain.enums import IssueKind, IssueSeverity
from mayaworkbench.domain.models import IndexIssue
from mayaworkbench.ids import new_ulid
from mayaworkbench.repositories.git import GitClient, TreeEntry

# High-confidence secret signatures only. Aggressive entropy heuristics create
# false exclusions, which are themselves an integrity risk, so anything less
# certain is reported as a low-confidence hint rather than dropped.
SECRET_SIGNATURES: tuple[tuple[str, str], ...] = (
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----", "private key block"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bASIA[0-9A-Z]{16}\b", "AWS temporary access key id"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "GitHub token"),
    (r"\bsk-[A-Za-z0-9]{32,}\b", "API secret key"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b", "Slack token"),
)

SECRET_CONTENT_PATTERNS: tuple[tuple[re.Pattern[bytes], str], ...] = tuple(
    (re.compile(pattern.encode("utf-8")), label) for pattern, label in SECRET_SIGNATURES
)

#: The same signatures for already-decoded text (UTF-16/UTF-8 with a BOM), where
#: the byte patterns would not match because of interleaved NUL bytes.
SECRET_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), label) for pattern, label in SECRET_SIGNATURES
)

BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class DiscoveryPolicy:
    max_file_bytes: int
    max_total_bytes: int
    max_files: int
    excluded_dirs: tuple[str, ...]
    excluded_globs: tuple[str, ...]
    extra_included_globs: tuple[str, ...]
    secret_filename_patterns: tuple[str, ...]
    scan_content_for_secrets: bool

    @classmethod
    def from_settings(cls, settings: IndexingSettings) -> "DiscoveryPolicy":
        return cls(
            max_file_bytes=settings.max_file_bytes,
            max_total_bytes=settings.max_total_bytes,
            max_files=settings.max_files,
            excluded_dirs=tuple(settings.excluded_dirs),
            excluded_globs=tuple(settings.excluded_globs),
            extra_included_globs=tuple(settings.extra_included_globs),
            secret_filename_patterns=tuple(settings.secret_filename_patterns),
            scan_content_for_secrets=settings.scan_content_for_secrets,
        )


@dataclass(frozen=True)
class Candidate:
    path: str
    size_bytes: int
    mode: str | None = None
    blob_sha: str | None = None


@dataclass(frozen=True)
class SkippedFile:
    path: str
    kind: IssueKind
    detail: str
    size_bytes: int = 0


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    total_bytes: int = 0
    truncated_by_budget: bool = False
    ignored_path_count: int = 0

    def issues(self, snapshot_id: str) -> list[IndexIssue]:
        return [
            IndexIssue(
                id="iss_" + new_ulid(),
                snapshot_id=snapshot_id,
                path=skipped.path,
                kind=skipped.kind,
                severity=_severity_for(skipped.kind),
                detail=skipped.detail,
            )
            for skipped in self.skipped
        ]


def _severity_for(kind: IssueKind) -> IssueSeverity:
    if kind in (IssueKind.SKIPPED_SECRET, IssueKind.BUDGET_EXCEEDED, IssueKind.MUTATED_DURING_INDEX):
        return IssueSeverity.ERROR
    if kind in (IssueKind.PARSE_ERROR, IssueKind.ENCODING_ERROR, IssueKind.SKIPPED_UNREADABLE):
        return IssueSeverity.WARNING
    return IssueSeverity.INFO


def _matches(path: str, patterns: tuple[str, ...]) -> str | None:
    basename = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatchcase(basename, pattern) or fnmatch.fnmatchcase(path, pattern):
            return pattern
    return None


def _excluded_dir(path: str, excluded_dirs: tuple[str, ...]) -> str | None:
    excluded = set(excluded_dirs)
    for part in path.split("/")[:-1]:
        if part in excluded:
            return part
    return None


def secret_filename_match(path: str, patterns: tuple[str, ...]) -> str | None:
    basename = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatchcase(basename, pattern) or fnmatch.fnmatchcase(path, pattern):
            return pattern
    return None


UTF8_BOM = "\ufeff".encode("utf-8")
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def has_text_bom(data: bytes) -> bool:
    return data.startswith(UTF8_BOM) or data.startswith(UTF16_BOMS)


def decode_indexable_text(data: bytes) -> str:
    """Decode text the way the index stores it, honouring a byte-order mark.

    A BOM is metadata, not content. Left in place, a UTF-8 BOM becomes an
    invisible ``\ufeff`` character that makes ``ast.parse`` fail with "invalid
    non-printable character", so a Windows-edited Python file would be indexed
    as a parse error for no real reason. UTF-16 text contains NUL bytes and is
    otherwise mistaken for binary. Both are decoded here, once, so the indexer,
    search and the AST parser agree about what the file contains.
    """
    if data.startswith(UTF8_BOM):
        return data.decode("utf-8-sig")
    if data.startswith(UTF16_BOMS):
        return data.decode("utf-16")
    return data.decode("utf-8")


def inspect_content(data: bytes, policy: DiscoveryPolicy) -> tuple[bool, IssueKind | None, str]:
    """Decide whether content is safe/indexable.

    Returns ``(indexable, skip_kind, detail)``. Content that is not decodable
    text is skipped rather than guessed at, and secret-looking content is never
    stored. A byte-order mark is a decoding hint, never a reason to skip.
    """
    bom_text = False
    if has_text_bom(data):
        try:
            text = decode_indexable_text(data)
            bom_text = True
        except UnicodeDecodeError:
            return (
                False,
                IssueKind.ENCODING_ERROR,
                "content has a byte-order mark but does not decode as text",
            )
    else:
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            return False, IssueKind.SKIPPED_BINARY, "binary content (NUL byte)"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False, IssueKind.ENCODING_ERROR, "not valid UTF-8; content was not indexed"
    if policy.scan_content_for_secrets:
        if bom_text:
            # The byte patterns would not match across UTF-16's interleaved NULs.
            for pattern, label in SECRET_TEXT_PATTERNS:
                if pattern.search(text[:200_000]):
                    return False, IssueKind.SKIPPED_SECRET, f"possible secret ({label})"
        else:
            head = data[:200_000]
            for pattern, label in SECRET_CONTENT_PATTERNS:
                if pattern.search(head):
                    return False, IssueKind.SKIPPED_SECRET, f"possible secret ({label})"
    return True, None, ""


def _evaluate(
    path: str,
    size_bytes: int,
    policy: DiscoveryPolicy,
) -> SkippedFile | None:
    excluded_dir = _excluded_dir(path, policy.excluded_dirs)
    if excluded_dir:
        return SkippedFile(path, IssueKind.EXCLUDED_DIRECTORY, f"excluded directory {excluded_dir}/")
    if not _matches(path, policy.extra_included_globs):
        matched = _matches(path, policy.excluded_globs)
        if matched:
            return SkippedFile(path, IssueKind.EXCLUDED_GLOB, f"matched excluded pattern {matched}")
    secret = secret_filename_match(path, policy.secret_filename_patterns)
    if secret:
        return SkippedFile(path, IssueKind.SKIPPED_SECRET, f"sensitive file pattern {secret}")
    if size_bytes > policy.max_file_bytes:
        return SkippedFile(
            path,
            IssueKind.SKIPPED_TOO_LARGE,
            f"{size_bytes} bytes exceeds max_file_bytes={policy.max_file_bytes}",
            size_bytes,
        )
    return None


def discover_tree(
    git: GitClient, commit: str, policy: DiscoveryPolicy
) -> DiscoveryResult:
    """Discover indexable files from an immutable Git tree."""
    result = DiscoveryResult()
    entries: list[TreeEntry] = git.ls_tree(commit)
    blob_entries = [entry for entry in entries if entry.type == "blob"]
    sizes = git.cat_file_batch_check([entry.sha for entry in blob_entries])

    for entry in entries:
        if entry.is_submodule:
            result.skipped.append(
                SkippedFile(entry.path, IssueKind.SKIPPED_SUBMODULE, f"submodule at {entry.sha}")
            )
            continue
        if entry.is_symlink:
            result.skipped.append(
                SkippedFile(entry.path, IssueKind.SKIPPED_SYMLINK, "symbolic link not followed")
            )
            continue
        if entry.type != "blob":
            continue
        size = sizes.get(entry.sha, 0)
        skip = _evaluate(entry.path, size, policy)
        if skip is not None:
            result.skipped.append(skip)
            continue
        if len(result.candidates) >= policy.max_files:
            result.skipped.append(
                SkippedFile(
                    entry.path,
                    IssueKind.BUDGET_EXCEEDED,
                    f"max_files={policy.max_files} reached",
                    size,
                )
            )
            result.truncated_by_budget = True
            break
        if result.total_bytes + size > policy.max_total_bytes:
            result.skipped.append(
                SkippedFile(
                    entry.path,
                    IssueKind.BUDGET_EXCEEDED,
                    f"max_total_bytes={policy.max_total_bytes} reached",
                    size,
                )
            )
            result.truncated_by_budget = True
            break
        result.candidates.append(
            Candidate(path=entry.path, size_bytes=size, mode=entry.mode, blob_sha=entry.sha)
        )
        result.total_bytes += size
    return result


def discover_worktree(
    git: GitClient, root: Path, policy: DiscoveryPolicy
) -> DiscoveryResult:
    """Discover indexable files from the (content-hashed) working tree."""
    result = DiscoveryResult()
    paths = git.ls_files_worktree()
    for path in paths:
        absolute = root / path
        try:
            stat = absolute.lstat()
        except OSError:
            result.skipped.append(
                SkippedFile(path, IssueKind.SKIPPED_UNREADABLE, "missing from working tree")
            )
            continue
        if os.path.islink(absolute):
            result.skipped.append(
                SkippedFile(path, IssueKind.SKIPPED_SYMLINK, "symbolic link not followed")
            )
            continue
        if not absolute.is_file():
            result.skipped.append(
                SkippedFile(path, IssueKind.SKIPPED_UNREADABLE, "not a regular file")
            )
            continue
        skip = _evaluate(path, stat.st_size, policy)
        if skip is not None:
            result.skipped.append(skip)
            continue
        if len(result.candidates) >= policy.max_files:
            result.skipped.append(
                SkippedFile(
                    path,
                    IssueKind.BUDGET_EXCEEDED,
                    f"max_files={policy.max_files} reached",
                    stat.st_size,
                )
            )
            result.truncated_by_budget = True
            break
        if result.total_bytes + stat.st_size > policy.max_total_bytes:
            result.skipped.append(
                SkippedFile(
                    path,
                    IssueKind.BUDGET_EXCEEDED,
                    f"max_total_bytes={policy.max_total_bytes} reached",
                    stat.st_size,
                )
            )
            result.truncated_by_budget = True
            break
        result.candidates.append(
            Candidate(path=path, size_bytes=stat.st_size, mode=_mode_for(stat.st_mode))
        )
        result.total_bytes += stat.st_size
    return result


def _mode_for(st_mode: int) -> str:
    return "100755" if st_mode & 0o111 else "100644"


__all__ = [
    "Candidate",
    "DiscoveryPolicy",
    "DiscoveryResult",
    "SkippedFile",
    "discover_tree",
    "discover_worktree",
    "inspect_content",
    "secret_filename_match",
    "SECRET_CONTENT_PATTERNS",
]

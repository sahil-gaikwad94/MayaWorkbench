"""Strict unified-diff parsing and application.

The agent edits files through a candidate patch. Using the system ``patch``
program would accept offsets, fuzz and partial application, which makes the
result non-reproducible and can silently touch the wrong lines. This module
therefore:

* parses a single unified diff with explicit file headers;
* refuses binary diffs, renames, mode changes and quoted paths;
* matches every hunk against the target content with **no fuzz**;
* computes all new file contents before writing anything, so application is
  all-or-nothing at the caller's commit point;
* reports created, modified and deleted files explicitly.

The parser is intentionally smaller than ``git apply``: it supports the subset
an agent needs, and refuses what it cannot verify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_PATCH_BYTES = 2_000_000
MAX_PATCH_FILES = 50
MAX_HUNK_LINES = 100_000

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


class PatchError(Exception):
    """A patch is malformed, unsupported, or does not apply exactly."""


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]


@dataclass
class FilePatch:
    path: str
    created: bool = False
    deleted: bool = False
    hunks: list[Hunk] = field(default_factory=list)


@dataclass(frozen=True)
class AppliedFile:
    path: str
    data: bytes
    created: bool
    deleted: bool


def _strip_timestamp(value: str) -> str:
    # Git headers are "--- a/path\t2024-..."; a space-separated timestamp is
    # also accepted. Paths containing tabs are not supported.
    return value.split("\t", 1)[0].strip()


def _header_path(value: str, *, side: str) -> tuple[str, bool]:
    raw = _strip_timestamp(value)
    if raw == "/dev/null":
        return "", True
    if raw.startswith('"') or raw.endswith('"'):
        raise PatchError("quoted diff paths are not supported")
    if raw.startswith("a/") or raw.startswith("b/"):
        raw = raw[2:]
    if not raw:
        raise PatchError(f"empty {side} path in diff header")
    if " " in raw:
        raise PatchError(f"unexpected space in {side} path: {raw!r}")
    return raw, False


def parse_patch(patch_text: str) -> list[FilePatch]:
    """Parse a unified diff into file patches, refusing unsupported features."""
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise PatchError("patch is empty")
    if len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError(f"patch exceeds {MAX_PATCH_BYTES} bytes")
    if "\x00" in patch_text:
        raise PatchError("patch contains a null byte")

    lines = patch_text.splitlines(keepends=True)
    files: list[FilePatch] = []
    current: FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            if current is not None and current.hunks:
                files.append(current)
                current = None
            i += 1
            continue
        if line.startswith("--- "):
            if current is not None and current.hunks:
                files.append(current)
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("--- header without a +++ header")
            old_path, old_null = _header_path(line[4:], side="old")
            new_path, new_null = _header_path(lines[i + 1][4:], side="new")
            if old_null and new_null:
                raise PatchError("both sides of a file header are /dev/null")
            path = new_path or old_path
            current = FilePatch(path=path, created=old_null, deleted=new_null)
            if current.created and current.deleted:
                raise PatchError("a patch cannot both create and delete a file")
            i += 2
            continue
        if line.startswith("new file mode "):
            if current is None:
                raise PatchError("file mode line outside a file patch")
            current.created = True
            i += 1
            continue
        if line.startswith("deleted file mode "):
            if current is None:
                raise PatchError("file mode line outside a file patch")
            current.deleted = True
            i += 1
            continue
        if line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            raise PatchError("renames and copies are not supported")
        if line.startswith(("old mode ", "new mode ")):
            raise PatchError("file mode changes are not supported")
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            raise PatchError("binary patches are not supported")
        if line.startswith("index ") or line.startswith("similarity index "):
            i += 1
            continue
        if line.startswith("@@"):
            if current is None:
                raise PatchError("hunk without a file header")
            match = _HUNK_RE.match(line.rstrip("\n"))
            if match is None:
                raise PatchError(f"malformed hunk header: {line.rstrip()!r}")
            old_start = int(match.group(1))
            old_len = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_len = int(match.group(4) or "1")
            i += 1
            old_lines: list[str] = []
            new_lines: list[str] = []
            while i < len(lines):
                body = lines[i]
                if body.startswith(("@@", "diff --git ", "--- ")):
                    break
                marker = body[:1]
                if marker == " ":
                    old_lines.append(body[1:])
                    new_lines.append(body[1:])
                elif marker == "-":
                    old_lines.append(body[1:])
                elif marker == "+":
                    new_lines.append(body[1:])
                elif body.startswith("\\ No newline at end of file"):
                    if old_lines:
                        old_lines[-1] = old_lines[-1].rstrip("\r\n")
                    if new_lines:
                        new_lines[-1] = new_lines[-1].rstrip("\r\n")
                else:
                    raise PatchError(f"unexpected hunk line: {body[:80]!r}")
                i += 1
            if len(old_lines) != old_len:
                raise PatchError(
                    f"hunk at -{old_start} declares {old_len} old lines, found {len(old_lines)}"
                )
            if len(new_lines) != new_len:
                raise PatchError(
                    f"hunk at +{new_start} declares {new_len} new lines, found {len(new_lines)}"
                )
            current.hunks.append(
                Hunk(
                    old_start=old_start,
                    old_len=old_len,
                    new_start=new_start,
                    new_len=new_len,
                    old_lines=tuple(old_lines),
                    new_lines=tuple(new_lines),
                )
            )
            continue
        if line.strip() == "":
            i += 1
            continue
        raise PatchError(f"unexpected diff line: {line[:80]!r}")
    if current is not None:
        files.append(current)

    if not files:
        raise PatchError("patch contains no file changes")
    if len(files) > MAX_PATCH_FILES:
        raise PatchError(f"patch touches more than {MAX_PATCH_FILES} files")
    seen: set[str] = set()
    for file_patch in files:
        if file_patch.path in seen:
            raise PatchError(f"patch touches the same file twice: {file_patch.path}")
        seen.add(file_patch.path)
        if not file_patch.hunks and not (file_patch.created or file_patch.deleted):
            raise PatchError(f"file patch has no hunks: {file_patch.path}")
        if file_patch.created and file_patch.hunks:
            first = file_patch.hunks[0]
            if first.old_len != 0:
                raise PatchError(f"new file hunk must start from /dev/null: {file_patch.path}")
        if len(file_patch.hunks) > 500:
            raise PatchError(f"too many hunks for {file_patch.path}")
    return files


def _decode(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"file is not valid UTF-8: {path}") from exc


def apply_file_patch(target: bytes, file_patch: FilePatch) -> bytes:
    """Apply one file patch exactly, refusing any offset or fuzz."""
    if file_patch.deleted and not file_patch.hunks:
        if target:
            raise PatchError(
                f"deletion patch targets a non-empty file without hunks: {file_patch.path}"
            )
        return b""
    text = _decode(target, file_patch.path)
    original = text.splitlines(keepends=True)
    if file_patch.created and original:
        raise PatchError(f"create patch targets an existing file: {file_patch.path}")

    output: list[str] = []
    position = 0
    for hunk in file_patch.hunks:
        index = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        if index < position:
            raise PatchError(f"overlapping hunks in {file_patch.path}")
        if index > len(original):
            raise PatchError(
                f"hunk starts past end of file in {file_patch.path} "
                f"(line {index + 1}, file has {len(original)})"
            )
        actual = tuple(original[index : index + hunk.old_len])
        if actual != hunk.old_lines:
            raise PatchError(
                f"hunk does not match {file_patch.path} at line {index + 1}: "
                "the patch is stale or has been modified"
            )
        output.extend(original[position:index])
        output.extend(hunk.new_lines)
        position = index + hunk.old_len
    output.extend(original[position:])
    if file_patch.deleted:
        # A deletion patch carries the removed content as context and removed
        # lines. Those hunks were verified above, so the result must be empty.
        if any(hunk.new_lines for hunk in file_patch.hunks):
            raise PatchError(f"deletion patch adds content in {file_patch.path}")
        return b""
    result = "".join(output)
    if len(result.encode("utf-8")) > 5_000_000:
        raise PatchError(f"patched file exceeds 5 MB: {file_patch.path}")
    return result.encode("utf-8")


def apply_patch(
    patch_text: str, contents: dict[str, bytes]
) -> list[AppliedFile]:
    """Apply a parsed patch against in-memory contents.

    ``contents`` must contain every existing target file. The returned list is
    complete before the caller writes anything.
    """
    files = parse_patch(patch_text)
    results: list[AppliedFile] = []
    for file_patch in files:
        if file_patch.created:
            if file_patch.path in contents:
                raise PatchError(f"file already exists: {file_patch.path}")
            target = b""
        else:
            if file_patch.path not in contents:
                raise PatchError(f"file does not exist: {file_patch.path}")
            target = contents[file_patch.path]
        data = apply_file_patch(target, file_patch)
        results.append(
            AppliedFile(
                path=file_patch.path,
                data=data,
                created=file_patch.created,
                deleted=file_patch.deleted,
            )
        )
    return results


__all__ = [
    "PatchError",
    "Hunk",
    "FilePatch",
    "AppliedFile",
    "MAX_PATCH_BYTES",
    "MAX_PATCH_FILES",
    "parse_patch",
    "apply_file_patch",
    "apply_patch",
]

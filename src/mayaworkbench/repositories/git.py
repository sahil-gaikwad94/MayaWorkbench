"""Git access layer.

Rules (§12.2, §12.3):

* every invocation uses an argv list, never a shell string;
* the environment is minimal and non-interactive;
* timeouts are enforced, and process groups are handled by the caller in later
  phases (read-only Git commands here do not spawn children that outlive us);
* paths are read from Git object storage rather than a mutable checkout
  whenever a commit-pinned snapshot is requested.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mayaworkbench.errors import GitError
from mayaworkbench.observability.logging import get_logger

log = get_logger("repositories.git")

DEFAULT_TIMEOUT = 120


def redact_remote_url(url: str | None) -> tuple[str | None, bool]:
    """Remove credentials embedded in a remote URL.

    A checkout created by CI or a credential helper often has a token in the
    remote (``https://x-access-token:ghp_...@host/org/repo.git``). Storing it
    would copy a live credential into the workbench database, the event log and
    any evidence bundle that quotes the repository. The redacted URL keeps the
    host and path so the remote remains identifiable; the caller is told that
    something was removed so it can warn the user.
    """
    if not url:
        return url, False
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, False
    if parts.username is None and parts.password is None:
        return url, False
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)), True


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    type: str
    sha: str

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    @property
    def is_submodule(self) -> bool:
        return self.mode == "160000" or self.type == "commit"

    @property
    def is_executable(self) -> bool:
        return self.mode == "100755"


@dataclass(frozen=True)
class GitResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: str
    duration_ms: int

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def stripped(self) -> str:
        return self.stdout_text.strip()


class GitClient:
    """Thin, typed wrapper around the ``git`` CLI."""

    def __init__(self, workdir: str | os.PathLike[str], *, timeout: int = DEFAULT_TIMEOUT):
        self.workdir = Path(workdir).expanduser().resolve()
        self.timeout = timeout

    # ------------------------------------------------------------------ runner
    def _env(self) -> dict[str, str]:
        keep = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SYSTEMROOT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
        }
        env = {key: value for key, value in os.environ.items() if key in keep}
        env.update(
            {
                "LC_ALL": "C",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "GIT_ASKPASS": "",
                "SSH_ASKPASS": "",
            }
        )
        return env

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: int | None = None,
        input_bytes: bytes | None = None,
    ) -> GitResult:
        # Neutralise a repository-controlled fsmonitor hook; external diff and
        # textconv drivers are disabled at the call site with --no-ext-diff and
        # --no-textconv (an empty diff.external would make Git try to exec "").
        argv = (
            "git",
            "-C",
            str(self.workdir),
            "-c",
            "core.fsmonitor=false",
            *args,
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
                env=self._env(),
                shell=False,
                cwd=str(self.workdir),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git {' '.join(args)} timed out after {timeout or self.timeout}s",
                argv=list(argv),
                hint="Large repositories may need a longer MAYA_GIT_TIMEOUT.",
            ) from exc
        except FileNotFoundError as exc:
            raise GitError(
                "The git executable was not found",
                argv=list(argv),
                hint="Install Git and ensure it is on PATH.",
            ) from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        result = GitResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr.decode("utf-8", errors="replace").strip(),
            duration_ms=duration_ms,
        )
        if check and result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr[:800]}",
                argv=list(argv),
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result

    # -------------------------------------------------------------- repository
    def version(self) -> str:
        return self.run("--version").stripped()

    def resolve_root(self) -> Path:
        result = self.run("rev-parse", "--show-toplevel", check=False)
        if result.returncode != 0:
            raise GitError(
                f"{self.workdir} is not inside a Git repository",
                stderr=result.stderr,
                hint="Attach a path that contains a .git directory.",
            )
        return Path(result.stripped()).resolve()

    def is_inside_work_tree(self) -> bool:
        result = self.run("rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and result.stripped() == "true"

    def head_commit(self) -> str | None:
        result = self.run("rev-parse", "--verify", "HEAD", check=False)
        return result.stripped() if result.returncode == 0 else None

    def tree_sha(self, commit: str) -> str:
        return self.run("rev-parse", "--verify", f"{commit}^{{tree}}").stripped()

    def commit_metadata(self, commit: str) -> dict[str, str]:
        """Return committer date, author date, subject, and parents."""
        fmt = "%H%x00%cI%x00%aI%x00%s%x00%P"
        result = self.run("show", "-s", f"--format={fmt}", commit)
        parts = result.stdout_text.split("\x00")
        if len(parts) < 5:
            raise GitError(f"Unexpected git show output for {commit}")
        return {
            "commit": parts[0].strip(),
            "committed_at": parts[1].strip(),
            "authored_at": parts[2].strip(),
            "subject": parts[3].strip(),
            "parents": parts[4].strip(),
        }

    def current_branch(self) -> str:
        result = self.run("rev-parse", "--abbrev-ref", "HEAD", check=False)
        return result.stripped() or "HEAD"

    def remote_url(self) -> str | None:
        result = self.run("config", "--get", "remote.origin.url", check=False)
        return result.stripped() or None

    def default_branch(self) -> str | None:
        result = self.run("symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        if result.returncode == 0 and result.stripped():
            return result.stripped().split("/", 1)[-1]
        branch = self.current_branch()
        return None if branch == "HEAD" else branch

    def is_dirty(self) -> bool:
        return bool(self.status_porcelain())

    def status_porcelain(self) -> str:
        result = self.run("status", "--porcelain=v1", "--untracked-files=all", check=False)
        if result.returncode != 0:
            raise GitError("git status failed", stderr=result.stderr, returncode=result.returncode)
        return result.stdout_text

    # ------------------------------------------------------------------ trees
    def ls_tree(self, commit: str) -> list[TreeEntry]:
        result = self.run("ls-tree", "-r", "-z", "--full-tree", commit)
        entries: list[TreeEntry] = []
        for record in result.stdout.split(b"\x00"):
            if not record:
                continue
            header, _, path_bytes = record.partition(b"\t")
            meta = header.decode("utf-8", errors="replace").split(" ")
            if len(meta) != 3:
                raise GitError(f"Unexpected git ls-tree record: {record!r}")
            entries.append(
                TreeEntry(
                    path=path_bytes.decode("utf-8", errors="surrogateescape"),
                    mode=meta[0],
                    type=meta[1],
                    sha=meta[2],
                )
            )
        entries.sort(key=lambda entry: entry.path)
        return entries

    def submodules(self, commit: str) -> dict[str, str]:
        return {
            entry.path: entry.sha
            for entry in self.ls_tree(commit)
            if entry.is_submodule
        }

    # ------------------------------------------------------------------- blobs
    def ls_files_worktree(self) -> list[str]:
        """Tracked plus untracked-but-not-ignored files, honouring .gitignore."""
        result = self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        paths = {
            path.decode("utf-8", errors="surrogateescape")
            for path in result.stdout.split(b"\x00")
            if path
        }
        return sorted(paths)

    def untracked_files(self) -> list[str]:
        result = self.run("ls-files", "-z", "--others", "--exclude-standard")
        return sorted(
            path.decode("utf-8", errors="surrogateescape")
            for path in result.stdout.split(b"\x00")
            if path
        )

    def diff_name_only(self, commit: str) -> list[str]:
        result = self.run("diff", "--name-only", "-z", "--no-color", commit, "--")
        return sorted(
            path.decode("utf-8", errors="surrogateescape")
            for path in result.stdout.split(b"\x00")
            if path
        )

    def diff_binary(self, commit: str) -> bytes:
        return self.run(
            "diff",
            "--binary",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            commit,
            "--",
        ).stdout

    def cat_file_batch(self, shas: list[str]) -> dict[str, tuple[str, bytes]]:
        """Read many objects in one process, preserving binary content."""
        if not shas:
            return {}
        payload = ("\n".join(shas) + "\n").encode("ascii")
        result = self.run("cat-file", "--batch", input_bytes=payload)
        data = result.stdout
        objects: dict[str, tuple[str, bytes]] = {}
        position = 0
        total = len(data)
        while position < total:
            newline = data.find(b"\n", position)
            if newline < 0:
                raise GitError("Truncated git cat-file --batch header")
            header = data[position:newline].decode("utf-8", errors="replace")
            position = newline + 1
            parts = header.split(" ")
            if len(parts) == 2 and parts[1] == "missing":
                raise GitError(f"Git object {parts[0]} is missing from the repository")
            if len(parts) != 3:
                raise GitError(f"Unexpected git cat-file header: {header!r}")
            oid, object_type, size_text = parts
            size = int(size_text)
            content = data[position : position + size]
            position += size + 1  # trailing LF after the object body
            objects[oid] = (object_type, content)
        return objects

    def cat_file_batch_check(self, shas: list[str]) -> dict[str, int]:
        """Return object sizes (bytes) for the given object ids."""
        if not shas:
            return {}
        payload = ("\n".join(shas) + "\n").encode("ascii")
        result = self.run("cat-file", "--batch-check", input_bytes=payload)
        sizes: dict[str, int] = {}
        for line in result.stdout_text.splitlines():
            parts = line.split(" ")
            if len(parts) == 3:
                sizes[parts[0]] = int(parts[2])
        return sizes

    # ------------------------------------------------------------------ static
    @staticmethod
    def looks_like_git_repository(path: Path) -> bool:
        if not path.is_dir():
            return False
        if (path / ".git").exists():
            return True
        try:
            return GitClient(path).run(
                "rev-parse", "--is-inside-work-tree", check=False
            ).stripped() == "true"
        except GitError:
            return False


__all__ = ["GitClient", "GitResult", "TreeEntry"]

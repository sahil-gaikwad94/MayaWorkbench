"""Typed errors with stable machine-readable codes.

The CLI (and later the HTTP API) maps these to exit codes / HTTP statuses.
Never swallow an error: every failure must be visible in the output and in the
workflow event log.
"""

from __future__ import annotations


class MayaError(Exception):
    """Base class for all expected MayaWorkbench failures."""

    code = "MAYA_ERROR"
    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail

    def __str__(self) -> str:
        return self.message


class UsageError(MayaError):
    """The user asked for something that is not valid input."""

    code = "USAGE_ERROR"
    exit_code = 2


class ConfigError(MayaError):
    """Configuration or environment is unusable."""

    code = "CONFIG_ERROR"


class NotFoundError(MayaError):
    """A requested entity does not exist."""

    code = "NOT_FOUND"


class ConflictError(MayaError):
    """The requested operation conflicts with existing state."""

    code = "CONFLICT"


class DatabaseError(MayaError):
    """Persistence failed or the database is inconsistent."""

    code = "DATABASE_ERROR"


class RepositoryError(MayaError):
    """The repository is missing, not a Git repository, or unreadable."""

    code = "REPOSITORY_ERROR"


class GitError(RepositoryError):
    """A Git invocation failed."""

    code = "GIT_ERROR"

    def __init__(
        self,
        message: str,
        *,
        argv: list[str] | None = None,
        returncode: int | None = None,
        stderr: str | None = None,
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint)
        self.argv = argv or []
        self.returncode = returncode
        self.stderr = stderr


class SnapshotError(MayaError):
    """A snapshot could not be created or read consistently."""

    code = "SNAPSHOT_ERROR"


class IndexingError(MayaError):
    """Repository indexing failed."""

    code = "INDEXING_ERROR"


class PolicyError(MayaError):
    """A policy (data-sharing, budget, path scope) refused the operation."""

    code = "POLICY_ERROR"
    exit_code = 3


class ModelError(MayaError):
    """A model provider call failed."""

    code = "MODEL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "unknown",
        retryable: bool = False,
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint)
        self.error_class = error_class
        self.retryable = retryable


class CitationError(MayaError):
    """A cited source location is not valid for the pinned snapshot."""

    code = "CITATION_ERROR"


__all__ = [
    "MayaError",
    "UsageError",
    "ConfigError",
    "NotFoundError",
    "ConflictError",
    "DatabaseError",
    "RepositoryError",
    "GitError",
    "SnapshotError",
    "IndexingError",
    "PolicyError",
    "ModelError",
    "CitationError",
]

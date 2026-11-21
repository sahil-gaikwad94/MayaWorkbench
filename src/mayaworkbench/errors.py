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


class RuntimeUnavailable(MayaError):
    """The requested isolated execution runtime cannot be established.

    This is fail-closed: a caller must not silently downgrade to host-privileged
    execution when an isolated profile was requested.
    """

    code = "RUNTIME_UNAVAILABLE"
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


class ExtractionError(MayaError):
    """A research document could not be converted into citable passages."""

    code = "EXTRACTION_ERROR"


class FetchError(MayaError):
    """A controlled document fetch was refused or failed."""

    code = "FETCH_ERROR"


class TaskError(MayaError):
    """A task, workflow or plan operation was refused."""

    code = "TASK_ERROR"


class ApprovalError(MayaError):
    """An approval is missing, expired, mismatched or already used."""

    code = "APPROVAL_ERROR"
    exit_code = 3


class WorkspaceError(MayaError):
    """An isolated worktree could not be created, verified or removed."""

    code = "WORKSPACE_ERROR"


class ToolError(MayaError):
    """A tool invocation was refused or failed."""

    code = "TOOL_ERROR"
    exit_code = 3


class VerificationError(MayaError):
    """Verification could not produce evidence for the requested run."""

    code = "VERIFICATION_ERROR"


class ExperimentError(MayaError):
    """An experiment definition, run, manifest or comparison was refused."""

    code = "EXPERIMENT_ERROR"


class ProjectMemoryError(MayaError):
    """Durable project memory could not be recorded or changed."""

    code = "MEMORY_ERROR"


class BudgetError(MayaError):
    """A task budget (time, tokens, cost, steps or output) was exhausted."""

    code = "BUDGET_ERROR"
    exit_code = 3


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
    "RuntimeUnavailable",
    "ModelError",
    "CitationError",
    "ExtractionError",
    "FetchError",
    "TaskError",
    "ApprovalError",
    "WorkspaceError",
    "ToolError",
    "ExperimentError",
    "ProjectMemoryError",
    "BudgetError",
]

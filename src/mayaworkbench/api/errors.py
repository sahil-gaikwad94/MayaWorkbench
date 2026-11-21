"""Typed JSON errors for the local control plane.

Every failure the HTTP layer emits is a small object with three keys:
``error`` (human message), ``code`` (stable machine code) and ``hint`` (optional
remediation).  Expected :class:`MayaError` failures are mapped to the HTTP
status that matches their meaning; unexpected exceptions never leave this
module as a raw stack trace or as a leaked secret.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.errors import (
    ApprovalError,
    BudgetError,
    ConflictError,
    ConfigError,
    DatabaseError,
    ExperimentError,
    MayaError,
    NotFoundError,
    PolicyError,
    ProjectMemoryError,
    RuntimeUnavailable,
    TaskError,
    ToolError,
    UsageError,
    VerificationError,
    WorkspaceError,
)

_POLICY_ERRORS = (
    PolicyError,
    ApprovalError,
    BudgetError,
    ConflictError,
    RuntimeUnavailable,
    ToolError,
    TaskError,
    WorkspaceError,
    VerificationError,
    ExperimentError,
    ProjectMemoryError,
)

_SERVER_ERRORS = (ConfigError, DatabaseError)


class ApiError(Exception):
    """An error raised at the HTTP boundary (validation, routing, limits)."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, "hint": self.hint}


def map_maya_error(exc: MayaError) -> ApiError:
    """Translate an application error into a stable HTTP error.

    - 400 — bad or missing input (usage/validation);
    - 403 — a policy, approval, budget or workflow-state refusal;
    - 404 — a requested entity does not exist;
    - 500 — the local database/configuration is unusable.
    """

    hint = exc.hint
    if isinstance(exc, NotFoundError):
        return ApiError(404, exc.code, str(exc), hint=hint)
    if isinstance(exc, UsageError):
        return ApiError(400, exc.code, str(exc), hint=hint)
    if isinstance(exc, _SERVER_ERRORS):
        return ApiError(500, exc.code, str(exc), hint=hint)
    if isinstance(exc, _POLICY_ERRORS):
        return ApiError(403, exc.code, str(exc), hint=hint)
    # Other expected failures (repository/index/model/etc.) are reported as a
    # clean client-visible error with the application's own stable code.
    return ApiError(400, exc.code, str(exc), hint=hint)


def internal_error() -> dict[str, Any]:
    """A deliberately opaque payload for unhandled exceptions."""

    return {
        "error": "Internal server error",
        "code": "INTERNAL",
        "hint": "Check the local server log; no details are exposed over HTTP.",
    }


__all__ = ["ApiError", "map_maya_error", "internal_error"]

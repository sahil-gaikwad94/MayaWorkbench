"""Formal constraint adapters: opt-in, honest, and never a pass by default.

A :class:`ConstraintVerifier` is a plugin that *may* prove or disprove a declared
constraint about a target. This module deliberately ships with no such plugin and
no solver dependency: a constraint is :attr:`ConstraintStatus.NOT_CHECKED` until a
caller registers a verifier that supports it. There is no ``PASSED`` status — a
formal result is either ``VERIFIED``, ``VIOLATED``, or ``NOT_CHECKED`` — so an
absent, unsupported, or broken verifier can never be read as a pass.

Verifiers are ordinary callables supplied by the caller (for example an adapter
around a separately installed solver). Nothing here claims to perform a proof,
and no model output is consulted.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ConstraintStatus",
    "ConstraintResult",
    "ConstraintVerifier",
    "ConstraintRegistry",
]


class ConstraintStatus(str, Enum):
    """The only outcomes a constraint check may produce.

    Notably there is no ``PASSED``: absence of a verifier is not evidence.
    """

    VERIFIED = "VERIFIED"
    VIOLATED = "VIOLATED"
    NOT_CHECKED = "NOT_CHECKED"


class ConstraintResult(BaseModel):
    """A single, immutable constraint verdict with bounded supporting references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    constraint_id: str = Field(min_length=1, max_length=300)
    status: ConstraintStatus
    reason: str = ""
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)


@runtime_checkable
class ConstraintVerifier(Protocol):
    """Plugin contract for an adapter that can check named constraints.

    ``context`` carries deterministic, caller-supplied facts (paths, hashes,
    declared bounds); it is never populated from model output by this module.
    """

    @property
    def name(self) -> str:
        """Stable, human-readable identifier for this verifier."""
        ...

    def supports(self, constraint_id: str) -> bool:
        """Return ``True`` only for constraint ids this verifier can check."""
        ...

    def verify(
        self,
        constraint_id: str,
        *,
        target: Any,
        context: Mapping[str, Any] | None = None,
    ) -> ConstraintResult:
        """Return a verdict for ``constraint_id`` about ``target``."""
        ...


class ConstraintRegistry:
    """Ordered collection of verifiers with fail-closed default behaviour."""

    def __init__(self) -> None:
        self._verifiers: list[ConstraintVerifier] = []

    # ------------------------------------------------------------------ registration
    def register(self, verifier: ConstraintVerifier) -> ConstraintVerifier:
        """Register a verifier. Returns it so this can be used as a decorator.

        Rejecting malformed plugins at registration time keeps ``verify`` simple
        and prevents a broken plugin from silently influencing a decision.
        """
        if not isinstance(verifier, ConstraintVerifier):
            raise TypeError("verifier must implement the ConstraintVerifier protocol")
        name = verifier.name
        if not isinstance(name, str) or not name:
            raise ValueError("verifier.name must be a non-empty string")
        if any(existing.name == name for existing in self._verifiers):
            raise ValueError(f"a verifier named {name!r} is already registered")
        self._verifiers.append(verifier)
        return verifier

    # ------------------------------------------------------------------- verification
    def verify(
        self,
        constraint_id: str,
        *,
        target: Any,
        context: Mapping[str, Any] | None = None,
    ) -> ConstraintResult:
        """Check one constraint; never raise and never default to a pass."""
        for verifier in list(self._verifiers):
            try:
                supported = verifier.supports(constraint_id)
            except Exception:  # noqa: BLE001 - a broken plugin cannot change a decision
                continue
            if supported is not True:
                continue
            try:
                result = verifier.verify(constraint_id, target=target, context=context)
            except Exception as exc:  # noqa: BLE001 - fail closed, never pass
                try:
                    label = repr(verifier.name)
                except Exception:  # noqa: BLE001 - even a broken name cannot escape
                    label = "<unnamed>"
                return ConstraintResult(
                    constraint_id=constraint_id,
                    status=ConstraintStatus.NOT_CHECKED,
                    reason=f"verifier {label} raised {type(exc).__name__}: {exc}",
                )
            return self._normalise(constraint_id, result)
        return ConstraintResult(
            constraint_id=constraint_id,
            status=ConstraintStatus.NOT_CHECKED,
            reason=(
                "no formal verifier registered"
                if not self._verifiers
                else f"no verifier supports constraint: {constraint_id}"
            ),
        )

    def verify_all(
        self,
        constraint_ids: Iterable[str],
        *,
        target: Any,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, ConstraintResult]:
        """Verify every declared id, preserving order and duplicates safely."""
        results: dict[str, ConstraintResult] = {}
        for constraint_id in constraint_ids:
            results[constraint_id] = self.verify(
                constraint_id, target=target, context=context
            )
        return results

    # -------------------------------------------------------------------- introspection
    def describe(self) -> list[dict[str, Any]]:
        """Machine-readable description of the registered verifiers."""
        descriptions: list[dict[str, Any]] = []
        for verifier in self._verifiers:
            supported: Any = None
            declared = getattr(verifier, "supported_constraints", None)
            if isinstance(declared, (list, tuple, set, frozenset)):
                supported = sorted(str(item) for item in declared)
            descriptions.append({"name": verifier.name, "supports": supported})
        return descriptions

    # ------------------------------------------------------------------------- helpers
    @staticmethod
    def _normalise(constraint_id: str, result: Any) -> ConstraintResult:
        """Coerce a plugin result without ever promoting it to a stronger verdict."""
        if not isinstance(result, ConstraintResult):
            return ConstraintResult(
                constraint_id=constraint_id,
                status=ConstraintStatus.NOT_CHECKED,
                reason="verifier returned an invalid result type",
            )
        if result.constraint_id != constraint_id:
            return ConstraintResult(
                constraint_id=constraint_id,
                status=ConstraintStatus.NOT_CHECKED,
                reason=(
                    "verifier returned a result for "
                    f"{result.constraint_id!r}, expected {constraint_id!r}"
                ),
            )
        return result

"""Constraint adapters are opt-in, fail-closed, and never a pass by default."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mayaworkbench.verification.constraints import (
    ConstraintRegistry,
    ConstraintResult,
    ConstraintStatus,
    ConstraintVerifier,
)


class _Verifier:
    def __init__(self, name: str, supported: tuple[str, ...]) -> None:
        self.name = name
        self.supported = supported

    @property
    def supported_constraints(self) -> tuple[str, ...]:
        return self.supported

    def supports(self, constraint_id: str) -> bool:
        return constraint_id in self.supported

    def verify(self, constraint_id, *, target, context=None):
        raise AssertionError("base stub has no verdict")


class _VerdictVerifier(_Verifier):
    def __init__(self, verdicts: dict[str, ConstraintStatus]) -> None:
        super().__init__("verdicts", tuple(verdicts))
        self.verdicts = verdicts
        self.calls: list[tuple[str, object, object]] = []

    def verify(self, constraint_id, *, target, context=None):
        self.calls.append((constraint_id, target, context))
        return ConstraintResult(
            constraint_id=constraint_id,
            status=self.verdicts[constraint_id],
            reason="checked by stub",
            evidence_refs=("stub:evidence/1",),
        )


def test_constraint_result_is_frozen_strict_and_bounded() -> None:
    result = ConstraintResult(
        constraint_id="C1",
        status=ConstraintStatus.VERIFIED,
        reason="proved",
        evidence_refs=("a", "b"),
    )
    assert result.status is ConstraintStatus.VERIFIED
    assert result.evidence_refs == ("a", "b")
    with pytest.raises(Exception):
        result.status = ConstraintStatus.VIOLATED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ConstraintResult(constraint_id="C1", status="PASSED")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ConstraintResult(constraint_id="C1", status=ConstraintStatus.VERIFIED, extra=1)


def test_protocol_is_runtime_checkable() -> None:
    assert isinstance(_VerdictVerifier({}), ConstraintVerifier)

    class _Incomplete:
        name = "broken"

        def supports(self, constraint_id: str) -> bool:
            return True

    assert not isinstance(_Incomplete(), ConstraintVerifier)


def test_empty_registry_is_not_checked_never_passed() -> None:
    registry = ConstraintRegistry()
    results = registry.verify_all(["C1", "C2"], target=object())
    assert set(results) == {"C1", "C2"}
    for result in results.values():
        assert result.status is ConstraintStatus.NOT_CHECKED
        assert result.reason == "no formal verifier registered"
        assert result.evidence_refs == ()
    assert "PASSED" not in {status.value for status in ConstraintStatus}
    assert registry.describe() == []


def test_registration_and_verdicts() -> None:
    registry = ConstraintRegistry()
    verifier = _VerdictVerifier(
        {"C1": ConstraintStatus.VERIFIED, "C2": ConstraintStatus.VIOLATED}
    )
    assert registry.register(verifier) is verifier
    assert registry.describe() == [
        {"name": "verdicts", "supports": ["C1", "C2"]}
    ]

    target = object()
    context = {"seed": 0}
    results = registry.verify_all(["C1", "C2"], target=target, context=context)
    assert results["C1"].status is ConstraintStatus.VERIFIED
    assert results["C2"].status is ConstraintStatus.VIOLATED
    assert results["C1"].evidence_refs == ("stub:evidence/1",)
    assert verifier.calls == [
        ("C1", target, context),
        ("C2", target, context),
    ]


def test_unsupported_constraint_stays_not_checked() -> None:
    registry = ConstraintRegistry()
    registry.register(_VerdictVerifier({"C1": ConstraintStatus.VERIFIED}))
    result = registry.verify("C9", target=object())
    assert result.status is ConstraintStatus.NOT_CHECKED
    assert "no verifier supports constraint" in result.reason


def test_duplicate_names_are_rejected() -> None:
    registry = ConstraintRegistry()
    registry.register(_VerdictVerifier({"C1": ConstraintStatus.VERIFIED}))
    other = _VerdictVerifier({"C2": ConstraintStatus.VERIFIED})
    other.name = "verdicts"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(other)
    with pytest.raises(TypeError):
        registry.register(object())  # type: ignore[arg-type]


def test_broken_verifier_never_changes_a_decision() -> None:
    class _Raises(_Verifier):
        def __init__(self) -> None:
            super().__init__("raises", ("C1",))

        def verify(self, constraint_id, *, target, context=None):
            raise RuntimeError("solver exploded")

    registry = ConstraintRegistry()
    registry.register(_Raises())
    result = registry.verify("C1", target=object())
    assert result.status is ConstraintStatus.NOT_CHECKED
    assert "raised RuntimeError" in result.reason

    # A verifier whose supports() fails is skipped, not trusted.
    class _BadSupports(_Verifier):
        def __init__(self) -> None:
            super().__init__("bad_supports", ("C1",))

        def supports(self, constraint_id: str) -> bool:
            raise RuntimeError("cannot tell")

    registry2 = ConstraintRegistry()
    registry2.register(_BadSupports())
    result2 = registry2.verify("C1", target=object())
    assert result2.status is ConstraintStatus.NOT_CHECKED


def test_invalid_plugin_results_are_downgraded() -> None:
    class _WrongId(_Verifier):
        def __init__(self) -> None:
            super().__init__("wrong_id", ("C1",))

        def verify(self, constraint_id, *, target, context=None):
            return ConstraintResult(
                constraint_id="C2", status=ConstraintStatus.VERIFIED
            )

    class _WrongType(_Verifier):
        def __init__(self) -> None:
            super().__init__("wrong_type", ("C1",))

        def verify(self, constraint_id, *, target, context=None):
            return {"status": "VERIFIED"}

    for plugin in (_WrongId(), _WrongType()):
        registry = ConstraintRegistry()
        registry.register(plugin)
        result = registry.verify("C1", target=object())
        assert result.status is ConstraintStatus.NOT_CHECKED
        assert result.constraint_id == "C1"

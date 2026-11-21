"""Deterministic skill execution: render procedures and run static check templates.

A skill is not an authority and does not execute arbitrary shell code. Procedures
are rendered into an execution plan; checkers run only the bounded, built-in static
checks named in the skill body. This keeps the skills system honest and testable.
"""

from __future__ import annotations

import time
from typing import Any

from mayaworkbench.skills.models import Skill, SkillResult

_FORBIDDEN_MARKERS = ("rm -rf", "sudo", "curl ", "wget ", "eval(", "os.system", "subprocess")


def execute_skill(skill: Skill, **kwargs: Any) -> SkillResult:
    """Execute a skill in the bounded, deterministic way its category allows."""
    started = time.monotonic()
    validation: list[str] = []
    if not skill.enabled:
        return SkillResult(
            skill_id=skill.id, status="SKIPPED", output="Skill is disabled",
            validation=["disabled"],
        )
    if not skill.body.strip():
        return SkillResult(
            skill_id=skill.id, status="FAILED", output="Skill has no body",
            validation=["empty body"],
        )
    lowered = skill.body.lower()
    for marker in _FORBIDDEN_MARKERS:
        if marker in lowered:
            validation.append(f"forbidden external action marker: {marker.strip()}")
    if validation:
        return SkillResult(
            skill_id=skill.id,
            status="FAILED",
            output="Skill body requests an action the executor does not permit",
            validation=validation,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    validation.append("body contains no forbidden external action markers")
    if skill.category == "checker":
        findings = []
        if "assert" not in lowered and "check:" not in lowered:
            findings.append("checker template has no assertion or declared check")
        if len(skill.body) > 100_000:
            findings.append("checker template is too large")
        validation.extend(findings)
        status = "PROCESSED" if not findings else "FAILED"
        output = "checker template validated; no repository code was executed"
    else:
        context = "\n".join(f"{key}: {value}" for key, value in sorted(kwargs.items()))
        output = f"# Execution plan: {skill.title or skill.name}\n{skill.body}\n"
        if context:
            output += f"\n# Supplied context\n{context}\n"
        validation.append("procedure rendered; no side effects performed")
        status = "PROCESSED"
    return SkillResult(
        skill_id=skill.id,
        status=status,
        output=output,
        validation=validation,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


__all__ = ["execute_skill"]

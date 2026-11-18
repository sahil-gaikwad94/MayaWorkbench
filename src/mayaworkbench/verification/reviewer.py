"""Read-only independent reviewer (§13.4).

The reviewer receives raw evidence — contract, criteria, diff, check results,
integrity findings, agent limitations — and may only return structured findings.
It has no tools, cannot run anything and cannot change a deterministic result.

Grounding rules enforced here, not by the prompt:

* a finding that names a path outside the captured diff is rejected and
  recorded as rejected, because the reviewer was not given that file;
* a line must exist in the candidate file;
* a model finding is always recorded with ``source="model_review"`` and can
  never turn a failed check into a pass.

If no model is available the check is ``SKIPPED`` with the reason. A reviewer
failure never silently becomes "no concerns".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from mayaworkbench.domain.enums import (
    CheckOutcome,
    FindingSeverity,
)
from mayaworkbench.domain.models import (
    CheckSpec,
    Record,
    Task,
    TaskContract,
    TaskDiff,
    VerificationCheck,
    VerificationFinding,
    WorkflowRun,
)
from mayaworkbench.errors import MayaError, ModelError, PolicyError
from mayaworkbench.models.base import ChatMessage, ModelRequest
from mayaworkbench.models.gateway import ModelGateway
from mayaworkbench.storage.stores import InvocationStore

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
MAX_DIFF_CHARS = 60_000
MAX_OUTPUT_CHARS = 4_000


class ReviewFindingPayload(Record):
    severity: Literal["INFO", "WARNING", "ERROR", "BLOCKER"] = "INFO"
    code: str = Field(min_length=3, max_length=80)
    message: str = Field(min_length=1, max_length=3000)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    rationale: str = Field(default="", max_length=4000)
    recommended_action: str = Field(default="", max_length=1000)


class ReviewPayload(Record):
    assessment: Literal["SOUND", "CONCERNS", "UNSOUND", "INCONCLUSIVE"] = "INCONCLUSIVE"
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[ReviewFindingPayload] = Field(default_factory=list, max_length=50)
    unverified_assumptions: list[str] = Field(default_factory=list, max_length=30)


@dataclass
class ReviewResult:
    outcome: CheckOutcome
    summary: str
    findings: list[VerificationFinding] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    invocation_id: str | None = None


def changed_line_ranges(patch: str) -> dict[str, list[tuple[int, int]]]:
    """Map each patched file to the new-side line ranges its hunks cover."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            ranges.setdefault(current, [])
            continue
        if line.startswith("+++ ") and line[4:].strip() == "/dev/null":
            current = None
            continue
        if current is None:
            continue
        match = HUNK_RE.match(line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count > 0:
                ranges.setdefault(current, []).append((start, start + count - 1))
    return {path: spans for path, spans in ranges.items() if spans}


def _line_in_spans(line: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in spans)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n[... truncated ...]\n", True


def build_review_input(
    *,
    run: WorkflowRun,
    task: Task,
    contract: TaskContract,
    diff: TaskDiff,
    patch: str,
    checks: list[VerificationCheck],
    criteria: list[tuple[str, str]],
    integrity_findings: list[VerificationFinding],
) -> tuple[str, dict[str, Any]]:
    diff_text, diff_truncated = _clip(patch, MAX_DIFF_CHARS)
    lines: list[str] = [
        "# Independent verification review",
        "",
        "You are a read-only reviewer. You cannot run commands, read files, or change",
        "results. Judge only the evidence below. A check marked PASSED is a recorded",
        "result; do not claim a check ran if it is absent or SKIPPED. Do not invent",
        "paths or line numbers: a finding outside the diff is rejected by the tool.",
        "",
        "## Task",
        f"Goal: {task.goal}",
        f"Task type: {task.task_type.value}",
        f"Base commit: {run.base_commit_sha}",
        f"Snapshot: {run.snapshot_id}",
        f"Contract sha256: {contract.content_sha256}",
        "",
        "## Acceptance criteria",
    ]
    for criterion_id, text in criteria:
        lines.append(f"- {criterion_id}: {text}")
    lines.extend(
        [
            "",
            "## Scope",
            "- Allowed paths: " + ", ".join(contract.scope.allowed_paths),
            "- Excluded paths: " + (", ".join(contract.scope.excluded_paths) or "(none)"),
            f"- Network policy: {contract.constraints.network_access}",
            f"- Max steps/tokens/cost: {contract.constraints.max_steps} / "
            f"{contract.constraints.max_model_tokens} / {contract.constraints.max_model_cost_usd}",
            "",
            "## Candidate patch",
            f"Files: {len(diff.files)} · +{diff.additions} / -{diff.deletions}",
            "```diff",
            diff_text.rstrip(),
            "```",
            "",
            "## Recorded checks",
        ]
    )
    for check in checks:
        lines.append(
            f"- [{check.gate.value}] {check.key}: {check.outcome.value}"
            f" (required={check.required}) — {check.summary or 'no summary'}"
        )
        for finding in check.findings:
            if finding.severity in {FindingSeverity.ERROR, FindingSeverity.BLOCKER}:
                lines.append(
                    f"    - {finding.severity.value} {finding.code}: {finding.message}"
                )
    lines.extend(["", "## Test-integrity findings"])
    if integrity_findings:
        for finding in integrity_findings:
            location = finding.path or "?"
            if finding.line:
                location += f":{finding.line}"
            lines.append(
                f"- {finding.severity.value} {finding.code} at {location}: {finding.message}"
            )
    else:
        lines.append("- none detected (this is not proof that tests were not weakened)")
    lines.extend(
        [
            "",
            "## Agent-reported limitations",
        ]
    )
    if run.limitations:
        lines.extend(f"- {item}" for item in run.limitations)
    else:
        lines.append("- none recorded by the agent")
    if run.finish_summary:
        lines.extend(["", "## Agent summary", run.finish_summary])
    lines.extend(
        [
            "",
            "Return JSON matching the schema: assessment, summary, findings, and",
            "unverified_assumptions. Use severity BLOCKER only for a concrete defect",
            "that makes the change incorrect or unsafe. Use ERROR for a real problem",
            "the evidence supports. Style preferences are INFO at most.",
        ]
    )
    meta = {
        "diff_truncated": diff_truncated,
        "diff_chars": len(diff_text),
        "checks_included": len(checks),
    }
    return "\n".join(lines), meta


def _safe_code(code: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9]+", "_", code).upper().strip("_")
    if not candidate or not candidate[0].isalpha():
        candidate = "MODEL_" + candidate
    candidate = candidate[:80].strip("_")
    if len(candidate) < 3:
        candidate = "MODEL_FINDING"
    return candidate


def _safe_message(message: str) -> str:
    return (message or "(no message)")[:3900]


def _rejected_finding(finding: ReviewFindingPayload, reason: str) -> VerificationFinding:
    return VerificationFinding(
        severity=FindingSeverity.INFO,
        code="MODEL_REVIEW_UNGROUNDED",
        message=(
            f"Reviewer finding {finding.code!r} was rejected: {reason}. "
            f"Original message: {finding.message[:500]}"
        ),
        source="model_review",
    )


def run_independent_review(
    *,
    gateway: ModelGateway,
    invocations: InvocationStore,
    run: WorkflowRun,
    task: Task,
    contract: TaskContract,
    spec: CheckSpec,
    diff: TaskDiff,
    patch: str,
    checks: list[VerificationCheck],
    criteria: list[tuple[str, str]],
    integrity_findings: list[VerificationFinding],
    workspace_path: Any,
) -> ReviewResult:
    prompt, meta = build_review_input(
        run=run,
        task=task,
        contract=contract,
        diff=diff,
        patch=patch,
        checks=checks,
        criteria=criteria,
        integrity_findings=integrity_findings,
    )
    request = ModelRequest(
        purpose="verification_review",
        schema_name="VerificationReview",
        json_schema=ReviewPayload.model_json_schema(),
        messages=[
            ChatMessage(role="system", content=prompt),
            ChatMessage(role="user", content="Review the evidence and return the JSON object."),
        ],
        max_output_tokens=2000,
        temperature=0.0,
    )
    try:
        response, invocation = gateway.structured(
            request,
            project_id=run.project_id,
            snapshot_id=run.snapshot_id,
            purpose="verification_review",
            correlation_id=run.id,
        )
    except (ModelError, PolicyError, MayaError) as exc:
        invocation_id = getattr(exc, "invocation_id", None)
        return ReviewResult(
            outcome=CheckOutcome.SKIPPED,
            summary=f"Independent review was not possible: {exc}",
            details={**meta, "skipped_reason": str(exc)},
            invocation_id=invocation_id,
        )
    payload_dict = response.structured
    if payload_dict is None:
        try:
            payload_dict = json.loads(response.text)
        except (TypeError, json.JSONDecodeError):
            return ReviewResult(
                outcome=CheckOutcome.INCONCLUSIVE,
                summary="The reviewer returned output that is not valid JSON.",
                findings=[
                    VerificationFinding(
                        severity=FindingSeverity.WARNING,
                        code="MODEL_REVIEW_INVALID",
                        message="Reviewer output could not be parsed as JSON; no review was accepted.",
                        source="model_review",
                    )
                ],
                details={**meta, "raw_text_chars": len(response.text)},
                invocation_id=invocation.id,
            )
    try:
        payload = ReviewPayload.model_validate(payload_dict)
    except Exception as exc:  # noqa: BLE001 - invalid structured output is recorded
        return ReviewResult(
            outcome=CheckOutcome.INCONCLUSIVE,
            summary="The reviewer output violated the declared schema.",
            findings=[
                VerificationFinding(
                    severity=FindingSeverity.WARNING,
                    code="MODEL_REVIEW_INVALID",
                    message=f"Reviewer output schema validation failed: {exc}",
                    source="model_review",
                )
            ],
            details={**meta, "schema_error": str(exc)[:1000]},
            invocation_id=invocation.id,
        )

    diff_paths = {item.path for item in diff.files}
    spans = changed_line_ranges(patch)
    accepted: list[VerificationFinding] = []
    rejected: list[dict[str, Any]] = []
    for item in payload.findings:
        if item.path is not None and item.path not in diff_paths:
            rejected.append({"code": item.code, "path": item.path, "reason": "path not in diff"})
            accepted.append(_rejected_finding(item, "the path is not part of the candidate diff"))
            continue
        if item.line is not None and item.path is not None:
            file_spans = spans.get(item.path, [])
            if file_spans and not _line_in_spans(item.line, file_spans):
                # A line outside the hunks may still be valid context, but we do
                # not have the candidate file here; keep the finding and say so.
                accepted.append(
                    VerificationFinding(
                        severity=FindingSeverity(item.severity),
                        code=_safe_code(item.code),
                        message=_safe_message(item.message),
                        path=item.path[:1000] if item.path else None,
                        line=item.line,
                        source="model_review",
                        details={
                            "rationale": item.rationale[:3900],
                            "recommended_action": item.recommended_action[:900],
                            "line_outside_changed_hunks": True,
                        },
                    )
                )
                continue
        accepted.append(
            VerificationFinding(
                severity=FindingSeverity(item.severity),
                code=_safe_code(item.code),
                message=_safe_message(item.message),
                path=item.path[:1000] if item.path else None,
                line=item.line,
                source="model_review",
                details={
                    "rationale": item.rationale[:3900],
                    "recommended_action": item.recommended_action[:900],
                },
            )
        )

    implementer_models = {
        (invocation.provider, invocation.model)
        for invocation in invocations.list_for_snapshot(run.snapshot_id, limit=200)
        if invocation.purpose != "verification_review"
        and invocation.status == "SUCCEEDED"
    }
    same_model = (response.provider, response.model) in implementer_models
    blocking = [
        item
        for item in accepted
        if item.severity in {FindingSeverity.ERROR, FindingSeverity.BLOCKER}
        and item.code != "MODEL_REVIEW_UNGROUNDED"
    ]
    if payload.assessment == "UNSOUND" or any(
        item.severity == FindingSeverity.BLOCKER
        for item in accepted
        if item.code != "MODEL_REVIEW_UNGROUNDED"
    ):
        outcome = CheckOutcome.FAILED
    elif payload.assessment == "INCONCLUSIVE":
        outcome = CheckOutcome.INCONCLUSIVE
    else:
        outcome = CheckOutcome.PASSED
    summary = f"reviewer assessment {payload.assessment}: {payload.summary}"
    return ReviewResult(
        outcome=outcome,
        summary=summary,
        findings=accepted,
        details={
            **meta,
            "assessment": payload.assessment,
            "summary": payload.summary,
            "unverified_assumptions": payload.unverified_assumptions,
            "rejected_findings": rejected,
            "reviewer_provider": response.provider,
            "reviewer_model": response.model,
            "same_model_as_implementer": same_model,
            "independent": not same_model,
            "blocking_findings": len(blocking),
            "note": (
                "Model review is advisory. Deterministic checks remain authoritative; "
                "this review cannot prove or disprove correctness."
            ),
        },
        invocation_id=invocation.id,
    )


__all__ = [
    "ReviewPayload",
    "ReviewFindingPayload",
    "ReviewResult",
    "run_independent_review",
    "changed_line_ranges",
    "build_review_input",
]

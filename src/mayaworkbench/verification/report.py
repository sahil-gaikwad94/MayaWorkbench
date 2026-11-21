"""Evidence graph, decision rules and report rendering (§13.2, §13.5, Gate G).

The report is built from recorded checks. It never upgrades a claim because a
model said so, and it never reports a check that did not run. Its vocabulary
separates what was implemented from what was validated.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from mayaworkbench.domain.enums import (
    CheckKind,
    CheckOutcome,
    CriterionStatus,
    EvidenceRelation,
    FindingSeverity,
    TaskDecision,
    WorkflowState,
)
from mayaworkbench.domain.models import (
    CheckSpec,
    CriterionAssessment,
    EvidenceLink,
    Task,
    TaskContract,
    TaskDiff,
    VerificationCheck,
    WorkflowRun,
)
from mayaworkbench.ids import new_ulid

FUNCTIONAL_KINDS = {CheckKind.COMMAND, CheckKind.ML_INVARIANT}


def criteria_from_contract(contract: TaskContract) -> list[tuple[str, str]]:
    return [
        (f"AC{index}", text)
        for index, text in enumerate(contract.acceptance_criteria, start=1)
    ]


def assess_criteria(
    criteria: list[tuple[str, str]],
    checks: list[VerificationCheck],
) -> list[CriterionAssessment]:
    assessments: list[CriterionAssessment] = []
    for criterion_id, text in criteria:
        mapped = [check for check in checks if criterion_id in check.criteria]
        passed = [check for check in mapped if check.outcome == CheckOutcome.PASSED]
        failed = [check for check in mapped if check.outcome == CheckOutcome.FAILED]
        if failed:
            status = CriterionStatus.FAILED
            note = "Failing checks: " + ", ".join(check.key for check in failed)
        elif passed and len(passed) == len(mapped):
            status = CriterionStatus.VERIFIED
            note = "Verified by: " + ", ".join(check.key for check in passed)
        elif passed:
            status = CriterionStatus.PARTIAL
            note = (
                "Passed: "
                + ", ".join(check.key for check in passed)
                + " · no outcome: "
                + ", ".join(
                    f"{check.key}={check.outcome.value}"
                    for check in mapped
                    if check.outcome != CheckOutcome.PASSED
                )
            )
        elif mapped:
            status = CriterionStatus.UNVERIFIED
            note = "Declared checks did not produce a pass: " + ", ".join(
                f"{check.key}={check.outcome.value}" for check in mapped
            )
        else:
            status = CriterionStatus.UNVERIFIED
            note = (
                "No declared check references this criterion. Source inspection of "
                "the diff shows a change exists, but it is not functional evidence."
            )
        assessments.append(
            CriterionAssessment(
                criterion_id=criterion_id,
                text=text,
                status=status,
                check_keys=[check.key for check in mapped],
                note=note,
            )
        )
    return assessments


def _preexisting(check: VerificationCheck) -> bool:
    return bool(check.details.get("preexisting_failure"))


def decide(
    *,
    run: WorkflowRun,
    checks: list[VerificationCheck],
    criteria: list[CriterionAssessment],
    reviewer: dict[str, Any] | None,
    declared_specs: Iterable[CheckSpec],
) -> tuple[TaskDecision, list[str], list[str]]:
    """Compute a decision from recorded evidence only."""
    blocking: list[str] = []
    limitations: list[str] = list(run.limitations)
    required = [check for check in checks if check.required]
    declared = list(declared_specs)

    if run.state == WorkflowState.CANCELLED:
        return TaskDecision.INCONCLUSIVE, ["the run was cancelled"], limitations
    if run.state == WorkflowState.FAILED:
        return TaskDecision.FAILED, [run.error or "the run failed"], limitations
    if run.state == WorkflowState.BLOCKED:
        return TaskDecision.BLOCKED, [run.error or "the run was blocked"], limitations

    hard_failures = [
        check
        for check in required
        if check.outcome == CheckOutcome.FAILED and not _preexisting(check)
    ]
    if hard_failures:
        blocking.extend(
            f"{check.key} failed: {check.summary or 'no summary'}" for check in hard_failures
        )
        return TaskDecision.REJECTED, blocking, limitations

    blocked = [check for check in required if check.outcome == CheckOutcome.BLOCKED]
    if blocked:
        blocking.extend(
            f"{check.key} was blocked: {check.summary or 'no summary'}" for check in blocked
        )
        return TaskDecision.BLOCKED, blocking, limitations

    no_evidence = [
        check
        for check in required
        if check.outcome
        in {CheckOutcome.INCONCLUSIVE, CheckOutcome.UNVERIFIED, CheckOutcome.SKIPPED}
    ]
    # Evidence the contract explicitly requires (for example Gate F: a
    # comparable experiment) cannot be traded for a passing functional check.
    # If it is missing, the honest decision is INCONCLUSIVE.
    mandatory_missing = [
        check for check in no_evidence if check.details.get("mandatory")
    ]
    if mandatory_missing:
        blocking.extend(
            f"{check.key} is required evidence but produced no usable result: "
            f"{check.summary or check.outcome.value}"
            for check in mandatory_missing
        )
        return TaskDecision.INCONCLUSIVE, blocking, limitations
    if no_evidence:
        for check in no_evidence:
            limitations.append(
                f"{check.key} produced no usable evidence ({check.outcome.value}): "
                f"{check.summary or 'no summary'}"
            )
        # A passing built-in (scope, diff, reproducibility) is not evidence that
        # the implementation works. If every declared functional check was
        # skipped or inconclusive, the honest decision is INCONCLUSIVE even
        # though deterministic gates passed.
        if not any(
            check.outcome == CheckOutcome.PASSED and check.kind in FUNCTIONAL_KINDS
            for check in required
        ):
            blocking.append("no required functional check produced a passing result")
            return TaskDecision.INCONCLUSIVE, blocking, limitations

    if not any(check.kind in FUNCTIONAL_KINDS for check in checks):
        blocking.append(
            "no functional check (command or ML invariant) was declared, so the "
            "implementation is inspected but not validated"
        )
        return TaskDecision.INCONCLUSIVE, blocking, limitations

    failed_criteria = [
        item for item in criteria if item.status == CriterionStatus.FAILED
    ]
    if failed_criteria:
        blocking.extend(
            f"{item.criterion_id} failed: {item.note}" for item in failed_criteria
        )
        return TaskDecision.REJECTED, blocking, limitations

    reviewer_findings = (reviewer or {}).get("findings", [])
    blocking_review = [
        item
        for item in reviewer_findings
        if item.get("severity") in {"ERROR", "BLOCKER"} and item.get("source") == "model_review"
    ]
    if any(item.get("severity") == "BLOCKER" for item in blocking_review):
        blocking.append("the independent reviewer raised a BLOCKER finding")
        return TaskDecision.NEEDS_HUMAN_DECISION, blocking, limitations

    warnings = [
        finding
        for check in checks
        for finding in check.findings
        if finding.severity in {FindingSeverity.WARNING, FindingSeverity.ERROR}
    ]
    preexisting = [check for check in required if _preexisting(check)]
    for check in preexisting:
        limitations.append(
            f"{check.key} already failed before the change; the failure is not "
            "attributed to this patch, and it is not evidence that the change works."
        )
    unverified = [
        item for item in criteria if item.status != CriterionStatus.VERIFIED
    ]
    for item in unverified:
        limitations.append(
            f"{item.criterion_id} is {item.status.value.lower()}: {item.note}"
        )
    if warnings:
        limitations.append(
            f"{len(warnings)} check finding(s) require review; see the report."
        )
    if blocking_review:
        limitations.append(
            f"the independent reviewer raised {len(blocking_review)} error-level finding(s)."
        )
    if (reviewer or {}).get("same_model_as_implementer"):
        limitations.append(
            "the reviewer model is the same model that implemented the change; "
            "independence is reduced."
        )
    if (reviewer or {}).get("skipped_reason"):
        limitations.append(
            "independent review did not run: " + str(reviewer["skipped_reason"])
        )
    if (reviewer or {}).get("diff_truncated"):
        limitations.append("the reviewer received a truncated diff.")

    reviewer_concerns = (reviewer or {}).get("assessment") in {"CONCERNS", "UNSOUND"}
    if unverified or warnings or preexisting or blocking_review or reviewer_concerns or declared == []:
        return TaskDecision.COMPLETED_WITH_LIMITATIONS, blocking, _dedupe(limitations)
    return TaskDecision.ACCEPTED, blocking, _dedupe(limitations)


def apply_mutation_downgrade(
    decision: TaskDecision,
    limitations: list[str],
    mutation: dict[str, Any] | None,
) -> tuple[TaskDecision, list[str]]:
    """Apply the single WP9 decision-rule exception.

    A SURVIVED mutation on a *declared* critical test is a blind spot. It can
    only downgrade: an accepted result becomes COMPLETED_WITH_LIMITATIONS, and
    an already failing result becomes REJECTED. Nothing else changes.
    """
    if not mutation or not mutation.get("counts", {}).get("SURVIVED"):
        return decision, limitations
    survivors = [
        item
        for item in mutation.get("results", [])
        if item.get("outcome") == "SURVIVED" and item.get("declared")
    ]
    if not survivors:
        return decision, limitations
    updated = decision
    if decision == TaskDecision.ACCEPTED:
        updated = TaskDecision.COMPLETED_WITH_LIMITATIONS
    elif decision == TaskDecision.FAILED:
        updated = TaskDecision.REJECTED
    locations = ", ".join(
        f"{item.get('file')}::{item.get('test')}" for item in survivors[:3]
    )
    warning = (
        f"mutation: {len(survivors)} declared critical test(s) SURVIVED mutation "
        f"probing ({locations or 'locations unavailable'}); this is a blind spot, "
        "never a pass"
    )
    return updated, list(limitations) + [warning]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def build_evidence_links(
    *,
    verification_id: str,
    run_id: str,
    report_id: str,
    checks: list[VerificationCheck],
    criteria: list[CriterionAssessment],
    reviewer: dict[str, Any] | None,
    patch_sha256: str,
    mutation: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
) -> list[EvidenceLink]:
    links: list[EvidenceLink] = []
    for check in checks:
        relation = (
            EvidenceRelation.CONTRADICTS
            if check.outcome == CheckOutcome.FAILED
            else EvidenceRelation.SUPPORTS
        )
        for criterion_id in check.criteria:
            links.append(
                EvidenceLink(
                    id="evl_" + new_ulid(),
                    verification_id=verification_id,
                    run_id=run_id,
                    report_id=report_id,
                    from_kind="CHECK",
                    from_ref=check.key,
                    relation=relation,
                    to_kind="CRITERION",
                    to_ref=criterion_id,
                    note=check.outcome.value,
                )
            )
        if check.artifact_id:
            links.append(
                EvidenceLink(
                    id="evl_" + new_ulid(),
                    verification_id=verification_id,
                    run_id=run_id,
                    report_id=report_id,
                    from_kind="CHECK",
                    from_ref=check.key,
                    relation=EvidenceRelation.RECORDED_IN,
                    to_kind="ARTIFACT",
                    to_ref=check.artifact_id,
                    note=check.outcome.value,
                )
            )
        if check.command:
            links.append(
                EvidenceLink(
                    id="evl_" + new_ulid(),
                    verification_id=verification_id,
                    run_id=run_id,
                    report_id=report_id,
                    from_kind="CHECK",
                    from_ref=check.key,
                    relation=EvidenceRelation.DERIVED_FROM,
                    to_kind="COMMAND",
                    to_ref=json.dumps(check.command)[:1000],
                    note=f"exit={check.exit_code}",
                )
            )
    for criterion in criteria:
        if criterion.status == CriterionStatus.UNVERIFIED:
            links.append(
                EvidenceLink(
                    id="evl_" + new_ulid(),
                    verification_id=verification_id,
                    run_id=run_id,
                    report_id=report_id,
                    from_kind="CRITERION",
                    from_ref=criterion.criterion_id,
                    relation=EvidenceRelation.RECORDED_IN,
                    to_kind="NOTE",
                    to_ref=criterion.note[:1000],
                    note="no check evidence",
                )
            )
    if reviewer:
        links.append(
            EvidenceLink(
                id="evl_" + new_ulid(),
                verification_id=verification_id,
                run_id=run_id,
                report_id=report_id,
                from_kind="MODEL_REVIEW",
                from_ref=reviewer.get("check_key", "review.independent"),
                relation=EvidenceRelation.SUPPORTS,
                to_kind="CRITERION",
                to_ref="*",
                note=str(reviewer.get("assessment", "INCONCLUSIVE")),
            )
        )
    links.append(
        EvidenceLink(
            id="evl_" + new_ulid(),
            verification_id=verification_id,
            run_id=run_id,
            report_id=report_id,
            from_kind="PATCH",
            from_ref=patch_sha256,
            relation=EvidenceRelation.RECORDED_IN,
            to_kind="REPORT",
            to_ref=report_id,
            note="the patch hash this report verified",
            sha256=patch_sha256,
        )
    )
    # WP9 — mutation probes and the deterministic security report are evidence
    # too. Their artifacts and individual findings enter the same graph, so a
    # reader can follow a SURVIVED probe or a security finding to raw output.
    for payload, check_key in ((mutation, "mutation.probe"), (security, "security.report")):
        if not payload or not payload.get("artifact_id"):
            continue
        links.append(
            EvidenceLink(
                id="evl_" + new_ulid(),
                verification_id=verification_id,
                run_id=run_id,
                report_id=report_id,
                from_kind="CHECK",
                from_ref=check_key,
                relation=EvidenceRelation.RECORDED_IN,
                to_kind="ARTIFACT",
                to_ref=payload["artifact_id"],
                note=check_key,
            )
        )
    for item in (mutation or {}).get("results", []):
        if not item.get("artifact_id"):
            continue
        links.append(
            EvidenceLink(
                id="evl_" + new_ulid(),
                verification_id=verification_id,
                run_id=run_id,
                report_id=report_id,
                from_kind="MUTATION",
                from_ref=str(item.get("mutation_id", "mutation"))[:1000],
                relation=EvidenceRelation.RECORDED_IN,
                to_kind="ARTIFACT",
                to_ref=item["artifact_id"],
                note=f"{item.get('outcome')}:{item.get('kind')}",
            )
        )
    for edge in (security or {}).get("evidence_links", []):
        links.append(
            EvidenceLink(
                id="evl_" + new_ulid(),
                verification_id=verification_id,
                run_id=run_id,
                report_id=report_id,
                from_kind=str(edge.get("from_kind", "SECURITY_FINDING"))[:40],
                from_ref=str(edge.get("from_ref", "security"))[:1000],
                relation=EvidenceRelation.RECORDED_IN,
                to_kind="ARTIFACT",
                to_ref=str(edge.get("to_ref", ""))[:1000],
                note=str(edge.get("note", ""))[:1000],
            )
        )
    return links


def build_report_payload(
    *,
    attempt_id: str,
    report_id: str,
    run: WorkflowRun,
    task: Task,
    contract: TaskContract,
    diff: TaskDiff,
    checks: list[VerificationCheck],
    criteria: list[CriterionAssessment],
    decision: TaskDecision,
    summary: str,
    blocking: list[str],
    limitations: list[str],
    reviewer: dict[str, Any] | None,
    declared_specs: list[CheckSpec],
    experiment: dict[str, Any] | None = None,
    mutation: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "verification_id": attempt_id,
        "report_id": report_id,
        "run_id": run.id,
        "task_id": task.id,
        "project_id": run.project_id,
        "snapshot_id": run.snapshot_id,
        "base_commit_sha": run.base_commit_sha,
        "contract_sha256": contract.content_sha256,
        "patch_sha256": diff.patch_sha256,
        "decision": decision.value,
        "summary": summary,
        "blocking": blocking,
        "limitations": limitations,
        "criteria": [item.model_dump(mode="json") for item in criteria],
        "checks": [item.model_dump(mode="json") for item in checks],
        "declared_checks": [item.model_dump(mode="json") for item in declared_specs],
        "reviewer": reviewer,
        "experiment": experiment,
        "mutation": mutation,
        "security": security,
        "evidence_classification": evidence_classification(
            checks, reviewer, diff, experiment
        ),
        "generated_at": _now_iso(),
    }


def evidence_classification(
    checks: list[VerificationCheck],
    reviewer: dict[str, Any] | None,
    diff: TaskDiff,
    experiment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate implemented, checked, model-assessed and unverified claims."""
    functional = [check for check in checks if check.kind in FUNCTIONAL_KINDS]
    observed = bool(
        experiment
        and experiment.get("comparable")
        and experiment.get("verdict") not in {"NOT_COMPARABLE", "INCONCLUSIVE"}
        and experiment.get("observations", 0) > 0
    )
    return {
        "implemented": {
            "value": bool(diff.files),
            "basis": f"{len(diff.files)} file(s), +{diff.additions}/-{diff.deletions}",
        },
        "deterministically_checked": {
            "value": any(
                check.outcome == CheckOutcome.PASSED
                and check.kind
                in {CheckKind.COMMAND, CheckKind.TEST_INTEGRITY, CheckKind.DETERMINISTIC}
                for check in checks
            ),
            "basis": [check.key for check in functional if check.outcome == CheckOutcome.PASSED],
        },
        "experimentally_observed": {
            "value": observed,
            "basis": (
                f"{experiment.get('comparison_id')} — {experiment.get('verdict')} "
                f"({experiment.get('primary_summary', 'no primary metric')})"
                if observed and experiment
                else (
                    "a comparison exists but produced no comparable observation"
                    if experiment
                    else "no experiment is attached to this task"
                )
            ),
        },
        "model_assessed": {
            "value": bool(reviewer and not reviewer.get("skipped_reason")),
            "basis": (reviewer or {}).get("assessment", "not run"),
        },
        "human_confirmed": {
            "value": False,
            "basis": "recorded by `maya task finish`, not by verification",
        },
        "unverified": [
            check.key
            for check in checks
            if check.outcome
            in {CheckOutcome.SKIPPED, CheckOutcome.BLOCKED, CheckOutcome.INCONCLUSIVE, CheckOutcome.UNVERIFIED}
        ],
    }


def render_markdown(
    payload: dict[str, Any],
    *,
    task: Task,
    run: WorkflowRun,
    contract: TaskContract,
) -> str:
    checks = [VerificationCheck.model_validate(item) for item in payload["checks"]]
    criteria = [CriterionAssessment.model_validate(item) for item in payload["criteria"]]
    lines: list[str] = [
        f"# Verification report: {task.title}",
        "",
        f"- Decision: **{payload['decision']}**",
        f"- Run: `{run.id}`",
        f"- Snapshot: `{run.snapshot_id}`",
        f"- Base commit: `{run.base_commit_sha}`",
        f"- Contract sha256: `{contract.content_sha256}`",
        f"- Patch sha256: `{payload['patch_sha256']}`",
        f"- Verification attempt: `{payload['verification_id']}`",
        "",
        "## Summary",
        "",
        payload["summary"],
        "",
        "## Checks",
        "",
        "| gate | check | outcome | required | evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in checks:
        evidence = check.artifact_id or ("command" if check.command else "recorded result")
        lines.append(
            f"| `{check.gate.value}` | `{check.key}` | `{check.outcome.value}` | "
            f"{'yes' if check.required else 'no'} | {evidence} |"
        )
    lines.extend(
        [
            "",
            "## Acceptance criteria",
            "",
            "| id | criterion | status | evidence |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in criteria:
        note = item.note.replace("|", "\\|")
        text = item.text.replace("|", "\\|")
        lines.append(f"| {item.criterion_id} | {text} | `{item.status.value}` | {note} |")
    findings = [finding for check in checks for finding in check.findings]
    lines.extend(["", "## Findings", ""])
    if findings:
        for finding in findings:
            location = finding.path or ""
            if finding.line:
                location += f":{finding.line}"
            lines.append(
                f"- **{finding.severity.value}** `{finding.code}` {location} — {finding.message}"
            )
    else:
        lines.append("- No findings were recorded.")

    reviewer = payload.get("reviewer")
    lines.extend(["", "## Independent review", ""])
    if reviewer and not reviewer.get("skipped_reason"):
        lines.append(
            f"- Assessment: **{reviewer.get('assessment', 'INCONCLUSIVE')}** "
            f"({reviewer.get('reviewer_provider')}/{reviewer.get('reviewer_model')})"
        )
        if reviewer.get("same_model_as_implementer"):
            lines.append(
                "- The reviewer is the same model as the implementer; this is not "
                "an independent check."
            )
        for item in reviewer.get("findings", []):
            lines.append(
                f"- {str(item.get('severity')).upper()} `{item.get('code')}` "
                f"{item.get('path') or ''} — {item.get('message')}"
            )
        if reviewer.get("rejected_findings"):
            lines.append(
                f"- {len(reviewer['rejected_findings'])} reviewer finding(s) were rejected "
                "as ungrounded."
            )
    else:
        lines.append("- Not run or unavailable: " + str((reviewer or {}).get("skipped_reason", "not requested")))

    lines.extend(["", "## What is actually established", ""])
    classification = payload.get("evidence_classification", {})
    implemented = classification.get("implemented", {})
    lines.append(
        f"- **Implemented:** {implemented.get('value')} ({implemented.get('basis', '')}). "
        "A diff is not a working behaviour."
    )
    checked = classification.get("deterministically_checked", {})
    lines.append(
        f"- **Deterministically checked:** {checked.get('value')} "
        f"({', '.join(checked.get('basis', [])) or 'no passing functional check'})."
    )
    experiment = payload.get("experiment")
    if experiment:
        lines.append(
            "- **Experimentally observed:** "
            f"{classification.get('experimentally_observed', {}).get('value')} — "
            f"{classification.get('experimentally_observed', {}).get('basis')}."
        )
    else:
        lines.append(
            "- **Experimentally observed:** false — no experiment is attached to this "
            "task; a passing check is not an experiment result."
        )
    model_assessed = classification.get("model_assessed", {})
    lines.append(
        f"- **Model-assessed:** {model_assessed.get('value')} "
        f"({model_assessed.get('basis')}). Advisory only."
    )
    lines.append("- **Human-confirmed:** recorded separately by `maya task finish`.")
    unverified = classification.get("unverified", [])
    lines.append(
        "- **Unverified:** " + (", ".join(unverified) if unverified else "no check was left unverified")
    )

    if experiment:
        lines.extend(
            [
                "",
                "## Experiment evidence",
                "",
                f"- Comparison: `{experiment.get('comparison_id')}`",
                f"- Verdict: **{experiment.get('verdict')}**"
                f" ({'comparable' if experiment.get('comparable') else 'not comparable'})",
                f"- Design: `{experiment.get('design', 'unknown')}`",
                f"- Primary metric: {experiment.get('primary_summary', 'not reported')}",
                f"- Observations: {experiment.get('observations', 0)}",
                "",
                "The experiment verdict is evidence under the recorded protocol. It is "
                "not a proof of scientific truth, and it does not change any deterministic "
                "check above.",
            ]
        )
        experiment_limitations = experiment.get("limitations") or []
        if experiment_limitations:
            lines.extend(["", "Experiment limitations:", ""])
            lines.extend(f"- {item}" for item in experiment_limitations)
        if experiment.get("bundle_files"):
            lines.extend(
                [
                    "",
                    "The verification bundle embeds the comparison report and every run "
                    "manifest under `experiments/`.",
                ]
            )

    mutation = payload.get("mutation")
    if mutation:
        counts = mutation.get("counts", {})
        lines.extend(
            [
                "",
                "## Mutation probes (declared critical tests)",
                "",
                f"- Declared: {len(mutation.get('declared', []))}; "
                f"KILLED {counts.get('KILLED', 0)}, SURVIVED {counts.get('SURVIVED', 0)}, "
                f"SKIPPED {counts.get('SKIPPED', 0)}.",
                f"- Critical-test baseline: {len(mutation.get('baseline', {}).get('files', {}))} file(s) pinned at the base revision.",
                f"- Bypass flag: {'YES' if mutation.get('bypass_detected') else 'no'}.",
            ]
        )
        for item in mutation.get("results", []):
            lines.append(
                f"- `{item.get('outcome')}` `{item.get('kind')}` "
                f"{item.get('file', '')}:{item.get('line', 0)} "
                f"{item.get('test', '')} — {item.get('reason', '')} "
                f"({item.get('artifact_id') or 'no artifact'})"
            )
        lines.append(
            "- Property-level probes only: a kill shows one assertion is load-bearing "
            "for one small fault; it is not semantic proof."
        )
    security = payload.get("security")
    if security:
        lines.extend(
            [
                "",
                "## Security report (deterministic)",
                "",
                f"- Findings: {len(security.get('findings', []))}; artifact `{security.get('artifact_id')}`.",
            ]
        )
        for item in security.get("findings", []):
            location = f"{item.get('path')}" if item.get("path") else ""
            lines.append(
                f"- **{item.get('severity')}** `{item.get('code')}` "
                f"{location} — {item.get('message', '')}"
            )
        lines.append(
            "- No model is used. These are shallow deterministic checks, not a "
            "penetration test or a security guarantee."
        )

    lines.extend(["", "## Limitations and assumptions", ""])
    if payload["limitations"]:
        lines.extend(f"- {item}" for item in payload["limitations"])
    else:
        lines.append("- None recorded.")
    if payload["blocking"]:
        lines.extend(["", "## Blocking reasons", ""])
        lines.extend(f"- {item}" for item in payload["blocking"])
    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "```bash",
            f"maya task diff {run.id}",
            f"maya verify run {run.id}",
            f"maya verify bundle {run.id} <new-directory>",
            "```",
            "",
            "This report was generated from recorded checks. It does not claim that "
            "unlisted checks ran, and it does not treat model review as proof.",
            "",
        ]
    )
    return "\n".join(lines)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _now_iso() -> str:
    from mayaworkbench.clock import utcnow

    return utcnow().isoformat()


__all__ = [
    "criteria_from_contract",
    "assess_criteria",
    "decide",
    "build_evidence_links",
    "build_report_payload",
    "render_markdown",
    "apply_mutation_downgrade",
    "evidence_classification",
    "canonical_sha256",
    "FUNCTIONAL_KINDS",
]

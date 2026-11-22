"""Service that assembles a reproducibility report from recorded evidence.

The report is a derived view, never a new truth store: the deterministic
assembler reads the pinned workspace, claims, mappings, the latest verification
attempt and the latest experiment comparison, then persists the resulting report
as a content-addressed artifact and an append-only event. Rebuilding the same run
with the same records yields the same canonical hash.
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.domain.enums import ActorKind, ArtifactKind, EventType
from mayaworkbench.domain.models import WorkflowEvent
from mayaworkbench.ids import new_ulid
from mayaworkbench.research.reproduction import (
    ReproducibilityReport,
    build_reproduction_report,
)


class ReproductionService:
    """Read recorded evidence and produce an honest four-tier report."""

    def __init__(
        self,
        *,
        artifacts: Any,
        events: Any,
        runs: Any,
        tasks: Any,
        claims: Any,
        mappings: Any,
        diffs: Any,
        verification: Any,
        experiments: Any,
        workspaces: Any,
    ):
        self.artifacts = artifacts
        self.events = events
        self.runs = runs
        self.tasks = tasks
        self.claims = claims
        self.mappings = mappings
        self.diffs = diffs
        self.verification = verification
        self.experiments = experiments
        self.workspaces = workspaces

    def build(self, run_id: str, *, persist: bool = True) -> tuple[ReproducibilityReport, str | None]:
        """Assemble the report for one run. Returns (report, artifact_id)."""
        run = self.runs.get(run_id)
        task = self.tasks.get(run.task_id)
        # The pinned snapshot is a property of the run, not of the Git worktree.
        snapshot_id = run.snapshot_id

        claims = self.claims.list(
            project_id=run.project_id,
            include_project_level=True,
            limit=2000,
        )
        mappings = (
            self.mappings.list_by_snapshot(snapshot_id) if snapshot_id else []
        )
        verification_report = self.verification.latest_report(run_id)
        checks = (
            self.verification.report_checks(verification_report)
            if verification_report is not None
            else []
        )
        comparison = self.experiments.latest_comparison_for_task(run.task_id)
        run_details = run.details or {}
        if comparison is not None:
            # Comparisons are fetched by task, so the task guard is explicit
            # even though the store query is supposed to have applied it.
            if comparison.task_id != run.task_id:
                raise ValueError(
                    f"comparison {comparison.id} belongs to task {comparison.task_id}, "
                    f"not run {run_id} task {run.task_id}"
                )
            # There is no run-level comparison store: the only honest binding
            # is the id the run itself declares. Mismatch is refused, absence
            # is reported by the builder as a task-scoped limitation and caps
            # RESULT_REPRODUCED at PARTIAL rather than pretending it is bound.
            bound_id = run_details.get("experiment_comparison_id")
            if bound_id is not None and str(bound_id) != comparison.id:
                raise ValueError(
                    f"run {run_id} declares experiment_comparison_id={bound_id}, "
                    f"which does not match comparison {comparison.id}"
                )
        diff = self.diffs.get_by_run(run_id)

        report = build_reproduction_report(
            run=run,
            task=task,
            run_details=run_details,
            claims=claims,
            mappings=mappings,
            report=verification_report,
            checks=checks,
            comparison=comparison,
            diff=diff,
        )

        artifact_id: str | None = None
        if persist:
            artifact = self.artifacts.put_text(
                report.model_dump_json(indent=2),
                kind=ArtifactKind.REPRODUCIBILITY_REPORT,
                media_type="application/json",
                created_by="reproduction_service",
            )
            artifact_id = artifact.id
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.REPRODUCIBILITY_REPORT_CREATED,
                    project_id=run.project_id,
                    snapshot_id=snapshot_id,
                    task_id=run.task_id,
                    run_id=run_id,
                    entity_type="reproducibility_report",
                    entity_id=artifact_id,
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="SYSTEM:reproduction",
                    payload={
                        "artifact_id": artifact_id,
                        "canonical_sha256": report.canonical_sha256,
                        "tiers": {
                            name: tier.level for name, tier in report.tiers.items()
                        },
                        "generated_at": report.generated_at,
                    },
                    schema_version="1.0",
                )
            )
        return report, artifact_id

    def report_from_artifact(self, artifact_id: str) -> ReproducibilityReport:
        """Rehydrate a persisted report without recomputing it."""
        from mayaworkbench.research.reproduction import ReproducibilityReport

        return ReproducibilityReport.model_validate_json(
            self.artifacts.read_text(artifact_id)
        )


__all__ = ["ReproductionService"]

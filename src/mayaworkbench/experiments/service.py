"""Experiment Lab orchestration (Phase 5).

Lifecycle:

1. ``create_definition`` pre-registers role, command, seeds, metrics,
   directions, protocol and budgets, and hashes all of it.
2. ``approve`` records a user decision bound to that exact hash. Editing an
   experiment means creating a new definition; an approval can never be widened
   after the fact.
3. ``run`` executes declared seeds in isolated worktrees with hard budgets and
   offline policy, creating one immutable manifest and one run row per seed.
4. ``record_external`` records a user-owned run whose metrics were produced
   elsewhere, which is explicitly *not* local execution.
5. ``compare`` classifies protocol differences and computes honest statistics.
6. ``bundle`` exports a verifiable reproduction package.

Nothing here can upgrade a verdict because a model said so; the reviewer is not
part of this phase's authority model.
"""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
    ExperimentProfile,
    ExperimentRole,
    ExperimentRunStatus,
    ExperimentStatus,
    ExperimentVerdict,
    MemoryAuthor,
    MemoryKind,
    MemoryStatus,
    MetricsSourceKind,
    RetentionClass,
)
from mayaworkbench.domain.models import (
    ExperimentBudget,
    ExperimentComparison,
    ExperimentDefinition,
    ExperimentManifest,
    ExperimentProtocol,
    ExperimentRun,
    MemorySource,
    MetricObservation,
    MetricSpec,
    MetricsSource,
    Project,
    ProjectMemory,
    Repository,
    RepositorySnapshot,
    WorkflowEvent,
)
from mayaworkbench.errors import ExperimentError, UsageError
from mayaworkbench.experiments import report as experiment_report
from mayaworkbench.experiments.comparison import build_comparison
from mayaworkbench.experiments.metrics import (
    build_observations,
    parse_imported_metrics,
)
from mayaworkbench.experiments.protocol import (
    canonical_sha256,
    protocol_values,
    validate_dimension,
)
from mayaworkbench.experiments.runner import (
    EXPERIMENT_SEED_ENV,
    TREATMENT_PLACEHOLDER,
    ExperimentRunner,
)
from mayaworkbench.ids import default_host_id, new_ulid
from mayaworkbench.version import EXPERIMENT_PROTOCOL_VERSION
from mayaworkbench.workspace.runner import DEFAULT_EXECUTABLES

MAX_ATTEMPTS_PER_SEED = 5
_STATIC_PROFILE = ExperimentProfile.STATIC


class ExperimentService:
    def __init__(
        self,
        *,
        home: Path,
        settings: Settings,
        artifacts: Any,
        events: Any,
        store: Any,
        repositories: Any,
        snapshots: Any,
        files: Any,
        tasks: Any = None,
        contracts: Any = None,
        memory: Any = None,
    ):
        self.home = Path(home)
        self.settings = settings
        self.artifacts = artifacts
        self.events = events
        self.store = store
        self.repositories = repositories
        self.snapshots = snapshots
        self.files = files
        self.tasks = tasks
        self.contracts = contracts
        self.memory = memory
        self.runner = ExperimentRunner(
            home=self.home,
            settings=settings.experiments,
            artifacts=artifacts,
            files=files,
        )

    # ----------------------------------------------------------- definitions
    def create_definition(
        self,
        *,
        project_id: str,
        snapshot_id: str,
        name: str,
        hypothesis: str,
        role: ExperimentRole,
        profile: ExperimentProfile,
        command: list[str],
        seeds: list[int],
        metrics: list[MetricSpec],
        timeout_seconds: int = 300,
        metrics_source: MetricsSource | None = None,
        protocol: ExperimentProtocol | None = None,
        treatment_dimensions: list[str] | None = None,
        allowed_variation: list[str] | None = None,
        budget: ExperimentBudget | None = None,
        min_seeds: int | None = None,
        non_inferiority_margin: float | None = None,
        task_id: str | None = None,
        approve: bool = False,
        created_by: str = "local-user",
    ) -> ExperimentDefinition:
        if not name.strip() or not hypothesis.strip():
            raise UsageError("An experiment needs a name and a hypothesis")
        metric_keys = [metric.key for metric in metrics]
        if len(metric_keys) != len(set(metric_keys)):
            raise ExperimentError("Metric keys must be unique within a definition")
        if sum(1 for metric in metrics if metric.primary) != 1:
            raise ExperimentError(
                "Exactly one metric must be declared primary",
                hint="The primary metric drives the verdict; secondary metrics are exploration.",
            )
        if len(seeds) != len(set(seeds)):
            raise ExperimentError(
                "Seeds must be unique; a duplicate seed is the same replicate twice"
            )
        if any(seed < 0 or seed > 2**32 - 1 for seed in seeds):
            raise ExperimentError("Seeds must be integers between 0 and 2**32 - 1")
        if metrics_source is not None and metrics_source.path:
            path = Path(metrics_source.path)
            if path.is_absolute() or ".." in path.parts:
                raise ExperimentError(
                    "The metrics path must be workspace-relative and may not contain '..'"
                )
        snapshot = self.snapshots.get(snapshot_id)
        repository = self.repositories.get(snapshot.repository_id)
        if repository.project_id != project_id:
            raise ExperimentError("The snapshot belongs to a different project")
        if snapshot.status.value != "INDEXED":
            raise ExperimentError(
                f"Snapshot {snapshot.id} is {snapshot.status.value}, not INDEXED",
                hint="Run `maya index` before pre-registering an experiment on it.",
            )
        if snapshot.commit_sha is None or len(snapshot.commit_sha) != 40:
            raise ExperimentError("The snapshot has no full 40-character commit SHA")
        if task_id is not None:
            if self.tasks is None or self.contracts is None:
                raise ExperimentError("Task linkage is not available in this service")
            task = self.tasks.get(task_id)
            if task.project_id != project_id:
                raise ExperimentError("The task belongs to a different project")
            contract = self.contracts.latest(task_id)
            if contract.snapshot_id != snapshot_id:
                raise ExperimentError(
                    "The task contract and the experiment must use the same snapshot",
                    hint=f"The task uses snapshot {contract.snapshot_id}.",
                )

        source = metrics_source or MetricsSource()
        experiment_budget = budget or ExperimentBudget()
        min_seed_count = (
            self.settings.experiments.default_min_seeds if min_seeds is None else min_seeds
        )
        dimensions = [
            validate_dimension(item, field_name="treatment dimension")
            for item in (
                treatment_dimensions
                or ["command", "repository.commit", "repository.patch"]
            )
        ]
        variation = [
            validate_dimension(item, field_name="allowed variation")
            for item in (allowed_variation or [])
        ]
        self._validate_budget(
            profile=profile,
            command=command,
            timeout_seconds=timeout_seconds,
            seeds=seeds,
            metrics=metrics,
            metrics_source=source,
            budget=experiment_budget,
            min_seeds=min_seed_count,
            non_inferiority_margin=non_inferiority_margin,
        )

        base_payload = {
            "schema_version": EXPERIMENT_PROTOCOL_VERSION,
            "project_id": project_id,
            "repository_id": repository.id,
            "snapshot_id": snapshot.id,
            "base_commit_sha": snapshot.commit_sha,
            "task_id": task_id,
            "name": name.strip(),
            "hypothesis": hypothesis.strip(),
            "role": role.value,
            "profile": profile.value,
            "command": list(command),
            "timeout_seconds": timeout_seconds,
            "seeds": sorted(seeds),
            "metrics": [item.model_dump(mode="json") for item in metrics],
            "metrics_source": source.model_dump(mode="json"),
            "protocol": (protocol or ExperimentProtocol()).model_dump(mode="json"),
            "treatment_dimensions": dimensions,
            "allowed_variation": variation,
            "budget": experiment_budget.model_dump(mode="json"),
            "min_seeds": min_seed_count,
            "non_inferiority_margin": non_inferiority_margin,
        }
        content_sha = canonical_sha256(base_payload)
        definition = ExperimentDefinition(
            id="exp_" + new_ulid(),
            project_id=project_id,
            repository_id=repository.id,
            snapshot_id=snapshot.id,
            base_commit_sha=snapshot.commit_sha,
            task_id=task_id,
            name=name.strip(),
            hypothesis=hypothesis.strip(),
            role=role,
            profile=profile,
            command=list(command),
            timeout_seconds=timeout_seconds,
            seeds=sorted(seeds),
            metrics=list(metrics),
            metrics_source=source,
            protocol=protocol or ExperimentProtocol(),
            treatment_dimensions=dimensions,
            allowed_variation=variation,
            budget=experiment_budget,
            min_seeds=min_seed_count,
            non_inferiority_margin=non_inferiority_margin,
            status=ExperimentStatus.DRAFT,
            content_sha256=content_sha,
            created_by=created_by,
        )
        artifact = self.artifacts.put_text(
            json.dumps(
                {**base_payload, "experiment_id": definition.id, "content_sha256": content_sha},
                indent=2,
                sort_keys=True,
            ),
            kind=ArtifactKind.EXPERIMENT_PROTOCOL,
            media_type="application/json",
            created_by="experiment-create",
        )
        definition.details["protocol_artifact_id"] = artifact.id
        self.store.create_definition(definition)
        self._event(
            EventType.EXPERIMENT_CREATED,
            definition,
            entity_type="experiment",
            entity_id=definition.id,
            actor=ActorKind.USER,
            payload={
                "name": definition.name,
                "role": definition.role.value,
                "profile": definition.profile.value,
                "snapshot_id": snapshot.id,
                "content_sha256": content_sha,
                "seeds": definition.seeds,
                "protocol_artifact_id": artifact.id,
                "task_id": task_id,
            },
        )
        if approve:
            return self.approve(definition.id, reason="approved at creation")
        return definition

    def _validate_budget(
        self,
        *,
        profile: ExperimentProfile,
        command: list[str],
        timeout_seconds: int,
        seeds: list[int],
        metrics: list[MetricSpec],
        metrics_source: MetricsSource,
        budget: ExperimentBudget,
        min_seeds: int,
        non_inferiority_margin: float | None,
    ) -> None:
        limits = self.settings.experiments
        if timeout_seconds > limits.max_runtime_seconds:
            raise ExperimentError(
                f"timeout {timeout_seconds}s exceeds the local experiment ceiling "
                f"{limits.max_runtime_seconds}s"
            )
        if len(seeds) > budget.max_runs:
            raise ExperimentError(
                f"{len(seeds)} seeds exceed the definition's max_runs={budget.max_runs}"
            )
        if len(seeds) > limits.max_runs:
            raise ExperimentError(
                f"{len(seeds)} seeds exceed the local ceiling of {limits.max_runs}"
            )
        if budget.max_total_runtime_seconds > limits.max_total_runtime_seconds:
            raise ExperimentError(
                "the declared total runtime budget exceeds the local ceiling"
            )
        if budget.max_artifact_bytes > limits.max_artifact_bytes:
            raise ExperimentError("the declared artifact budget exceeds the local ceiling")
        if budget.max_metric_observations > limits.max_metric_observations:
            raise ExperimentError(
                "the declared metric-observation budget exceeds the local ceiling"
            )
        if min_seeds < 1 or min_seeds > len(seeds):
            raise ExperimentError(
                f"min_seeds={min_seeds} must be between 1 and the number of seeds "
                f"({len(seeds)})"
            )
        if len(seeds) * timeout_seconds > budget.max_total_runtime_seconds:
            raise ExperimentError(
                f"worst-case runtime ({len(seeds)} x {timeout_seconds}s) exceeds the "
                f"declared total budget of {budget.max_total_runtime_seconds}s",
                hint="Reduce seeds or timeouts, or raise the budget deliberately.",
            )
        if non_inferiority_margin is not None:
            primary = next(item for item in metrics if item.primary)
            if primary.direction.value == "NEUTRAL":
                raise ExperimentError(
                    "a non-inferiority margin is meaningless for a NEUTRAL metric"
                )
            if non_inferiority_margin < 0:
                raise ExperimentError("the non-inferiority margin must be non-negative")

        if profile == _STATIC_PROFILE:
            if command:
                raise ExperimentError(
                    "STATIC experiments do not execute commands; record metrics with "
                    "`maya experiment record`"
                )
            if metrics_source.kind != MetricsSourceKind.IMPORTED:
                raise ExperimentError(
                    "STATIC experiments must declare IMPORTED metrics"
                )
        else:
            if not command:
                raise ExperimentError(
                    f"{profile.value} experiments require a command",
                    hint="Use STATIC + `maya experiment record` for external runs.",
                )
            if metrics_source.kind == MetricsSourceKind.IMPORTED:
                raise ExperimentError(
                    "an executable experiment cannot use IMPORTED metrics at run time; "
                    "ingest them afterwards with `maya experiment ingest`"
                )
            _validate_command(command)
        if metrics_source.kind == MetricsSourceKind.JSON_FILE:
            if not metrics_source.path:
                raise ExperimentError("JSON_FILE metrics require --metrics-file")
        elif metrics_source.path:
            raise ExperimentError(
                "--metrics-file is only valid with the JSON_FILE metrics source"
            )

    def approve(self, experiment_id: str, *, reason: str | None = None) -> ExperimentDefinition:
        definition = self.store.get_definition(experiment_id)
        if definition.status != ExperimentStatus.DRAFT:
            raise ExperimentError(
                f"Experiment is {definition.status.value}; only DRAFT definitions can be approved",
                hint="Create a new definition for a changed experiment.",
            )
        self.store.update_definition_approval(
            definition.id,
            status=ExperimentStatus.APPROVED.value,
            approved_by="local-user",
            approved_at=utcnow(),
            approval_sha256=definition.content_sha256,
        )
        self._event(
            EventType.EXPERIMENT_APPROVED,
            definition,
            entity_type="experiment",
            entity_id=definition.id,
            actor=ActorKind.USER,
            payload={
                "content_sha256": definition.content_sha256,
                "reason": reason or "",
                "seeds": definition.seeds,
                "command": definition.command,
            },
        )
        return self.store.get_definition(definition.id)

    def revoke(self, experiment_id: str, *, reason: str) -> ExperimentDefinition:
        definition = self.store.get_definition(experiment_id)
        if definition.status != ExperimentStatus.APPROVED:
            raise ExperimentError(
                f"Experiment is {definition.status.value}; only APPROVED definitions can be revoked"
            )
        self.store.update_definition_approval(
            definition.id,
            status=ExperimentStatus.REVOKED.value,
            approved_by=definition.approved_by,
            approved_at=definition.approved_at,
            approval_sha256=definition.approval_sha256,
            revoked_reason=reason,
        )
        self._event(
            EventType.EXPERIMENT_REVOKED,
            definition,
            entity_type="experiment",
            entity_id=definition.id,
            actor=ActorKind.USER,
            payload={"reason": reason, "content_sha256": definition.content_sha256},
        )
        return self.store.get_definition(definition.id)

    # --------------------------------------------------------------- queries
    def resolve(self, identifier: str) -> ExperimentDefinition:
        if identifier.startswith("exp_"):
            return self.store.get_definition(identifier)
        matches = [
            item
            for item in self.store.list_definitions(limit=1000)
            if item.name == identifier
        ]
        if not matches:
            raise ExperimentError(
                f"No experiment matches {identifier!r}",
                hint="Use an experiment id (`maya experiment list`).",
            )
        if len(matches) > 1:
            raise ExperimentError(
                f"{len(matches)} experiments are named {identifier!r}",
                hint="Pass the experiment id instead.",
            )
        return matches[0]

    def list(self, *, project_id: str | None = None, task_id: str | None = None) -> list[ExperimentDefinition]:
        return self.store.list_definitions(project_id=project_id, task_id=task_id)

    def definition_view(self, experiment_id: str) -> dict[str, Any]:
        definition = self.store.get_definition(experiment_id)
        runs = self.store.list_runs(experiment_id)
        comparisons = self.store.list_comparisons(candidate_experiment_id=experiment_id)
        return {
            "definition": definition.model_dump(mode="json"),
            "runs": [run.model_dump(mode="json") for run in runs],
            "comparisons": [item.model_dump(mode="json") for item in comparisons],
            "usable_seeds": sorted({run.seed for run in runs if run.usable}),
        }

    def plan(self, experiment_id: str) -> dict[str, Any]:
        definition = self.store.get_definition(experiment_id)
        blockers: list[str] = []
        if definition.status != ExperimentStatus.APPROVED:
            blockers.append(f"status is {definition.status.value}; run `maya experiment approve`")
        if definition.profile in {ExperimentProfile.FULL_EXPERIMENT, ExperimentProfile.LARGE_TRAINING}:
            blockers.append(
                f"{definition.profile.value} is not executed by the local Phase 5 runner"
            )
        # STATIC experiments are intentionally populated with `record`; that is
        # their execution mode, not a blocker. `run` still refuses them with a
        # hint, so a user cannot believe a command executed.
        runs = self.store.list_runs(definition.id)
        planned = [
            {
                "seed": seed,
                "attempt": self.store.next_attempt(definition.id, seed),
                "command": self.command_for(definition, seed),
            }
            for seed in definition.seeds
        ]
        values = protocol_values(
            definition,
            commit_sha=definition.base_commit_sha,
            patch_hash=self._snapshot(definition).patch_hash,
        )
        unknown = sorted(path for path, value in values.items() if value is None)
        limits = self.settings.experiments
        return {
            "definition": definition.model_dump(mode="json"),
            "blockers": blockers,
            "planned_runs": planned,
            "existing_runs": len(runs),
            "worst_case_seconds": len(planned) * definition.timeout_seconds,
            "budget": definition.budget.model_dump(mode="json"),
            "local_ceilings": {
                "max_runtime_seconds": limits.max_runtime_seconds,
                "max_total_runtime_seconds": limits.max_total_runtime_seconds,
                "max_runs": limits.max_runs,
            },
            "unknown_protocol_fields": unknown,
            "execution_mode": definition.profile.value,
            "reminder": (
                "Record externally produced metrics with `maya experiment record`."
                if definition.profile == _STATIC_PROFILE
                else (
                    "A command runs offline with OS resource limits in a fresh worktree "
                    f"at `{definition.base_commit_sha[:12]}`; it is not a security "
                    "sandbox."
                )
            ),
        }

    def command_for(self, definition: ExperimentDefinition, seed: int) -> list[str]:
        return [
            item.replace(TREATMENT_PLACEHOLDER, str(seed))
            if TREATMENT_PLACEHOLDER in item
            else item
            for item in definition.command
        ]

    def _snapshot(self, definition: ExperimentDefinition) -> RepositorySnapshot:
        return self.snapshots.get(definition.snapshot_id)

    def _repository(self, definition: ExperimentDefinition) -> Repository:
        return self.repositories.get(definition.repository_id)

    # -------------------------------------------------------------- execution
    def run(
        self,
        experiment_id: str,
        *,
        seeds: list[int] | None = None,
        keep_workspace: bool = False,
    ) -> list[ExperimentRun]:
        definition = self.store.get_definition(experiment_id)
        if definition.status != ExperimentStatus.APPROVED:
            raise ExperimentError(
                f"Experiment is {definition.status.value}; execution requires an approval",
                hint=f"Run `maya experiment approve {definition.id}` after reviewing `maya experiment plan {definition.id}`.",
            )
        if definition.profile in {ExperimentProfile.FULL_EXPERIMENT, ExperimentProfile.LARGE_TRAINING}:
            raise ExperimentError(
                f"{definition.profile.value} is not executed by the local Phase 5 runner",
                hint="Export the bundle and run it on user-owned compute, then use `maya experiment record`.",
            )
        if definition.profile == _STATIC_PROFILE:
            raise ExperimentError(
                "STATIC experiments do not execute separate commands",
                hint=f"Use `maya experiment record {definition.id} --seed <n> --metrics <file>`.",
            )
        requested = definition.seeds if seeds is None else list(seeds)
        if not requested:
            raise ExperimentError("No seeds were requested")
        unknown = sorted(set(requested) - set(definition.seeds))
        if unknown:
            raise ExperimentError(
                "Seeds were not pre-registered: " + ", ".join(str(item) for item in unknown),
                hint="Add them to a new definition; undeclared seeds are a p-hacking surface.",
            )
        for seed in requested:
            if self.store.next_attempt(definition.id, seed) > MAX_ATTEMPTS_PER_SEED:
                raise ExperimentError(
                    f"seed {seed} already has {MAX_ATTEMPTS_PER_SEED} attempts",
                    hint="Create a new pre-registration instead of retrying indefinitely.",
                )

        repository = self._repository(definition)
        snapshot = self._snapshot(definition)
        spent_ms = sum(run.duration_ms or 0 for run in self.store.list_runs(definition.id))
        planned_ms = len(requested) * definition.timeout_seconds * 1000
        if spent_ms + planned_ms > definition.budget.max_total_runtime_seconds * 1000:
            raise ExperimentError(
                "the requested runs cannot fit in the declared total runtime budget",
                hint=(
                    f"spent {spent_ms / 1000:.1f}s; remaining budget allows fewer seeds. "
                    "Create a new definition with a larger budget deliberately."
                ),
            )

        results: list[ExperimentRun] = []
        for seed in requested:
            attempt = self.store.next_attempt(definition.id, seed)
            manifest = self._create_manifest(definition, snapshot, seed, attempt)
            run = ExperimentRun(
                id="xrun_" + new_ulid(),
                experiment_id=definition.id,
                project_id=definition.project_id,
                task_id=definition.task_id,
                manifest_id=manifest.id,
                seed=seed,
                attempt=attempt,
                status=ExperimentRunStatus.PENDING,
                command=self.command_for(definition, seed),
                details={
                    "declared_command": list(definition.command),
                    "manifest_sha256": manifest.manifest_sha256,
                },
            )
            self.store.create_run(run)
            self.store.update_run(run.id, status=ExperimentRunStatus.RUNNING.value)
            self._event(
                EventType.EXPERIMENT_RUN_STARTED,
                definition,
                entity_type="experiment_run",
                entity_id=run.id,
                actor=ActorKind.TOOL,
                payload={
                    "seed": seed,
                    "attempt": attempt,
                    "manifest_id": manifest.id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "command": run.command,
                },
            )
            try:
                execution = self.runner.execute(
                    definition=definition,
                    run_id=run.id,
                    attempt=attempt,
                    seed=seed,
                    repository=repository,
                    snapshot=snapshot,
                    keep_workspace=keep_workspace,
                )
            except Exception as exc:  # noqa: BLE001 - recorded, then surfaced
                message = f"{type(exc).__name__}: {exc}"
                self.store.update_run(
                    run.id,
                    status=ExperimentRunStatus.FAILED.value,
                    error=message[:4000],
                    termination_reason="RUNNER_ERROR",
                    completed_at=utcnow(),
                )
                self._event(
                    EventType.EXPERIMENT_RUN_FAILED,
                    definition,
                    entity_type="experiment_run",
                    entity_id=run.id,
                    actor=ActorKind.TOOL,
                    payload={"seed": seed, "error": message[:2000]},
                )
                results.append(self.store.get_run(run.id))
                break
            results.append(
                self._finalize_execution(
                    definition, run, execution, manifest
                )
            )
            if execution.status == ExperimentRunStatus.BLOCKED:
                break
        return results

    def _create_manifest(
        self,
        definition: ExperimentDefinition,
        snapshot: RepositorySnapshot,
        seed: int,
        attempt: int,
    ) -> ExperimentManifest:
        limits = {
            "timeout_seconds": definition.timeout_seconds,
            "max_output_bytes": self.settings.experiments.max_output_bytes,
            "max_artifact_bytes": definition.budget.max_artifact_bytes,
            "max_metric_observations": definition.budget.max_metric_observations,
            "max_total_runtime_seconds": definition.budget.max_total_runtime_seconds,
            "network_policy": "DENY_BY_DEFAULT",
            "dependency_install": "DENY",
            "profile": definition.profile.value,
            "executables": list(DEFAULT_EXECUTABLES),
        }
        declared_inputs = {
            "evaluation_script_path": definition.protocol.evaluation.script_path,
            "evaluation_script_hash": definition.protocol.evaluation.script_hash or None,
            "lockfile_path": definition.protocol.environment.lockfile_path,
            "lockfile_hash": definition.protocol.environment.lockfile_hash or None,
            "dataset_path": definition.protocol.dataset.path,
            "dataset_fingerprint": definition.protocol.dataset.fingerprint or None,
            "dataset_fingerprint_method": definition.protocol.dataset.fingerprint_method,
            "config_hash": definition.protocol.config.hash or None,
            "basis": "declared in the pre-registration; observed hashes are on the run",
        }
        environment = {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": _cpu_count(),
            "host_id": default_host_id(),
            "seed": seed,
            "seed_env": EXPERIMENT_SEED_ENV,
            "cwd": "isolated worktree",
            "model_calls": "none",
        }
        manifest = ExperimentManifest(
            id="xman_" + new_ulid(),
            experiment_id=definition.id,
            project_id=definition.project_id,
            snapshot_id=definition.snapshot_id,
            role=definition.role,
            seed=seed,
            attempt=attempt,
            definition_sha256=definition.content_sha256,
            repository_id=definition.repository_id,
            commit_sha=definition.base_commit_sha,
            snapshot_kind=snapshot.kind.value,
            includes_worktree_patch=snapshot.includes_worktree_patch,
            patch_hash=snapshot.patch_hash,
            protocol=definition.protocol,
            command=self.command_for(definition, seed),
            limits=limits,
            environment=environment,
            verified_inputs=declared_inputs,
            manifest_sha256="0" * 64,
        )
        payload = manifest.model_dump(
            mode="json",
            exclude={"id", "manifest_sha256", "created_at"},
        )
        manifest.manifest_sha256 = canonical_sha256(payload)
        self.store.create_manifest(manifest)
        self.artifacts.put_text(
            json.dumps(
                {**payload, "manifest_id": manifest.id, "manifest_sha256": manifest.manifest_sha256},
                indent=2,
                sort_keys=True,
            ),
            kind=ArtifactKind.EXPERIMENT_MANIFEST,
            media_type="application/json",
            created_by="experiment-manifest",
        )
        return manifest

    def _finalize_execution(
        self,
        definition: ExperimentDefinition,
        run: ExperimentRun,
        execution: Any,
        manifest: ExperimentManifest,
    ) -> ExperimentRun:
        output_artifact = None
        if execution.output_text:
            output_artifact = self.artifacts.put_text(
                execution.output_text,
                kind=ArtifactKind.EXPERIMENT_OUTPUT,
                media_type="text/plain; charset=utf-8",
                created_by="experiment-runner",
                retention=RetentionClass.PROJECT,
            )
        metrics_artifact = None
        observations: list[MetricObservation] = []
        metrics_error = execution.metrics_error
        if execution.metrics is not None:
            observations = build_observations(
                run_id=run.id,
                experiment_id=definition.id,
                project_id=definition.project_id,
                extracted=execution.metrics,
            )
            if execution.metrics.missing_declared and not metrics_error:
                metrics_error = (
                    "declared metrics were not produced: "
                    + ", ".join(execution.metrics.missing_declared)
                )
            if execution.metrics.undeclared:
                note = (
                    "undeclared metrics were recorded but are excluded from comparisons: "
                    + ", ".join(execution.metrics.undeclared)
                )
                execution.warnings.append(note)
            metrics_artifact = self.artifacts.put_text(
                json.dumps(
                    {
                        "run_id": run.id,
                        "source": execution.metrics.source,
                        "values": {
                            key: [
                                {"sample_index": index, "step": step, "value": value}
                                for index, step, value in values
                            ]
                            for key, values in execution.metrics.values.items()
                        },
                        "undeclared": execution.metrics.undeclared,
                        "missing_declared": execution.metrics.missing_declared,
                        "warnings": execution.metrics.warnings,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                kind=ArtifactKind.EXPERIMENT_METRICS,
                media_type="application/json",
                created_by="experiment-runner",
            )
        if observations:
            self.store.add_observations(run, observations)
        metrics_ingested = bool(observations) and metrics_error is None
        details = {
            "declared_command": list(definition.command),
            "manifest_sha256": manifest.manifest_sha256,
            "environment": execution.environment,
            "network_isolated": execution.network_isolated,
            "network_enforcement": execution.network_enforcement,
            "verified_inputs": execution.verified_inputs,
            "warnings": execution.warnings,
            "workspace_dirty": execution.workspace_dirty,
            "output_truncated": execution.output_truncated,
            "execution": "LOCAL",
            "metrics_source": definition.metrics_source.kind.value,
        }
        self.store.update_run(
            run.id,
            status=execution.status.value,
            exit_code=execution.exit_code,
            timed_out=execution.timed_out,
            blocked=execution.blocked,
            cancelled=execution.cancelled,
            duration_ms=execution.duration_ms,
            cpu_user_ms=execution.cpu_user_ms,
            cpu_system_ms=execution.cpu_system_ms,
            max_rss_kb=execution.max_rss_kb,
            output_artifact_id=output_artifact.id if output_artifact else None,
            metrics_artifact_id=metrics_artifact.id if metrics_artifact else None,
            metrics_ingested=metrics_ingested,
            observation_count=len(observations),
            metrics_error=metrics_error,
            error=execution.error,
            termination_reason=execution.termination_reason,
            workspace_kept=execution.workspace_kept,
            workspace_path=execution.workspace_path if execution.workspace_kept else None,
            completed_at=utcnow(),
            details=details,
        )
        stored = self.store.get_run(run.id)
        event_type = {
            ExperimentRunStatus.COMPLETED: EventType.EXPERIMENT_RUN_COMPLETED,
            ExperimentRunStatus.BLOCKED: EventType.EXPERIMENT_RUN_BLOCKED,
        }.get(execution.status, EventType.EXPERIMENT_RUN_FAILED)
        self._event(
            event_type,
            definition,
            entity_type="experiment_run",
            entity_id=run.id,
            actor=ActorKind.TOOL,
            payload={
                "seed": run.seed,
                "attempt": run.attempt,
                "status": execution.status.value,
                "exit_code": execution.exit_code,
                "duration_ms": execution.duration_ms,
                "observations": len(observations),
                "metrics_error": metrics_error,
                "error": execution.error,
                "manifest_sha256": manifest.manifest_sha256,
                "output_artifact_id": output_artifact.id if output_artifact else None,
            },
        )
        if observations:
            self._event(
                EventType.EXPERIMENT_METRICS_INGESTED,
                definition,
                entity_type="experiment_run",
                entity_id=run.id,
                actor=ActorKind.TOOL,
                payload={
                    "observations": len(observations),
                    "metrics": sorted({item.metric_key for item in observations}),
                    "metrics_artifact_id": metrics_artifact.id if metrics_artifact else None,
                },
            )
        return stored

    # --------------------------------------------------------- external runs
    def record_external(
        self,
        experiment_id: str,
        *,
        seed: int,
        metrics_text: str,
        note: str,
        executed_at: str | None = None,
    ) -> ExperimentRun:
        definition = self.store.get_definition(experiment_id)
        if definition.status != ExperimentStatus.APPROVED:
            raise ExperimentError(
                f"Experiment is {definition.status.value}; recording requires an approval"
            )
        if seed not in definition.seeds:
            raise ExperimentError(
                f"seed {seed} was not pre-registered for this experiment"
            )
        if not note.strip():
            raise ExperimentError(
                "a note is required: it is how a reader learns where and how the run executed"
            )
        if self.store.next_attempt(definition.id, seed) > MAX_ATTEMPTS_PER_SEED:
            raise ExperimentError(f"seed {seed} already has {MAX_ATTEMPTS_PER_SEED} attempts")
        size = len(metrics_text.encode("utf-8"))
        if size > definition.budget.max_artifact_bytes:
            raise ExperimentError(
                f"metrics document is {size} bytes, above the definition's artifact budget"
            )
        extracted = parse_imported_metrics(
            metrics_text,
            metrics=definition.metrics,
            max_observations=min(
                definition.budget.max_metric_observations,
                self.settings.experiments.max_metric_observations,
            ),
        )
        if extracted.missing_declared:
            raise ExperimentError(
                "imported metrics omit declared metrics: "
                + ", ".join(extracted.missing_declared),
                hint="A run missing its pre-registered metrics is not an observation.",
            )
        attempt = self.store.next_attempt(definition.id, seed)
        manifest = self._create_manifest(
            definition, self._snapshot(definition), seed, attempt
        )
        run = ExperimentRun(
            id="xrun_" + new_ulid(),
            experiment_id=definition.id,
            project_id=definition.project_id,
            task_id=definition.task_id,
            manifest_id=manifest.id,
            seed=seed,
            attempt=attempt,
            status=ExperimentRunStatus.COMPLETED,
            command=[],
            metrics_ingested=True,
            observation_count=extracted.total_observations,
            started_at=utcnow(),
            completed_at=utcnow(),
            details={
                "execution": "IMPORTED",
                "external_note": note.strip(),
                "executed_at": executed_at,
                "declared_command": list(definition.command),
                "manifest_sha256": manifest.manifest_sha256,
                "warnings": extracted.warnings,
            },
        )
        artifact = self.artifacts.put_text(
            metrics_text,
            kind=ArtifactKind.EXPERIMENT_METRICS,
            media_type="application/json",
            created_by="experiment-import",
        )
        run.metrics_artifact_id = artifact.id
        observations = build_observations(
            run_id=run.id,
            experiment_id=definition.id,
            project_id=definition.project_id,
            extracted=extracted,
        )
        self.store.create_run(run)
        if observations:
            self.store.add_observations(run, observations)
        self.store.update_run(
            run.id,
            metrics_artifact_id=artifact.id,
            observation_count=len(observations),
        )
        self._event(
            EventType.EXPERIMENT_RUN_COMPLETED,
            definition,
            entity_type="experiment_run",
            entity_id=run.id,
            actor=ActorKind.USER,
            payload={
                "seed": seed,
                "attempt": attempt,
                "execution": "IMPORTED",
                "note": note.strip(),
                "observations": len(observations),
                "metrics_artifact_id": artifact.id,
            },
        )
        if observations:
            self._event(
                EventType.EXPERIMENT_METRICS_INGESTED,
                definition,
                entity_type="experiment_run",
                entity_id=run.id,
                actor=ActorKind.USER,
                payload={"observations": len(observations), "execution": "IMPORTED"},
            )
        return self.store.get_run(run.id)

    def ingest(self, run_id: str, *, metrics_text: str) -> ExperimentRun:
        run = self.store.get_run(run_id)
        if run.metrics_ingested:
            raise ExperimentError(
                f"Run {run.id} already has {run.observation_count} observations",
                hint="Metrics are append-only; start a new attempt instead of overwriting evidence.",
            )
        if run.status != ExperimentRunStatus.COMPLETED:
            raise ExperimentError(
                f"Run {run.id} is {run.status.value}; only a completed command can be "
                "repaired with imported metrics",
                hint="Use `maya experiment record` for a run produced elsewhere.",
            )
        if run.observation_count:
            raise ExperimentError(
                f"Run {run.id} already recorded {run.observation_count} partial observations",
                hint=(
                    "Evidence is append-only and observation ids are deterministic; start "
                    "a new attempt instead of mixing metric sources."
                ),
            )
        definition = self.store.get_definition(run.experiment_id)
        size = len(metrics_text.encode("utf-8"))
        if size > min(
            definition.budget.max_artifact_bytes,
            self.settings.experiments.max_artifact_bytes,
        ):
            raise ExperimentError("metrics document exceeds the artifact budget")
        extracted = parse_imported_metrics(
            metrics_text,
            metrics=definition.metrics,
            max_observations=min(
                definition.budget.max_metric_observations,
                self.settings.experiments.max_metric_observations,
            ),
        )
        if extracted.missing_declared:
            raise ExperimentError(
                "imported metrics omit declared metrics: "
                + ", ".join(extracted.missing_declared),
                hint="A run missing its pre-registered metrics is not an observation.",
            )
        artifact = self.artifacts.put_text(
            metrics_text,
            kind=ArtifactKind.EXPERIMENT_METRICS,
            media_type="application/json",
            created_by="experiment-ingest",
        )
        observations = build_observations(
            run_id=run.id,
            experiment_id=definition.id,
            project_id=definition.project_id,
            extracted=extracted,
        )
        if observations:
            self.store.add_observations(run, observations)
        self.store.update_run(
            run.id,
            metrics_artifact_id=artifact.id,
            observation_count=len(observations),
            metrics_ingested=bool(observations),
            metrics_error=None,
            details={
                **run.details,
                "metrics_source": MetricsSourceKind.IMPORTED.value,
                "ingest_warnings": extracted.warnings,
            },
        )
        self._event(
            EventType.EXPERIMENT_METRICS_INGESTED,
            definition,
            entity_type="experiment_run",
            entity_id=run.id,
            actor=ActorKind.USER,
            payload={
                "observations": len(observations),
                "execution": "INGESTED",
                "metrics_artifact_id": artifact.id,
            },
        )
        return self.store.get_run(run.id)

    # ------------------------------------------------------------ comparison
    def compare(
        self,
        candidate_id: str,
        *,
        baseline_id: str,
        remember: bool = False,
    ) -> ExperimentComparison:
        baseline = self.store.get_definition(baseline_id)
        candidate = self.store.get_definition(candidate_id)
        if baseline.project_id != candidate.project_id:
            raise ExperimentError("Both experiments must belong to the same project")
        if baseline.id == candidate.id:
            raise ExperimentError("An experiment cannot be compared with itself")
        baseline_runs = self.store.latest_runs_by_seed(baseline.id)
        candidate_runs = self.store.latest_runs_by_seed(candidate.id)
        run_ids = [
            run.id for run in [*baseline_runs.values(), *candidate_runs.values()]
        ]
        observations: dict[str, list[MetricObservation]] = {}
        for run_id in run_ids:
            observations[run_id] = self.store.observations_for_run(run_id)
        baseline_snapshot = self._snapshot(baseline)
        candidate_snapshot = self._snapshot(candidate)
        outcome = build_comparison(
            baseline=baseline,
            candidate=candidate,
            baseline_runs=baseline_runs,
            candidate_runs=candidate_runs,
            observations=observations,
            baseline_patch_hash=baseline_snapshot.patch_hash,
            candidate_patch_hash=candidate_snapshot.patch_hash,
        )
        imported = [
            run
            for run in [*baseline_runs.values(), *candidate_runs.values()]
            if run.details.get("execution") == "IMPORTED"
        ]
        if imported:
            outcome.limitations.append(
                f"{len(imported)} run(s) were produced outside this workbench (imported "
                "metrics); their execution environment was not verified here"
            )
        if not (
            baseline.role == ExperimentRole.BASELINE
            and candidate.role == ExperimentRole.CANDIDATE
        ):
            outcome.limitations.append(
                "the roles are not the conventional BASELINE/CANDIDATE pair; the verdict "
                "still uses the declared primary metric and direction"
            )
        if baseline.non_inferiority_margin is not None or candidate.non_inferiority_margin is not None:
            if baseline.non_inferiority_margin != candidate.non_inferiority_margin:
                outcome.limitations.append(
                    "the two arms declare different non-inferiority margins; the candidate's "
                    "margin was used for the primary metric"
                )

        comparison_id = "xcmp_" + new_ulid()
        runs = [*baseline_runs.values(), *candidate_runs.values()]
        manifests = [
            self.store.get_manifest(run.manifest_id)
            for run in runs
            if run.manifest_id
        ]
        payload = {
            "schema_version": EXPERIMENT_PROTOCOL_VERSION,
            "comparison_id": comparison_id,
            "project_id": baseline.project_id,
            "task_id": candidate.task_id or baseline.task_id,
            "baseline": baseline.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "baseline_snapshot": baseline_snapshot.model_dump(mode="json"),
            "candidate_snapshot": candidate_snapshot.model_dump(mode="json"),
            "baseline_runs": [
                run.model_dump(mode="json") for run in baseline_runs.values()
            ],
            "candidate_runs": [
                run.model_dump(mode="json") for run in candidate_runs.values()
            ],
            "manifests": [item.model_dump(mode="json") for item in manifests],
            "design": outcome.design.value,
            "comparable": outcome.comparable,
            "verdict": outcome.verdict.value,
            "findings": [item.model_dump(mode="json") for item in outcome.findings],
            "metrics": [item.model_dump(mode="json") for item in outcome.metrics],
            "limitations": outcome.limitations,
            "details": outcome.details,
            "generated_at": utcnow().isoformat(),
        }
        json_sha = canonical_sha256(payload)
        comparison = ExperimentComparison(
            id=comparison_id,
            project_id=baseline.project_id,
            task_id=candidate.task_id or baseline.task_id,
            baseline_experiment_id=baseline.id,
            candidate_experiment_id=candidate.id,
            baseline_definition_sha256=baseline.content_sha256,
            candidate_definition_sha256=candidate.content_sha256,
            verdict=outcome.verdict,
            design=outcome.design,
            comparable=outcome.comparable,
            findings=outcome.findings,
            metrics=outcome.metrics,
            baseline_run_ids=outcome.baseline_run_ids,
            candidate_run_ids=outcome.candidate_run_ids,
            limitations=outcome.limitations,
            json_sha256=json_sha,
            details={**outcome.details, "run_summary": _run_summary(runs)},
        )
        markdown = experiment_report.render_comparison_markdown(
            comparison,
            baseline=baseline,
            candidate=candidate,
            runs=runs,
            manifests=manifests,
        )
        json_artifact = self.artifacts.put_text(
            json.dumps({**payload, "json_sha256": json_sha}, indent=2, sort_keys=True),
            kind=ArtifactKind.EXPERIMENT_REPORT,
            media_type="application/json",
            created_by="experiment-compare",
        )
        markdown_artifact = self.artifacts.put_text(
            markdown,
            kind=ArtifactKind.EXPERIMENT_REPORT,
            media_type="text/markdown; charset=utf-8",
            created_by="experiment-compare",
        )
        comparison.report_artifact_id = markdown_artifact.id
        comparison.details["json_artifact_id"] = json_artifact.id
        comparison.details["markdown_artifact_id"] = markdown_artifact.id
        self.store.create_comparison(comparison)
        self._event(
            EventType.EXPERIMENT_COMPARED,
            candidate,
            entity_type="experiment_comparison",
            entity_id=comparison.id,
            actor=ActorKind.TOOL,
            payload={
                "baseline_experiment_id": baseline.id,
                "candidate_experiment_id": candidate.id,
                "verdict": comparison.verdict.value,
                "comparable": comparison.comparable,
                "design": comparison.design.value,
                "json_sha256": json_sha,
                "limitations": comparison.limitations[:20],
            },
        )
        if remember:
            self.remember_comparison(comparison)
        return comparison

    def remember_comparison(self, comparison: ExperimentComparison) -> ProjectMemory:
        """Record a comparison outcome as durable project memory.

        A negative result is scoped to this protocol and repository revision; it
        is never a general prohibition. Called only on explicit request
        (``--remember``) or from the comparison that the user reviewed.
        """
        if self.memory is None:
            raise ExperimentError("Project memory is not available")
        candidate = self.store.get_definition(comparison.candidate_experiment_id)
        baseline = self.store.get_definition(comparison.baseline_experiment_id)
        negative = comparison.verdict in {
            ExperimentVerdict.NO_DETECTABLE_DIFFERENCE,
            ExperimentVerdict.BASELINE_ADVANTAGE,
            ExperimentVerdict.NOT_COMPARABLE,
            ExperimentVerdict.INCONCLUSIVE,
        }
        primary = next((item for item in comparison.metrics if item.primary), None)
        statement = (
            f"Comparing {candidate.name!r} (candidate) with {baseline.name!r} (baseline) "
            f"at commit {candidate.base_commit_sha[:12]} produced {comparison.verdict.value}"
            + (
                f" on the primary metric {primary.key}: baseline mean {primary.baseline_mean}, "
                f"candidate mean {primary.candidate_mean}, difference {primary.difference} "
                f"(95% CI {primary.ci_low} to {primary.ci_high}, n="
                f"{len(primary.candidate_values)}/{len(primary.baseline_values)})."
                if primary
                else "."
            )
            + " This is scoped to the recorded protocol and revision; it is not a general "
            "statement about the method."
        )
        return self.memory.add(
            project_id=comparison.project_id,
            kind=MemoryKind.NEGATIVE_RESULT if negative else MemoryKind.EXPERIMENT_RESULT,
            title=f"Experiment: {candidate.name} vs {baseline.name} — {comparison.verdict.value}",
            statement=statement,
            author_kind=MemoryAuthor.SYSTEM,
            status=MemoryStatus.UNCONFIRMED,
            scope=f"experiment-comparison:{comparison.id}",
            repository_id=candidate.repository_id,
            snapshot_id=candidate.snapshot_id,
            commit_sha=candidate.base_commit_sha,
            confidence="MEDIUM",
            sources=[
                MemorySource(
                    kind="EXPERIMENT_COMPARISON",
                    ref=comparison.id,
                    sha256=comparison.json_sha256,
                    note="persisted comparison payload",
                ),
                MemorySource(
                    kind="EXPERIMENT",
                    ref=candidate.id,
                    sha256=candidate.content_sha256,
                    note="candidate pre-registration",
                ),
                MemorySource(
                    kind="EXPERIMENT",
                    ref=baseline.id,
                    sha256=baseline.content_sha256,
                    note="baseline pre-registration",
                ),
            ],
            details={"verdict": comparison.verdict.value},
        )

    def latest_comparison_for_task(self, task_id: str) -> ExperimentComparison | None:
        return self.store.latest_comparison_for_task(task_id)

    def comparison_markdown(self, comparison_id: str) -> str:
        comparison = self.store.get_comparison(comparison_id)
        if not comparison.report_artifact_id:
            raise ExperimentError("The comparison has no Markdown report artifact")
        return self.artifacts.read_text(comparison.report_artifact_id)

    # ---------------------------------------------------------------- bundles
    def bundle(self, identifier: str, destination: str | Path) -> Any:
        from mayaworkbench.experiments.bundle import write_experiment_bundle

        if identifier.startswith("xcmp_"):
            comparison = self.store.get_comparison(identifier)
            documents, subject, gaps = self._comparison_bundle(comparison)
            return write_experiment_bundle(
                destination,
                kind="COMPARISON",
                subject=subject,
                documents=documents,
                gaps=gaps,
            )
        definition = self.resolve(identifier)
        documents, subject, gaps = self._experiment_bundle(definition)
        return write_experiment_bundle(
            destination,
            kind="EXPERIMENT",
            subject=subject,
            documents=documents,
            gaps=gaps,
        )

    def export_run_bundle(
        self,
        identifier: str,
        destination: str | Path,
        *,
        attestation: dict[str, Any] | None = None,
    ) -> Any:
        """Export one run, or a comparison's two arms, as a portable archive.

        Unlike ``bundle()`` this produces a single deterministic gzip-tar that a
        user can carry to compute this workbench does not control. The archive
        carries exact commands, declared seeds, commit and patch hashes,
        environment locks, a dataset fingerprint, expected output hashes and
        hardware metadata. Nothing here claims that a remote runner is trusted.
        """
        from mayaworkbench.experiments.bundle import (
            compute_dataset_fingerprint,
            write_run_bundle,
        )

        if identifier.startswith("xcmp_"):
            comparison = self.store.get_comparison(identifier)
            definitions = [
                self.store.get_definition(comparison.baseline_experiment_id),
                self.store.get_definition(comparison.candidate_experiment_id),
            ]
            runs = [
                *self.store.list_runs(definitions[0].id),
                *self.store.list_runs(definitions[1].id),
            ]
            json_artifact_id = comparison.details.get("json_artifact_id")
            comparison_payload = (
                json.loads(self.artifacts.read_text(json_artifact_id))
                if json_artifact_id
                else comparison.model_dump(mode="json")
            )
            definition_doc: dict[str, Any] = {
                "kind": "COMPARISON",
                "comparison_id": comparison.id,
                "comparison_sha256": comparison.json_sha256,
                "comparison": comparison_payload,
                "arms": [self._definition_document(item) for item in definitions],
            }
        else:
            definition = self.resolve(identifier)
            definitions = [definition]
            runs = self.store.list_runs(definition.id)
            definition_doc = {
                **self._definition_document(definition),
                "kind": "SINGLE",
            }
        definition_doc["declared_seeds"] = {
            item.id: list(item.seeds) for item in definitions
        }
        definition_doc["command_templates"] = {
            item.id: {
                "command": list(item.command),
                "timeout_seconds": item.timeout_seconds,
                "profile": item.profile.value,
                "metrics_source": item.metrics_source.kind.value,
            }
            for item in definitions
        }
        definition_doc["commit_sha_by_arm"] = {
            item.id: item.base_commit_sha for item in definitions
        }

        run_payloads, observations, documents, manifests = self._run_documents(runs)
        manifest_docs = [
            json.loads(documents[f"manifests/{manifest['id']}.json"].decode("utf-8"))
            for manifest in manifests
        ]
        outputs = {
            Path(name).name: data
            for name, data in documents.items()
            if name.startswith("outputs/")
        }
        environment: dict[str, Any] = {}
        for manifest in manifest_docs:
            for key, value in (manifest.get("environment") or {}).items():
                environment.setdefault(key, value)
        environment["bundle_export"] = "portable runner bundle; execution is user-owned"

        if len(definitions) == 1:
            hardware: dict[str, Any] = definitions[0].protocol.hardware.model_dump(
                mode="json"
            )
        else:
            hardware = {
                "note": "arms may declare different hardware; see the per-arm records",
                "arms": {
                    item.id: item.protocol.hardware.model_dump(mode="json")
                    for item in definitions
                },
            }
        inputs: dict[str, str] = {}
        for item in definitions:
            protocol = item.protocol
            declared = {
                "dataset": protocol.dataset.fingerprint,
                "evaluation_script": protocol.evaluation.script_hash,
                "lockfile": protocol.environment.lockfile_hash,
                "config": protocol.config.hash,
            }
            for key, digest in declared.items():
                if digest and re.fullmatch(r"[0-9a-f]{64}", digest):
                    inputs[f"{item.id}/{key}"] = digest
        dataset_fingerprint = compute_dataset_fingerprint(inputs)
        gaps = self._reproduction_gaps(definitions, runs)
        gaps.append(
            "results are not locally attested evidence: a signature identifies a "
            "key, it does not prove the scientific result"
        )
        context = self._runner_bundle_context(definitions)
        return write_run_bundle(
            destination,
            definition_doc,
            manifest_docs,
            run_payloads,
            observations,
            outputs,
            environment,
            dataset_fingerprint,
            hardware,
            gaps=gaps,
            attestation=attestation,
            context=context,
        )

    def import_run_bundle(self, path: str | Path) -> Any:
        """Import verified bundle results as IMPORTED, never as local evidence."""
        from mayaworkbench.experiments.bundle import (
            import_run_bundle as import_bundle_records,
        )

        result = import_bundle_records(path, self.store)
        definitions = {item.id: item for item in result.definitions}
        for run in result.runs:
            if run.id not in result.created_run_ids:
                continue
            definition = definitions.get(run.experiment_id)
            if definition is None:
                continue
            self._event(
                EventType.EXPERIMENT_RUN_COMPLETED,
                definition,
                entity_type="experiment_run",
                entity_id=run.id,
                actor=ActorKind.USER,
                payload={
                    "seed": run.seed,
                    "attempt": run.attempt,
                    "execution": "IMPORTED",
                    "attestation": result.attestation,
                    "observations": run.observation_count,
                    "bundle_sha256": result.archive_sha256,
                    "limitation": result.limitation,
                },
            )
        return result

    def _runner_bundle_context(
        self, definitions: list[ExperimentDefinition]
    ) -> dict[str, Any]:
        """Carry immutable project/repository/snapshot metadata for import.

        A portable bundle lands in a home that has none of these rows; without
        them the strict foreign keys on experiment records reject the import.
        """
        projects: dict[str, Any] = {}
        repositories: dict[str, Any] = {}
        snapshots: dict[str, Any] = {}
        for definition in definitions:
            repository = self.repositories.get(definition.repository_id)
            snapshot = self.snapshots.get(definition.snapshot_id)
            if definition.project_id not in projects:
                slug = "imported-" + "".join(
                    char for char in definition.project_id.lower() if char.isalnum()
                )[-20:]
                projects[definition.project_id] = Project(
                    id=definition.project_id,
                    name=repository.name,
                    slug=slug,
                    description="Recreated from a portable runner bundle",
                    # Deterministic: a synthetic project must not make repeated
                    # exports of the same evidence differ byte-for-byte.
                    created_at=repository.created_at,
                    updated_at=repository.created_at,
                ).model_dump(mode="json")
            if repository.id not in repositories:
                repositories[repository.id] = repository.model_dump(mode="json")
            if snapshot.id not in snapshots:
                snapshots[snapshot.id] = snapshot.model_dump(mode="json")
        return {
            "projects": list(projects.values()),
            "repositories": list(repositories.values()),
            "snapshots": list(snapshots.values()),
        }

    def _definition_document(self, definition: ExperimentDefinition) -> dict[str, Any]:
        artifact_id = definition.details.get("protocol_artifact_id")
        if not artifact_id:
            raise ExperimentError(
                f"Experiment {definition.id} has no stored pre-registration payload",
                hint="Only definitions created by `maya experiment create` can be bundled.",
            )
        stored = json.loads(self.artifacts.read_text(artifact_id))
        content_payload = {
            key: value
            for key, value in stored.items()
            if key not in {"experiment_id", "content_sha256"}
        }
        if canonical_sha256(content_payload) != definition.content_sha256:
            raise ExperimentError(
                f"Experiment {definition.id} pre-registration payload no longer matches its hash"
            )
        return {
            "definition": definition.model_dump(mode="json"),
            "content_payload": content_payload,
            "content_sha256": definition.content_sha256,
            "protocol_artifact_id": artifact_id,
        }

    def _run_documents(
        self, runs: list[ExperimentRun]
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, bytes], list[dict[str, Any]]]:
        documents: dict[str, bytes] = {}
        manifests: list[dict[str, Any]] = []
        observations: dict[str, Any] = {}
        for run in sorted(runs, key=lambda item: (item.experiment_id, item.seed, item.attempt)):
            if run.manifest_id:
                manifest = self.store.get_manifest(run.manifest_id)
                manifests.append(manifest.model_dump(mode="json"))
                documents[f"manifests/{manifest.id}.json"] = _json_bytes(
                    manifest.model_dump(mode="json")
                )
            rows = self.store.observations_for_run(run.id)
            observations[run.id] = [
                {
                    "metric_key": item.metric_key,
                    "value": item.value,
                    "sample_index": item.sample_index,
                    "step": item.step,
                    "source": item.source,
                }
                for item in rows
            ]
            if run.output_artifact_id:
                try:
                    output = self.artifacts.read_bytes(run.output_artifact_id)
                except Exception:  # noqa: BLE001 - a missing output is a gap, not a crash
                    output = b""
                if output and len(output) <= self.settings.experiments.max_output_bytes:
                    documents[f"outputs/{run.id}.txt"] = output
        return (
            [run.model_dump(mode="json") for run in runs],
            observations,
            documents,
            manifests,
        )

    def _reproduction_gaps(
        self,
        definitions: list[ExperimentDefinition],
        runs: list[ExperimentRun],
    ) -> list[str]:
        gaps: list[str] = []
        unknown_by_arm: dict[str, list[str]] = {}
        for definition in definitions:
            values = protocol_values(
                definition,
                commit_sha=definition.base_commit_sha,
                patch_hash=self._snapshot(definition).patch_hash,
            )
            unknown_by_arm[definition.id] = sorted(
                key for key, value in values.items() if value is None
            )
        unknown_sets = {tuple(paths) for paths in unknown_by_arm.values()}
        if len(unknown_sets) == 1:
            shared = next(iter(unknown_sets), ())
            if shared:
                scope = "both arms" if len(definitions) > 1 else "this experiment"
                verb = "leave" if len(definitions) > 1 else "leaves"
                gaps.append(
                    f"{scope} {verb} these protocol fields unknown, so a reproducer "
                    "cannot verify them: " + ", ".join(shared)
                )
        else:
            for definition_id, paths in unknown_by_arm.items():
                if paths:
                    gaps.append(
                        f"{definition_id} leaves these protocol fields unknown: "
                        + ", ".join(paths)
                    )
        if all(not item.protocol.evaluation.script_hash for item in definitions):
            gaps.append("no evaluation-script hash was declared")
        else:
            for item in definitions:
                if not item.protocol.evaluation.script_hash:
                    gaps.append(f"{item.id}: no evaluation-script hash was declared")
        if all(
            not item.protocol.environment.lockfile_hash for item in definitions
        ):
            gaps.append("no dependency lockfile hash was declared")
        else:
            for item in definitions:
                if not item.protocol.environment.lockfile_hash:
                    gaps.append(
                        f"{item.id}: no dependency lockfile hash was declared"
                    )
        imported = [run for run in runs if run.details.get("execution") == "IMPORTED"]
        for run in imported:
            gaps.append(
                f"run {run.id} was produced outside this workbench; its environment "
                f"was not verified here ({run.details.get('external_note', '')})"
            )
        snapshots = {definition.snapshot_id: self._snapshot(definition) for definition in definitions}
        for snapshot in snapshots.values():
            if snapshot.includes_worktree_patch and not snapshot.patch_artifact_id:
                gaps.append(
                    f"snapshot {snapshot.id} includes a working-tree patch but no patch artifact"
                )
        return gaps

    def _reproduce_document(
        self,
        *,
        definitions: list[ExperimentDefinition],
        runs: list[ExperimentRun],
        gaps: list[str],
        patch_paths: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        commits = {
            definition.id: definition.base_commit_sha for definition in definitions
        }
        patches = {
            definition.id: self._snapshot(definition).patch_hash
            for definition in definitions
        }
        single_commit = len(set(commits.values())) == 1
        return {
            "schema_version": EXPERIMENT_PROTOCOL_VERSION,
            # A comparison can span two commits. ``commit_sha`` is only set when
            # every arm used the same revision; ``commits`` is authoritative.
            "commit_sha": next(iter(commits.values())) if single_commit else None,
            "commits": commits,
            "patch_hashes": patches,
            "patch_paths": patch_paths
            if patch_paths is not None
            else {
                definition.id: ("patch.diff" if patches[definition.id] else None)
                for definition in definitions
            },
            "seeds": {
                definition.id: definition.seeds for definition in definitions
            },
            "commands": [
                {
                    "experiment_id": definition.id,
                    "commit_sha": definition.base_commit_sha,
                    "seed": seed,
                    "argv": self.command_for(definition, seed),
                    "timeout_seconds": definition.timeout_seconds,
                    "metrics_source": definition.metrics_source.kind.value,
                    "metrics_path": definition.metrics_source.path,
                }
                for definition in definitions
                for seed in definition.seeds
            ],
            "recorded_models": "none (the Experiment Lab never calls a model)",
            "prerequisites": [
                "the same Python major/minor version and framework versions",
                "the same repository commit (and the bundled patch, when present)",
                "the same declared hardware class; CPU count and OS are recorded",
                "offline execution: no network is available to the command",
            ],
            "gaps": gaps,
            "verify": "maya experiment verify-bundle <this directory>",
        }

    def _bundle_experiment(
        self, definition: ExperimentDefinition
    ) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
        runs = self.store.list_runs(definition.id)
        run_payloads, observations, documents, _manifests = self._run_documents(runs)
        documents["definition.json"] = _json_bytes(self._definition_document(definition))
        documents["runs.json"] = _json_bytes(run_payloads)
        documents["observations.json"] = _json_bytes(observations)
        snapshot = self._snapshot(definition)
        patch_paths: dict[str, str | None] = {definition.id: None}
        if snapshot.patch_artifact_id:
            try:
                documents["patch.diff"] = self.artifacts.read_bytes(
                    snapshot.patch_artifact_id
                )
                patch_paths[definition.id] = "patch.diff"
            except Exception:  # noqa: BLE001 - recorded as a gap below
                pass
        comparisons = self.store.list_comparisons(candidate_experiment_id=definition.id)
        if comparisons:
            documents["comparisons.json"] = _json_bytes(
                [item.model_dump(mode="json") for item in comparisons]
            )
        gaps = self._reproduction_gaps([definition], runs)
        if snapshot.patch_artifact_id and patch_paths[definition.id] is None:
            gaps.append(
                f"the working-tree patch for snapshot {snapshot.id} could not be read; "
                "the bundle does not contain it"
            )
        reproduce = self._reproduce_document(
            definitions=[definition],
            runs=runs,
            gaps=gaps,
            patch_paths=patch_paths,
        )
        documents["reproduce.json"] = _json_bytes(reproduce)
        documents["README.md"] = _bundle_readme(
            title=f"Experiment: {definition.name}",
            definition_lines=[
                f"Pre-registration: `{definition.id}` (sha256 `{definition.content_sha256}`)",
                f"Role: `{definition.role.value}`; profile: `{definition.profile.value}`",
                f"Commit: `{definition.base_commit_sha}`",
                f"Seeds: {', '.join(str(seed) for seed in definition.seeds)}",
                f"Status: `{definition.status.value}`",
            ],
            gaps=gaps,
            extra=[
                "Each `manifests/*.json` is the immutable pre-run provenance record and "
                "is hashed before the command starts.",
                "`observations.json` holds raw values; replicate aggregation is declared "
                "on the metric and applied at comparison time.",
            ],
        )
        subject = {
            "experiment_id": definition.id,
            "content_sha256": definition.content_sha256,
            "commit_sha": definition.base_commit_sha,
            "runs": len(runs),
        }
        return documents, subject, gaps

    def _comparison_bundle(
        self, comparison: ExperimentComparison
    ) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
        baseline = self.store.get_definition(comparison.baseline_experiment_id)
        candidate = self.store.get_definition(comparison.candidate_experiment_id)
        runs = [
            *self.store.list_runs(baseline.id),
            *self.store.list_runs(candidate.id),
        ]
        run_payloads, observations, documents, _manifests = self._run_documents(runs)
        documents["baseline-definition.json"] = _json_bytes(
            self._definition_document(baseline)
        )
        documents["candidate-definition.json"] = _json_bytes(
            self._definition_document(candidate)
        )
        documents["runs.json"] = _json_bytes(run_payloads)
        documents["observations.json"] = _json_bytes(observations)
        documents["report.md"] = self.comparison_markdown(comparison.id).encode("utf-8")
        comparison_json = None
        json_artifact_id = comparison.details.get("json_artifact_id")
        if json_artifact_id:
            comparison_json = self.artifacts.read_bytes(json_artifact_id)
            documents["comparison.json"] = comparison_json
        else:
            payload = comparison.model_dump(mode="json")
            payload["json_sha256"] = comparison.json_sha256
            documents["comparison.json"] = _json_bytes(payload)
        patch_paths: dict[str, str | None] = {}
        for definition in (baseline, candidate):
            snapshot = self._snapshot(definition)
            patch_paths[definition.id] = None
            if not snapshot.patch_artifact_id:
                continue
            try:
                data = self.artifacts.read_bytes(snapshot.patch_artifact_id)
            except Exception:  # noqa: BLE001 - recorded as a gap below
                continue
            path = f"patches/{definition.id}.diff"
            documents[path] = data
            patch_paths[definition.id] = path
        gaps = self._reproduction_gaps([baseline, candidate], runs)
        for definition in (baseline, candidate):
            snapshot = self._snapshot(definition)
            if snapshot.patch_artifact_id and patch_paths[definition.id] is None:
                gaps.append(
                    f"the working-tree patch for snapshot {snapshot.id} could not be "
                    "read; the bundle does not contain it"
                )
        reproduce = self._reproduce_document(
            definitions=[baseline, candidate],
            runs=runs,
            gaps=gaps,
            patch_paths=patch_paths,
        )
        documents["reproduce.json"] = _json_bytes(reproduce)
        documents["README.md"] = _bundle_readme(
            title=f"Experiment comparison: {candidate.name} vs {baseline.name}",
            definition_lines=[
                f"Verdict: `{comparison.verdict.value}`; comparable: "
                f"`{'yes' if comparison.comparable else 'no'}`; design: `{comparison.design.value}`",
                f"Baseline: `{baseline.id}` (sha256 `{baseline.content_sha256}`)",
                f"Candidate: `{candidate.id}` (sha256 `{candidate.content_sha256}`)",
                f"Baseline commit: `{baseline.base_commit_sha}`",
                f"Candidate commit: `{candidate.base_commit_sha}`",
                f"Comparison sha256: `{comparison.json_sha256}`",
            ],
            gaps=gaps,
            extra=[
                "`report.md` explains the statistics, the protocol diff and what the "
                "verdict does not establish.",
                "`comparison.json` is the exact payload the comparison hash covers.",
            ],
        )
        subject = {
            "comparison_id": comparison.id,
            "baseline_experiment_id": baseline.id,
            "candidate_experiment_id": candidate.id,
            "commit_sha": candidate.base_commit_sha,
            "comparison_sha256": comparison.json_sha256,
            "runs": len(runs),
        }
        return documents, subject, gaps

    def collect_for_verification(self, task_id: str) -> tuple[dict[str, bytes], dict[str, Any] | None]:
        """Compact experiment evidence for a Phase 4 verification bundle."""
        comparison = self.store.latest_comparison_for_task(task_id)
        if comparison is None:
            return {}, None
        baseline = self.store.get_definition(comparison.baseline_experiment_id)
        candidate = self.store.get_definition(comparison.candidate_experiment_id)
        runs = [
            *self.store.list_runs(baseline.id),
            *self.store.list_runs(candidate.id),
        ]
        run_payloads, observations, _documents, manifests = self._run_documents(runs)
        payload = {
            "schema_version": EXPERIMENT_PROTOCOL_VERSION,
            "comparison": comparison.model_dump(mode="json"),
            "baseline": self._definition_document(baseline)["definition"],
            "candidate": self._definition_document(candidate)["definition"],
            "runs": run_payloads,
            "manifests": manifests,
            "observations": observations,
            "statistics": experiment_report.statistics_payload(comparison),
        }
        documents = {
            "experiments/comparison.json": _json_bytes(payload),
            "experiments/report.md": self.comparison_markdown(comparison.id).encode(
                "utf-8"
            ),
        }
        for run in runs:
            if run.manifest_id:
                manifest = self.store.get_manifest(run.manifest_id)
                documents[f"experiments/manifests/{manifest.id}.json"] = _json_bytes(
                    manifest.model_dump(mode="json")
                )
        summary = {
            "comparison_id": comparison.id,
            "verdict": comparison.verdict.value,
            "comparable": comparison.comparable,
            "design": comparison.design.value,
            "json_sha256": comparison.json_sha256,
            "primary": next(
                (
                    item.model_dump(mode="json")
                    for item in comparison.metrics
                    if item.primary
                ),
                None,
            ),
            "limitations": comparison.limitations[:20],
            "bundle_files": sorted(documents),
        }
        return documents, summary

    # ----------------------------------------------------------------- events
    def _event(
        self,
        event_type: EventType,
        definition: ExperimentDefinition,
        *,
        entity_type: str,
        entity_id: str,
        actor: ActorKind,
        payload: dict[str, Any],
    ) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=definition.project_id,
                repository_id=definition.repository_id,
                snapshot_id=definition.snapshot_id,
                task_id=definition.task_id,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_kind=actor,
                actor_name="experiment",
                payload=payload,
            )
        )


def _validate_command(command: list[str]) -> None:
    if not isinstance(command, list) or not command:
        raise ExperimentError("command must be a non-empty argv list")
    if len(command) > 64:
        raise ExperimentError("command has too many arguments")
    for index, item in enumerate(command):
        if not isinstance(item, str) or not item:
            raise ExperimentError("command arguments must be non-empty strings")
        if "\x00" in item or "\n" in item or "\r" in item:
            raise ExperimentError("command arguments may not contain null bytes or newlines")
        if len(item) > 4096:
            raise ExperimentError("a command argument exceeds 4096 characters")
        if index > 0 and item.startswith(("/", "\\")):
            raise ExperimentError(
                f"absolute path argument is not allowed: {item}",
                hint="Use a workspace-relative path so the same command runs in any checkout.",
            )
        if ".." in Path(item).parts:
            raise ExperimentError(f"command argument may not contain '..': {item}")
    executable = Path(command[0]).name
    if executable not in DEFAULT_EXECUTABLES:
        raise ExperimentError(
            f"executable {executable!r} is not allowed",
            hint=f"Allowed: {', '.join(DEFAULT_EXECUTABLES)}",
        )
    if len(command) > 1 and command[0] in {"python", "python3"} and command[1] == "-c":
        raise ExperimentError(
            "inline python -c commands are not allowed",
            hint="Put the benchmark in a script so its hash is part of the protocol.",
        )
    if any(item in {"pip", "pip3", "poetry", "uv", "conda"} for item in command[1:3]):
        raise ExperimentError("dependency installation is not permitted in an experiment")


def _cpu_count() -> int | None:
    import os

    return os.cpu_count()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _bundle_readme(
    *,
    title: str,
    definition_lines: list[str],
    gaps: list[str],
    extra: list[str],
) -> bytes:
    lines = [
        f"# {title}",
        "",
        "This directory is an experiment reproduction bundle. Every file is hashed in "
        "`manifest.json`; verification recomputes the hashes and cross-checks the runs, "
        "manifests and observations.",
        "",
        "## What was recorded",
        "",
        *[f"- {line}" for line in definition_lines],
        "",
        *[f"- {line}" for line in extra],
        "",
        "## How to reproduce",
        "",
        "1. For each experiment, check out the commit recorded in `reproduce.json` "
        "(`commits`) and apply the patch at its `patch_paths` entry when that entry "
        "is not null.",
        "2. Use the Python/framework versions from each run manifest.",
        "3. Run the commands in `reproduce.json` with `MAYA_EXPERIMENT_SEED` set to the seed.",
        "4. Compare the metrics from the recorded contract.",
        "5. Verify the bundle: `maya experiment verify-bundle <directory>`.",
        "",
        "## Known gaps",
        "",
    ]
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append(
            "- None recorded. That means the recorded fields are known, not that the "
            "run is guaranteed to reproduce."
        )
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def _run_summary(runs: list[ExperimentRun]) -> list[dict[str, Any]]:
    return [
        {
            "id": run.id,
            "experiment_id": run.experiment_id,
            "seed": run.seed,
            "attempt": run.attempt,
            "status": run.status.value,
            "execution": run.details.get("execution", "LOCAL"),
            "usable": run.usable,
            "duration_ms": run.duration_ms,
            "observations": run.observation_count,
        }
        for run in sorted(runs, key=lambda item: (item.experiment_id, item.seed, item.attempt))
    ]


__all__ = ["ExperimentService", "MAX_ATTEMPTS_PER_SEED"]

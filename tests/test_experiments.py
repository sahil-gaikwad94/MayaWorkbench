"""End-to-end tests for the Phase 5 Experiment Lab.

These tests use the real service, real Git worktrees and real commands. The only
thing they do not control is whether the host permits a network namespace; that
case is tested separately and the execution tests skip with an explicit reason.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mayaworkbench.app.container import Services
from mayaworkbench.domain.enums import (
    ExperimentProfile,
    ExperimentRole,
    ExperimentStatus,
    ExperimentVerdict,
    MetricAggregation,
    MetricDirection,
    MetricsSourceKind,
)
from mayaworkbench.domain.models import (
    ExperimentBudget,
    ExperimentProtocol,
    MetricsSource,
    MetricSpec,
)
from mayaworkbench.errors import ExperimentError
from mayaworkbench.experiments.bundle import verify_experiment_bundle
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner

EVAL = """import json, os, random, sys

seed = int(os.environ.get("MAYA_EXPERIMENT_SEED", "0"))
fast = "--fast" in sys.argv
r = random.Random(seed + (2000 if fast else 1000))
shift = 0.90 if fast else 1.00
print(json.dumps({"metrics": {"loss": shift + r.random() * 0.01}}))
"""

NO_METRICS = """print("this script never emits a metrics document")
"""


def _git(repo, *args):  # type: ignore[no-untyped-def]
    from tests.conftest import git

    return git(repo, *args)


@pytest.fixture
def experiment_env(services: Services, project, repo):
    (repo / "eval.py").write_text(EVAL, encoding="utf-8")
    (repo / "eval_bad.py").write_text(NO_METRICS, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add experiment scripts")
    attached = services.repository_service.attach(project, repo)
    outcome = services.repository_service.create_snapshot(attached.repository)
    services.indexer.index(outcome.snapshot.id)
    return SimpleNamespace(
        services=services,
        project=project,
        repository=attached.repository,
        snapshot=outcome.snapshot,
        repo=repo,
    )


def _requires_network_namespace() -> bool:
    """Use the runner's positive isolation proof, not a static assumption."""
    import tempfile
    from pathlib import Path

    from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner

    runner = CommandRunner(
        Path(tempfile.mkdtemp(prefix="maya-netns-")),
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
            max_runtime_seconds=10,
        ),
        run_home=Path(tempfile.mkdtemp(prefix="maya-netns-home-")),
    )
    isolated, _reason = runner._network_isolation()
    return isolated


NETWORK_NAMESPACE = pytest.mark.skipif(
    not _requires_network_namespace(),
    reason="this host cannot create a network namespace, so experiments fail closed",
)


def create(
    env,
    *,
    role: ExperimentRole,
    name: str,
    command: list[str] | None = None,
    seeds: list[int] | None = None,
    profile: ExperimentProfile = ExperimentProfile.CPU_SMOKE,
    metrics_source: MetricsSource | None = None,
    metrics: list[MetricSpec] | None = None,
    treatment: list[str] | None = None,
    timeout: int = 60,
    budget: ExperimentBudget | None = None,
    task_id: str | None = None,
    protocol: ExperimentProtocol | None = None,
    min_seeds: int = 2,
    snapshot=None,
    approve: bool = True,
):
    service = env.services.experiment_service
    definition = service.create_definition(
        project_id=env.project.id,
        snapshot_id=(snapshot or env.snapshot).id,
        name=name,
        hypothesis="the candidate reduces loss",
        role=role,
        profile=profile,
        command=command if command is not None else [],
        seeds=seeds or [1, 2],
        timeout_seconds=timeout,
        metrics=metrics
        or [
            MetricSpec(
                key="loss",
                direction=MetricDirection.LOWER_IS_BETTER,
                aggregation=MetricAggregation.LAST,
                primary=True,
            )
        ],
        metrics_source=metrics_source
        or MetricsSource(kind=MetricsSourceKind.STDOUT_JSON),
        protocol=protocol or ExperimentProtocol(),
        treatment_dimensions=treatment,
        budget=budget
        or ExperimentBudget(
            max_runs=10,
            max_total_runtime_seconds=600,
            max_artifact_bytes=2_000_000,
            max_metric_observations=1000,
        ),
        task_id=task_id,
        min_seeds=min_seeds,
    )
    if approve:
        service.approve(definition.id, reason="test approval")
    return definition


@NETWORK_NAMESPACE
def test_full_local_flow_produces_a_verifiable_bundle(experiment_env, tmp_path):
    env = experiment_env
    baseline = create(
        env,
        role=ExperimentRole.BASELINE,
        name="baseline",
        command=["python", "eval.py"],
    )
    candidate = create(
        env,
        role=ExperimentRole.CANDIDATE,
        name="candidate",
        command=["python", "eval.py", "--fast"],
    )
    plan = env.services.experiment_service.plan(candidate.id)
    assert plan["blockers"] == []
    assert len(plan["planned_runs"]) == 2

    for definition in (baseline, candidate):
        runs = env.services.experiment_service.run(definition.id)
        assert len(runs) == 2
        assert all(run.usable for run in runs), [
            (run.status.value, run.error, run.metrics_error) for run in runs
        ]
        for run in runs:
            manifest = env.services.experiment_store.get_manifest(run.manifest_id)
            assert manifest.definition_sha256 == definition.content_sha256
            assert manifest.command == env.services.experiment_service.command_for(
                definition, run.seed
            )

    comparison = env.services.experiment_service.compare(
        candidate.id, baseline_id=baseline.id, remember=True
    )
    assert comparison.comparable is True
    assert comparison.verdict == ExperimentVerdict.CANDIDATE_ADVANTAGE
    assert comparison.design.value == "PAIRED"
    assert comparison.metrics[0].p_value is not None

    destination = tmp_path / "comparison-bundle"
    result = env.services.experiment_service.bundle(comparison.id, destination)
    assert result.manifest["kind"] == "COMPARISON"
    assert any(name.startswith("manifests/") for name in
               [entry["name"] for entry in result.manifest["files"]])
    assert any(entry["name"].startswith("outputs/") for entry in result.manifest["files"])
    verification = verify_experiment_bundle(destination)
    assert verification.ok, verification.errors
    assert verification.reproduce["commit_sha"] == candidate.base_commit_sha
    assert verification.reproduce["commits"] == {
        baseline.id: baseline.base_commit_sha,
        candidate.id: candidate.base_commit_sha,
    }
    assert len(verification.reproduce["commands"]) == 4

    # The comparison created an UNCONFIRMED memory; the CLI confirms it.
    memories = env.services.memory_service.list(env.project.id)
    remembered = [
        item
        for item in memories
        if item.scope == f"experiment-comparison:{comparison.id}"
    ]
    assert len(remembered) == 1
    assert remembered[0].status.value == "UNCONFIRMED"
    assert remembered[0].author_kind.value == "SYSTEM"


def test_unapproved_experiment_cannot_run(experiment_env):
    env = experiment_env
    definition = create(
        env,
        role=ExperimentRole.BASELINE,
        name="draft",
        command=["python", "eval.py"],
        approve=False,
    )
    assert definition.status == ExperimentStatus.DRAFT
    with pytest.raises(ExperimentError, match="approval"):
        env.services.experiment_service.run(definition.id)

    approved = env.services.experiment_service.approve(definition.id, reason="ok")
    assert approved.status == ExperimentStatus.APPROVED
    with pytest.raises(ExperimentError, match="DRAFT"):
        env.services.experiment_service.approve(definition.id, reason="again")

    revoked = env.services.experiment_service.revoke(
        definition.id, reason="changed my mind"
    )
    assert revoked.status == ExperimentStatus.REVOKED
    with pytest.raises(ExperimentError, match="approval"):
        env.services.experiment_service.run(definition.id)


def test_pre_registration_rejects_ambiguous_definitions(experiment_env):
    env = experiment_env
    service = env.services.experiment_service
    common = dict(
        project_id=env.project.id,
        snapshot_id=env.snapshot.id,
        name="bad",
        hypothesis="bad",
        role=ExperimentRole.BASELINE,
        profile=ExperimentProfile.CPU_SMOKE,
        command=["python", "eval.py"],
        timeout_seconds=30,
    )
    two_primary = [
        MetricSpec(key="loss", direction=MetricDirection.LOWER_IS_BETTER, primary=True),
        MetricSpec(key="acc", direction=MetricDirection.HIGHER_IS_BETTER, primary=True),
    ]
    with pytest.raises(ExperimentError, match="primary"):
        service.create_definition(**common, seeds=[1, 2], metrics=two_primary)

    no_primary = [
        MetricSpec(key="loss", direction=MetricDirection.LOWER_IS_BETTER, primary=False)
    ]
    with pytest.raises(ExperimentError, match="primary"):
        service.create_definition(**common, seeds=[1, 2], metrics=no_primary)

    duplicate = [
        MetricSpec(key="loss", direction=MetricDirection.LOWER_IS_BETTER, primary=True),
        MetricSpec(key="loss", direction=MetricDirection.LOWER_IS_BETTER, primary=False),
    ]
    with pytest.raises(ExperimentError, match="unique"):
        service.create_definition(**common, seeds=[1, 2], metrics=duplicate)

    with pytest.raises(ExperimentError, match="Seeds"):
        service.create_definition(
            **common, seeds=[1, 1], metrics=no_primary[:0] + [two_primary[0]]
        )
    with pytest.raises(ExperimentError, match="treatment"):
        service.create_definition(
            **common,
            seeds=[1, 2],
            metrics=[two_primary[0]],
            treatment_dimensions=["not.a.real.path"],
        )


def test_budget_and_seed_guards(experiment_env):
    env = experiment_env
    service = env.services.experiment_service
    with pytest.raises(ExperimentError, match="timeout"):
        create(
            env,
            role=ExperimentRole.BASELINE,
            name="slow",
            command=["python", "eval.py"],
            timeout=10**6,
        )
    with pytest.raises(ExperimentError, match="worst-case"):
        create(
            env,
            role=ExperimentRole.BASELINE,
            name="over-budget",
            command=["python", "eval.py"],
            timeout=600,
            seeds=[1, 2, 3],
            budget=ExperimentBudget(max_runs=5, max_total_runtime_seconds=900),
        )

    definition = create(
        env,
        role=ExperimentRole.BASELINE,
        name="seeded",
        command=["python", "eval.py"],
        seeds=[1, 2],
    )
    with pytest.raises(ExperimentError, match="pre-registered"):
        service.run(definition.id, seeds=[99])


def test_metrics_failure_is_not_a_usable_run_and_can_be_repaired(experiment_env):
    env = experiment_env
    definition = create(
        env,
        role=ExperimentRole.BASELINE,
        name="bad metrics",
        command=["python", "eval_bad.py"],
        seeds=[1],
        min_seeds=1,
        metrics_source=MetricsSource(kind=MetricsSourceKind.STDOUT_JSON),
    )
    if not _requires_network_namespace():
        pytest.skip("network namespace unavailable; the run would be blocked")
    [failed] = env.services.experiment_service.run(definition.id)
    assert failed.status.value == "COMPLETED"
    assert failed.metrics_ingested is False
    assert failed.metrics_error

    with pytest.raises(ExperimentError, match="declared metrics"):
        env.services.experiment_service.ingest(
            failed.id, metrics_text='{"metrics": {"accuracy": 1.0}}'
        )
    repaired = env.services.experiment_service.ingest(
        failed.id, metrics_text='{"metrics": {"loss": 1.25}}'
    )
    assert repaired.metrics_ingested is True
    assert repaired.observation_count == 1
    with pytest.raises(ExperimentError, match="already"):
        env.services.experiment_service.ingest(
            failed.id, metrics_text='{"metrics": {"loss": 1.0}}'
        )


def test_external_runs_are_marked_imported_and_explained(experiment_env, tmp_path):
    env = experiment_env
    service = env.services.experiment_service
    static = MetricsSource(kind=MetricsSourceKind.IMPORTED)
    baseline = create(
        env,
        role=ExperimentRole.BASELINE,
        name="external baseline",
        profile=ExperimentProfile.STATIC,
        metrics_source=static,
        seeds=[1, 2, 3],
    )
    candidate = create(
        env,
        role=ExperimentRole.CANDIDATE,
        name="external candidate",
        profile=ExperimentProfile.STATIC,
        metrics_source=static,
        seeds=[1, 2, 3],
    )
    for seed, value in {1: 1.00, 2: 1.01, 3: 0.99}.items():
        service.record_external(
            baseline.id,
            seed=seed,
            metrics_text=json.dumps({"metrics": {"loss": value}}),
            note="run on the lab server",
        )
    for seed, value in {1: 0.90, 2: 0.92, 3: 0.89}.items():
        service.record_external(
            candidate.id,
            seed=seed,
            metrics_text=json.dumps({"metrics": {"loss": value}}),
            note="run on the lab server",
        )
    with pytest.raises(ExperimentError, match="note"):
        service.record_external(
            candidate.id,
            seed=1,
            metrics_text='{"metrics": {"loss": 1.0}}',
            note="  ",
        )

    comparison = service.compare(candidate.id, baseline_id=baseline.id)
    assert any("outside this workbench" in item for item in comparison.limitations)
    bundle = tmp_path / "external-bundle"
    result = service.bundle(comparison.id, bundle)
    assert any("outside this workbench" in gap for gap in result.manifest["gaps"])
    assert verify_experiment_bundle(bundle).ok


def test_bundle_tampering_is_detected(experiment_env, tmp_path):
    env = experiment_env
    service = env.services.experiment_service
    static = MetricsSource(kind=MetricsSourceKind.IMPORTED)
    baseline = create(
        env,
        role=ExperimentRole.BASELINE,
        name="tb",
        profile=ExperimentProfile.STATIC,
        metrics_source=static,
        seeds=[1, 2],
    )
    candidate = create(
        env,
        role=ExperimentRole.CANDIDATE,
        name="tc",
        profile=ExperimentProfile.STATIC,
        metrics_source=static,
        seeds=[1, 2],
    )
    for seed, value in {1: 1.0, 2: 1.02}.items():
        service.record_external(
            baseline.id,
            seed=seed,
            metrics_text=json.dumps({"metrics": {"loss": value}}),
            note="external",
        )
        service.record_external(
            candidate.id,
            seed=seed,
            metrics_text=json.dumps({"metrics": {"loss": value - 0.1}}),
            note="external",
        )
    comparison = service.compare(candidate.id, baseline_id=baseline.id)
    destination = tmp_path / "bundle"
    service.bundle(comparison.id, destination)
    assert verify_experiment_bundle(destination).ok

    (destination / "report.md").write_text("edited\n", encoding="utf-8")
    tampered = verify_experiment_bundle(destination)
    assert not tampered.ok
    assert any("hash mismatch" in error for error in tampered.errors)

    (destination / "report.md").unlink()
    (destination / "extra.txt").write_text("unlisted\n", encoding="utf-8")
    extra = verify_experiment_bundle(destination)
    assert not extra.ok
    assert any("not listed" in error for error in extra.errors)

    with pytest.raises(ExperimentError, match="not empty"):
        service.bundle(comparison.id, destination)


def test_cross_commit_bundle_records_each_arm_commit(experiment_env, tmp_path):
    env = experiment_env
    service = env.services.experiment_service
    static = MetricsSource(kind=MetricsSourceKind.IMPORTED)
    baseline = create(
        env,
        role=ExperimentRole.BASELINE,
        name="cross baseline",
        profile=ExperimentProfile.STATIC,
        metrics_source=static,
        seeds=[1, 2, 3],
    )
    (env.repo / "CHANGELOG.md").write_text("- a candidate change\n", encoding="utf-8")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-qm", "candidate revision")
    second = env.services.repository_service.create_snapshot(env.repository).snapshot
    env.services.indexer.index(second.id)
    candidate = create(
        env,
        role=ExperimentRole.CANDIDATE,
        name="cross candidate",
        profile=ExperimentProfile.STATIC,
        metrics_source=static,
        seeds=[1, 2, 3],
        snapshot=second,
    )
    assert baseline.base_commit_sha != candidate.base_commit_sha
    for definition, values in (
        (baseline, {1: 1.00, 2: 1.01, 3: 0.99}),
        (candidate, {1: 0.90, 2: 0.92, 3: 0.89}),
    ):
        for seed, value in values.items():
            service.record_external(
                definition.id,
                seed=seed,
                metrics_text=json.dumps({"metrics": {"loss": value}}),
                note="external",
            )
    comparison = service.compare(candidate.id, baseline_id=baseline.id)
    assert comparison.comparable is True

    destination = tmp_path / "cross-commit-bundle"
    service.bundle(comparison.id, destination)
    reproduce = json.loads((destination / "reproduce.json").read_text())
    assert reproduce["commit_sha"] is None
    assert reproduce["commits"] == {
        baseline.id: baseline.base_commit_sha,
        candidate.id: candidate.base_commit_sha,
    }
    assert {
        command["commit_sha"] for command in reproduce["commands"]
    } == set(reproduce["commits"].values())
    result = verify_experiment_bundle(destination)
    assert result.ok, result.errors


def test_comparison_requires_a_different_experiment(experiment_env):
    env = experiment_env
    definition = create(
        env,
        role=ExperimentRole.BASELINE,
        name="only",
        command=["python", "eval.py"],
    )
    with pytest.raises(ExperimentError, match="itself"):
        env.services.experiment_service.compare(
            definition.id, baseline_id=definition.id
        )

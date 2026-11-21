"""End-to-end tests for portable, user-owned runner bundles (WP10).

These tests use the real experiment service, real stores and real SQLite homes.
No remote runner, network call or model invocation is involved: runs are
recorded from user-owned metrics, exported to a deterministic gzip-tar, verified,
and re-imported into a brand-new workbench home.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mayaworkbench.app.container import Services
from mayaworkbench.cli.runner_bundle_commands import register_runner_bundle_commands
from mayaworkbench.cli.runner_commands import register_runner_commands
from mayaworkbench.domain.enums import (
    ExperimentProfile,
    ExperimentRole,
    MetricAggregation,
    MetricDirection,
    MetricsSourceKind,
)
from mayaworkbench.domain.models import (
    ExperimentDataset,
    ExperimentEnvironment,
    ExperimentEvaluation,
    ExperimentHardware,
    ExperimentProtocol,
    MetricsSource,
    MetricSpec,
)
from mayaworkbench.errors import ExperimentError
from mayaworkbench.experiments.bundle import (
    RUN_BUNDLE_SCHEMA_VERSION,
    compute_dataset_fingerprint,
    import_run_bundle as import_bundle_records,
    sha256_file,
    verify_run_bundle,
)
from mayaworkbench.verification.attestation import sign_bundle

from tests.conftest import git

DATA_SHA = "a" * 64
EVAL_SHA = "b" * 64
LOCK_SHA = "c" * 64


def _protocol() -> ExperimentProtocol:
    return ExperimentProtocol(
        dataset=ExperimentDataset(
            id="tiny",
            version="1",
            fingerprint=DATA_SHA,
            fingerprint_method="sha256-of-declared-inputs",
            path="data/train.csv",
        ),
        evaluation=ExperimentEvaluation(script_path="eval.py", script_hash=EVAL_SHA),
        environment=ExperimentEnvironment(
            lockfile_path="requirements.lock", lockfile_hash=LOCK_SHA, python_version="3.12"
        ),
        hardware=ExperimentHardware(profile="CPU_SMOKE", cpu_count=2, memory_mb=4096),
    )


@pytest.fixture
def env(services: Services, project, repo):
    (repo / "eval.py").write_text("print('ok')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add eval script")
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


def _arm(env, *, name: str, role: ExperimentRole, values: dict[int, float]):
    service = env.services.experiment_service
    definition = service.create_definition(
        project_id=env.project.id,
        snapshot_id=env.snapshot.id,
        name=name,
        hypothesis="a user-owned runner reproduces the result",
        role=role,
        profile=ExperimentProfile.STATIC,
        command=[],
        seeds=sorted(values),
        metrics=[
            MetricSpec(
                key="loss",
                direction=MetricDirection.LOWER_IS_BETTER,
                aggregation=MetricAggregation.LAST,
                primary=True,
            )
        ],
        metrics_source=MetricsSource(kind=MetricsSourceKind.IMPORTED),
        protocol=_protocol(),
        min_seeds=len(values),
    )
    service.approve(definition.id, reason="test approval")
    for seed, value in values.items():
        service.record_external(
            definition.id,
            seed=seed,
            metrics_text=json.dumps({"metrics": {"loss": value}}),
            note="run on a user-owned notebook",
        )
    return definition


def _members(path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


def _retar(path: Path, members: dict[str, bytes]) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(raw.getvalue(), compresslevel=9, mtime=0))
    return path


def _fresh_home(tmp_path: Path, name: str) -> Services:
    home = Services.open(str(tmp_path / name))
    return home


def test_export_verify_import_comparison_bundle_has_provenance_parity(env, tmp_path):
    service = env.services.experiment_service
    baseline = _arm(
        env, name="bundle baseline", role=ExperimentRole.BASELINE, values={1: 1.0, 2: 1.02}
    )
    candidate = _arm(
        env, name="bundle candidate", role=ExperimentRole.CANDIDATE, values={1: 0.9, 2: 0.92}
    )
    comparison = service.compare(candidate.id, baseline_id=baseline.id)

    path = tmp_path / "comparison.tar.gz"
    result = service.export_run_bundle(comparison.id, path)
    assert result.archive_sha256
    verification = verify_run_bundle(path)
    assert verification.ok, verification.errors
    assert verification.manifest["schema_version"] == RUN_BUNDLE_SCHEMA_VERSION
    assert verification.reproduce["kind"] == "COMPARISON"
    assert verification.reproduce["commit_sha"] == candidate.base_commit_sha
    assert set(verification.reproduce["seeds"]) == {baseline.id, candidate.id}
    assert len(verification.reproduce["commands"]) == 4
    expected_fingerprint = compute_dataset_fingerprint(
        {f"{baseline.id}/dataset": DATA_SHA, f"{baseline.id}/evaluation_script": EVAL_SHA,
         f"{baseline.id}/lockfile": LOCK_SHA,
         f"{candidate.id}/dataset": DATA_SHA, f"{candidate.id}/evaluation_script": EVAL_SHA,
         f"{candidate.id}/lockfile": LOCK_SHA}
    )
    assert verification.reproduce["dataset_fingerprint"] == expected_fingerprint
    assert verification.reproduce["environment_lock_hashes"][candidate.id]["lockfile_hash"] == LOCK_SHA

    # Determinism: the same evidence produces byte-identical archives.
    second = tmp_path / "comparison-again.tar.gz"
    service.export_run_bundle(comparison.id, second)
    assert path.read_bytes() == second.read_bytes()

    imported_home = _fresh_home(tmp_path, "fresh")
    try:
        imported = imported_home.experiment_service.import_run_bundle(path)
        assert imported.attestation == "UNATTESTED"
        assert "outside this workbench" in imported.limitation
        assert len(imported.created_run_ids) == 4
        assert imported.observation_count == 4

        for definition in (baseline, candidate):
            fresh_definition = imported_home.experiment_store.get_definition(definition.id)
            assert fresh_definition.content_sha256 == definition.content_sha256
            assert fresh_definition.seeds == definition.seeds
        for arm in (baseline, candidate):
            for source_run in env.services.experiment_store.list_runs(arm.id):
                fresh_run = imported_home.experiment_store.get_run(source_run.id)
                assert fresh_run.seed == source_run.seed
                assert fresh_run.attempt == source_run.attempt
                assert fresh_run.command == source_run.command
                assert fresh_run.details["execution"] == "IMPORTED"
                assert fresh_run.details["attestation"] == "UNATTESTED"
                assert "outside this workbench" in fresh_run.details["imported_limitation"]
                source_observations = env.services.experiment_store.observations_for_run(
                    source_run.id
                )
                fresh_observations = imported_home.experiment_store.observations_for_run(
                    source_run.id
                )
                assert [(o.metric_key, o.value) for o in fresh_observations] == [
                    (o.metric_key, o.value) for o in source_observations
                ]
                manifest = imported_home.experiment_store.get_manifest(source_run.manifest_id)
                assert manifest.manifest_sha256 == env.services.experiment_store.get_manifest(
                    source_run.manifest_id
                ).manifest_sha256

        # Gate F consumes this exact limitation signal: imported/unattested
        # evidence is never treated as locally executed evidence.
        fresh_comparison = imported_home.experiment_service.compare(
            candidate.id, baseline_id=baseline.id
        )
        assert any(
            "outside this workbench" in item for item in fresh_comparison.limitations
        )
        assert imported_home.experiment_store.get_definition(
            candidate.id
        ).content_sha256 == candidate.content_sha256
    finally:
        imported_home.close()


def test_single_run_export_import_and_dataset_fingerprint_helper(env, tmp_path):
    service = env.services.experiment_service
    arm = _arm(env, name="single arm", role=ExperimentRole.BASELINE, values={1: 1.0, 2: 1.1})
    path = tmp_path / "single.tar.gz"
    service.export_run_bundle(arm.id, path)
    verified = verify_run_bundle(path)
    assert verified.ok, verified.errors
    assert verified.reproduce["kind"] == "SINGLE"
    assert verified.reproduce["seeds"][arm.id] == [1, 2]

    assert compute_dataset_fingerprint({}) == ""
    with pytest.raises(ExperimentError, match="sha256"):
        compute_dataset_fingerprint({"data.csv": "not-a-digest"})
    digest = sha256_file(env.repo / "eval.py")
    assert len(digest) == 64
    assert compute_dataset_fingerprint({"eval.py": digest}) == compute_dataset_fingerprint(
        {"eval.py": digest}
    )

    imported_home = _fresh_home(tmp_path, "fresh-single")
    try:
        result = imported_home.experiment_service.import_run_bundle(path)
        assert result.attestation == "UNATTESTED"
        assert len(result.created_run_ids) == 2
    finally:
        imported_home.close()


def test_tamper_detection_and_import_refusal(env, tmp_path):
    service = env.services.experiment_service
    arm = _arm(env, name="tamper arm", role=ExperimentRole.BASELINE, values={1: 1.0})
    path = tmp_path / "bundle.tar.gz"
    service.export_run_bundle(arm.id, path)
    original = _members(path)

    changed = dict(original)
    runs = json.loads(changed["runs.json"])
    runs[0]["status"] = "COMPLETED"
    runs[0]["command"] = ["python", "evil.py"]
    changed["runs.json"] = json.dumps(runs, indent=2, sort_keys=True).encode("utf-8")
    tampered = _retar(tmp_path / "tampered.tar.gz", changed)
    result = verify_run_bundle(tampered)
    assert not result.ok
    assert any("hash mismatch" in error for error in result.errors)
    with pytest.raises(ExperimentError, match="verification"):
        import_bundle_records(tampered, env.services.experiment_store)

    extra = dict(original)
    extra["evil.txt"] = b"unlisted"
    extra_result = verify_run_bundle(_retar(tmp_path / "extra.tar.gz", extra))
    assert not extra_result.ok
    assert any("not listed" in error for error in extra_result.errors)

    missing = dict(original)
    missing.pop("observations.json")
    missing_result = verify_run_bundle(_retar(tmp_path / "missing.tar.gz", missing))
    assert not missing_result.ok
    assert any("missing" in error for error in missing_result.errors)


def test_path_traversal_and_schema_version_are_refused(env, tmp_path):
    service = env.services.experiment_service
    arm = _arm(env, name="schema arm", role=ExperimentRole.BASELINE, values={1: 1.0})
    path = tmp_path / "bundle.tar.gz"
    service.export_run_bundle(arm.id, path)
    original = _members(path)

    traverse = dict(original)
    traverse["../outside.txt"] = b"escape"
    traverse_result = verify_run_bundle(_retar(tmp_path / "traversal.tar.gz", traverse))
    assert not traverse_result.ok
    assert any("unsafe archive path" in error for error in traverse_result.errors)

    manifest = dict(json.loads(original["manifest.json"]))
    manifest["schema_version"] = "9.9"
    versioned = dict(original)
    versioned["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    version_result = verify_run_bundle(_retar(tmp_path / "version.tar.gz", versioned))
    assert not version_result.ok
    assert any("schema version" in error for error in version_result.errors)

    reproduce = dict(json.loads(original["reproduce.json"]))
    reproduce.pop("dataset_fingerprint")
    incomplete = dict(original)
    incomplete["reproduce.json"] = json.dumps(reproduce, indent=2, sort_keys=True).encode()
    incomplete_result = verify_run_bundle(_retar(tmp_path / "incomplete.tar.gz", incomplete))
    assert not incomplete_result.ok
    assert any("dataset_fingerprint" in error for error in incomplete_result.errors)


def test_signature_uses_existing_attestation_helper(env, tmp_path, monkeypatch):
    service = env.services.experiment_service
    arm = _arm(env, name="signed arm", role=ExperimentRole.BASELINE, values={1: 1.0})
    path = tmp_path / "unsigned.tar.gz"
    service.export_run_bundle(arm.id, path)
    assert verify_run_bundle(path).attestation_present is False

    staging = tmp_path / "staging"
    staging.mkdir()
    for name, data in _members(path).items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    envelope = sign_bundle(staging, key="trusted-runner-key")
    signed = _retar(
        tmp_path / "signed.tar.gz",
        {**_members(path), "attestation.json": json.dumps(envelope).encode()},
    )
    assert verify_run_bundle(signed).attestation_present is True

    monkeypatch.setenv("MAYA_SIGNING_KEY", "trusted-runner-key")
    imported_home = _fresh_home(tmp_path, "fresh-signed")
    try:
        result = imported_home.experiment_service.import_run_bundle(signed)
        assert result.attestation == "SIGNED_BY_USER"
        assert "outside this workbench" in result.limitation
        assert "identifies the signing key" in result.limitation
    finally:
        imported_home.close()

    monkeypatch.setenv("MAYA_SIGNING_KEY", "wrong-key")
    second_home = _fresh_home(tmp_path, "fresh-wrong-key")
    try:
        result = second_home.experiment_service.import_run_bundle(signed)
        assert result.attestation == "UNATTESTED"
    finally:
        second_home.close()


def test_runner_bundle_commands_register_export_and_import():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_runner_commands(sub)
    register_runner_bundle_commands(sub)

    export = parser.parse_args(
        ["runner", "export", "--run", "xrun_1", "--out", "/tmp/out.tar.gz"]
    )
    assert export.func.__name__ == "cmd_runner_export"
    assert export.run_id == "xrun_1"
    assert export.out == "/tmp/out.tar.gz"
    comparison = parser.parse_args(
        ["runner", "export", "--comparison", "xcmp_1", "--out", "/tmp/out.tar.gz"]
    )
    assert comparison.comparison_id == "xcmp_1"
    imported = parser.parse_args(["runner", "import", "/tmp/in.tar.gz"])
    assert imported.func.__name__ == "cmd_runner_import"
    assert imported.bundle == "/tmp/in.tar.gz"
    with pytest.raises(SystemExit):
        parser.parse_args(["runner", "export", "--out", "/tmp/out.tar.gz"])

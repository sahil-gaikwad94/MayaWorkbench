"""WP2-01 end-to-end: negative results are recorded, confirmed, and recalled as advice."""

from __future__ import annotations

import json
from datetime import timedelta

from mayaworkbench.cli.main import main
from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import MemoryKind, MemoryStatus
from mayaworkbench.errors import ProjectMemoryError


def test_record_attempt_is_unconfirmed_and_scoped(services, project):
    memory = services.memory_service.record_attempt(
        project_id=project.id,
        goal="Reduce attention memory use",
        hypothesis="Replace full attention with block-sparse attention",
        outcome="Regression: 4% lower accuracy at equal compute",
        failure_signature="accuracy_delta=-0.04",
        base_commit="a" * 40,
        patch_hash="b" * 64,
        environment="cpu-smoke",
    )
    assert memory.kind is MemoryKind.NEGATIVE_RESULT
    assert memory.status is MemoryStatus.UNCONFIRMED
    assert memory.details["failure_signature"] == "accuracy_delta=-0.04"
    assert memory.commit_sha == "a" * 40


def test_unconfirmed_and_expired_results_are_never_suggested(services, project):
    memory = services.memory_service.record_attempt(
        project_id=project.id,
        goal="Stabilise training with a larger batch",
        hypothesis="Double batch size without changing learning rate",
        outcome="Loss diverged after 200 steps",
    )
    assert (
        services.memory_service.suggest_prior_attempts(
            project_id=project.id, goal="Double the batch size for training"
        )
        == []
    )
    services.memory_service.confirm(memory.id)
    assert services.memory_service.suggest_prior_attempts(
        project_id=project.id, goal="Double the batch size for training"
    )

    expired = services.memory_service.record_attempt(
        project_id=project.id,
        goal="Try a different optimiser",
        hypothesis="Switch to Adafactor for memory savings",
        outcome="No improvement",
        expires_at=utcnow() - timedelta(days=1),
    )
    services.memory_service.confirm(expired.id)
    assert all(
        item["memory_id"] != expired.id
        for item in services.memory_service.suggest_prior_attempts(
            project_id=project.id, goal="Switch optimiser to Adafactor"
        )
    )


def test_suggestions_are_advisory_and_ranked(services, project):
    first = services.memory_service.record_attempt(
        project_id=project.id,
        goal="Reproduce causal masking regression",
        hypothesis="Move the causal mask after normalization",
        outcome="Masking broke and accuracy fell",
        failure_signature="causal mask normalization",
    )
    second = services.memory_service.record_attempt(
        project_id=project.id,
        goal="Unrelated networking change",
        hypothesis="Use a different HTTP client",
        outcome="No improvement",
    )
    services.memory_service.confirm(first.id)
    services.memory_service.confirm(second.id)
    suggestions = services.memory_service.suggest_prior_attempts(
        project_id=project.id,
        goal="Move causal mask after normalization and reproduce the regression",
    )
    assert suggestions
    assert suggestions[0]["memory_id"] == first.id
    assert "advisory" in suggestions[0]
    assert "prohibition" in suggestions[0]["advisory"]


def test_record_requires_goal_and_valid_commit(services, project):
    try:
        services.memory_service.record_attempt(
            project_id=project.id, goal="", hypothesis="x", outcome="y"
        )
    except ProjectMemoryError:
        pass
    else:
        raise AssertionError("empty goal must be refused")
    try:
        services.memory_service.record_attempt(
            project_id=project.id,
            goal="g",
            hypothesis="h",
            outcome="o",
            base_commit="short",
        )
    except ProjectMemoryError:
        pass
    else:
        raise AssertionError("short commit must be refused")


def test_experience_cli_round_trip(tmp_path, repo, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    code, _ = run(["init", "--project", "experience-project"])
    assert code == 0
    code, payload = run(
        [
            "experience",
            "record",
            "--project",
            "experience-project",
            "--goal",
            "Increase throughput by caching embeddings",
            "--hypothesis",
            "Cache all intermediate embeddings",
            "--outcome",
            "Memory blew up",
        ]
    )
    assert code == 0
    memory_id = payload["memory"]["id"]
    code, _ = run(["memory", "confirm", memory_id])
    assert code == 0
    code, payload = run(
        [
            "experience",
            "suggest",
            "--project",
            "experience-project",
            "--goal",
            "Cache intermediate embeddings to increase throughput",
        ]
    )
    assert code == 0
    assert any(item["memory_id"] == memory_id for item in payload["suggestions"])

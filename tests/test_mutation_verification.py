"""WP9 end-to-end mutation-probe and security-report tests.

No model is involved. Every probe below runs a real pytest process through the
existing :class:`CommandRunner` path on a real temporary Git repository. The
tests check outcomes from recorded exit codes and artifacts, never a status
string that the code merely claims.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mayaworkbench.app.container import Services
from mayaworkbench.domain.enums import (
    CheckKind,
    CheckOutcome,
    TaskDecision,
    VerificationGate,
)
from mayaworkbench.domain.models import (
    CheckSpec,
    DiffFile,
    TaskConstraints,
    TaskContract,
    TaskDiff,
    TaskScope,
)
from mayaworkbench.verification import execution
from mayaworkbench.verification.mutation import (
    MutationProbe,
    critical_baseline,
    critical_tests_from_specs,
    detect_bypass,
    parse_critical_tests,
)
from mayaworkbench.verification.report import apply_mutation_downgrade
from mayaworkbench.verification.security_report import (
    CATEGORIES,
    build_security_report,
)

SHA = "a" * 40
DIGEST = "b" * 64
TEST_PATH = "tests/test_math.py"

SOURCE = """def add(a, b):
    return a + b


def test_add():
    assert add(1, 2) == 3
"""

SKIPPED_SOURCE = """import pytest


@pytest.mark.skip(reason="flaky")
def test_add():
    assert 1 + 1 == 2
"""

MULTI_SOURCE = """def test_one():
    assert (1 + 1) == 2


def test_two():
    assert (2 + 2) == 4


def test_three():
    assert (3 + 3) == 6


def test_four():
    assert (4 + 4) == 8
"""


def _git(root: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _git_show(root: Path, commit: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )
    return result.stdout.decode("utf-8") if result.returncode == 0 else None


@pytest.fixture
def artifact_store(tmp_path: Path):
    services = Services.open(str(tmp_path / "home"))
    try:
        yield services.artifacts
    finally:
        services.close()


@pytest.fixture
def git_repo(tmp_path: Path):
    """A real repository at a pinned base commit with a genuine assertion."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / TEST_PATH).write_text(SOURCE, encoding="utf-8")
    (root / "utils.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    commit = _git(root, "rev-parse", "HEAD")
    return SimpleNamespace(root=root, base_commit=commit)


def _reader(repo):
    def read(path: str, tree: str) -> str | None:
        if tree == "base":
            return _git_show(repo.root, repo.base_commit, path)
        if tree == "candidate":
            try:
                return (repo.root / path).read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    return read


def _constraints() -> TaskConstraints:
    # Local trusted execution with an explicit network policy; all outcomes are
    # derived from exit codes, never from a claimed capability.
    return TaskConstraints(
        network_access="ALLOW_WITH_APPROVAL",
        max_runtime_seconds=60,
        max_output_bytes=200_000,
    )


def _probe(artifact_store, repo, *, max_probes=8, max_seconds=120.0):
    return MutationProbe(
        artifacts=artifact_store,
        workspace=repo.root,
        constraints=_constraints(),
        run_home=repo.root.parent / "probe-runs",
        max_probes=max_probes,
        max_seconds=max_seconds,
    )


def _critical(path=TEST_PATH, test="test_add", mutation="invert_comparison"):
    return {
        "path": path,
        "test": test,
        "mutation": mutation,
    }


def test_baseline_and_candidate_run_through_existing_helpers(tmp_path, git_repo):
    """The declared test genuinely passes on both trees before any mutation."""
    baseline = execution.BaselineWorkspace.create(
        repository_root=git_repo.root,
        base_commit=git_repo.base_commit,
        patch_text=None,
        destination=tmp_path / "baseline",
    )
    try:
        for label, workspace in (("base", baseline.path), ("candidate", git_repo.root)):
            result = execution.execute_check(
                owner=label,
                workspace=workspace,
                constraints=_constraints(),
                run_home=tmp_path / "runs" / label,
                argv=["python", "-m", "pytest", f"{TEST_PATH}::test_add", "-q"],
                script=None,
                timeout_seconds=30,
            )
            assert execution.command_outcome(result) == CheckOutcome.PASSED, (
                label,
                result.exit_code,
                result.stdout[-500:],
                result.stderr[-500:],
            )
    finally:
        baseline.remove()


def test_killed_on_real_mutation(artifact_store, git_repo):
    parsed, errors = critical_tests_from_specs(
        [
            CheckSpec(
                key="command.unit",
                gate=VerificationGate.FUNCTIONAL,
                kind=CheckKind.COMMAND,
                title="unit",
                argv=["python", "-c", "pass"],
                details={"critical_tests": [_critical()]},
            )
        ]
    )
    assert parsed and not errors
    pinned = critical_baseline(parsed, _reader(git_repo))
    assert pinned["files"][TEST_PATH]["sha256"] == hashlib.sha256(
        SOURCE.encode("utf-8")
    ).hexdigest()

    result = _probe(artifact_store, git_repo).run(
        parsed,
        source_reader=_reader(git_repo),
        baseline=pinned,
    )

    assert result.counts() == {"KILLED": 1, "SURVIVED": 0, "SKIPPED": 0}
    probe = result.results[0]
    assert probe.outcome == "KILLED"
    assert probe.file == TEST_PATH
    assert probe.line == 6
    assert probe.exit_code == 1
    assert probe.artifact_id
    stored = json.loads(artifact_store.read_text(probe.artifact_id))
    assert stored["outcome"] == "KILLED"
    assert stored["argv"][:3] == ["python", "-m", "pytest"]
    assert stored["baseline_sha256"] == pinned["files"][TEST_PATH]["sha256"]


def test_survived_is_reported_never_a_pass(artifact_store, git_repo):
    (git_repo.root / TEST_PATH).write_text(SKIPPED_SOURCE, encoding="utf-8")
    parsed, errors = parse_critical_tests([_critical()])
    assert not errors
    result = _probe(artifact_store, git_repo).run(
        parsed,
        source_reader=_reader(git_repo),
        declarations=errors,
    )

    assert result.counts()["SURVIVED"] == 1
    item = result.results[0]
    assert item.outcome == "SURVIVED"
    assert item.artifact_id
    assert result.survived_declared == [item]
    stored = json.loads(artifact_store.read_text(item.artifact_id))
    assert stored["outcome"] == "SURVIVED"
    assert "still passed" in stored["reason"]


def test_skipped_without_declaration_and_bad_declaration(artifact_store, git_repo):
    result = _probe(artifact_store, git_repo).run([], source_reader=_reader(git_repo))
    assert result.counts() == {"KILLED": 0, "SURVIVED": 0, "SKIPPED": 1}
    assert result.results[0].outcome == "SKIPPED"
    assert "no critical tests were declared" in result.results[0].reason

    _, errors = parse_critical_tests(["not-a-node-id", {"path": TEST_PATH}])
    assert len(errors) == 2
    result = _probe(artifact_store, git_repo).run(
        [], source_reader=_reader(git_repo), declarations=errors
    )
    assert "declaration[0]" in result.results[0].reason


def test_caps_are_enforced(artifact_store, git_repo):
    (git_repo.root / TEST_PATH).write_text(MULTI_SOURCE, encoding="utf-8")
    declared = [
        {
            "path": TEST_PATH,
            "test": f"test_{name}",
            "mutation": "invert_comparison",
        }
        for name in ("one", "two", "three", "four")
    ]
    capped = _probe(artifact_store, git_repo, max_probes=2).run(
        parse_critical_tests(declared)[0],
        source_reader=_reader(git_repo),
    )
    assert capped.counts()["KILLED"] == 2
    assert capped.counts()["SKIPPED"] >= 1
    assert any("probe cap" in item.reason for item in capped.results)

    timed = _probe(artifact_store, git_repo, max_seconds=0).run(
        parse_critical_tests(declared)[0],
        source_reader=_reader(git_repo),
    )
    assert timed.counts()["KILLED"] == 0
    assert any("time budget" in item.reason for item in timed.results)


def test_bypass_flag_and_baseline_hash(artifact_store, git_repo):
    base = _git_show(git_repo.root, git_repo.base_commit, TEST_PATH)
    assert base is not None
    weakened = base.replace(
        "def test_add():", "import pytest\n\n@pytest.mark.skip(reason='no')\ndef test_add():"
    )
    (git_repo.root / TEST_PATH).write_text(weakened, encoding="utf-8")
    flagged = detect_bypass(TEST_PATH, base, weakened, "test_add")
    assert flagged is not None
    assert "SKIP_ADDED" in flagged["codes"]

    result = _probe(artifact_store, git_repo).run(
        parse_critical_tests([_critical()])[0],
        source_reader=_reader(git_repo),
    )
    assert result.bypass_detected is True
    assert result.bypass_details
    assert result.baseline["files"][TEST_PATH]["sha256"] == hashlib.sha256(
        base.encode("utf-8")
    ).hexdigest()
    # A bypass flag is recorded as INFO evidence; it never becomes a pass.
    assert any(item.bypass_detected for item in result.results)


def test_security_report_shape_and_findings(artifact_store, git_repo):
    workspace = git_repo.root
    (workspace / "utils.py").write_text(
        "TOKEN = 'ghp_" + "a" * 30 + "'\n", encoding="utf-8"
    )
    (workspace / "secrets.yaml").write_text("value: x\n", encoding="utf-8")
    (workspace / "loose.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "loose.py").chmod(0o666)

    contract = TaskContract(
        id="contract_1",
        task_id="task_1",
        project_id="project_1",
        version=1,
        task_type="ENGINEERING_CHANGE",
        goal="security evidence",
        repository_id="repo_1",
        snapshot_id="snap_1",
        scope=TaskScope(allowed_paths=["utils.py"], base_commit_sha=SHA),
        acceptance_criteria=["evidence exists"],
        constraints=TaskConstraints(),
        content_sha256=DIGEST,
    )
    diff = TaskDiff(
        id="diff_1",
        run_id="run_1",
        task_id="task_1",
        project_id="project_1",
        workspace_id="ws_1",
        base_commit_sha=SHA,
        patch_artifact_id="art_patch",
        patch_sha256="c" * 64,
        scope_violations=["outside.py"],
        files=[
            DiffFile(path="utils.py", binary=False, status="modified"),
            DiffFile(path="secrets.yaml", binary=False, status="added"),
            DiffFile(path="loose.py", binary=False, status="added"),
        ],
    )
    review = SimpleNamespace(
        findings=[],
        dependency_changes=["requirements.txt"],
    )
    checks = [
        SimpleNamespace(
            details={"network_enforcement": "not_requested"},
        )
    ]
    report = build_security_report(
        diff=diff,
        contract=contract,
        review=review,
        artifacts=artifact_store,
        workspace=workspace,
        checks=checks,
        run=SimpleNamespace(id="run_1"),
    )

    assert report["model_required"] is False
    assert report["artifact_id"]
    assert set(CATEGORIES) <= set(report["categories"])
    assert all(item["severity"] in {"INFO", "WARNING", "ERROR"} for item in report["findings"])
    codes = {item["code"] for item in report["findings"]}
    assert {"SCOPE_VIOLATION", "SECRET_PATH", "SECRET_CONTENT", "DEPENDENCY_CHANGE", "WORLD_WRITABLE_FILE"} <= codes
    for item in report["findings"]:
        assert item["category"] in CATEGORIES
        assert item["evidence"]["artifact_id"] == report["artifact_id"]
        assert item["evidence"]["to_kind"] == "ARTIFACT"
    assert any(link["from_kind"] == "SECURITY_FINDING" for link in report["evidence_links"])
    stored = json.loads(artifact_store.read_text(report["artifact_id"]))
    assert len(stored["findings"]) == len(report["findings"])
    assert {item["code"] for item in stored["findings"]} == codes


def test_security_report_clean_case_has_no_high_severity(artifact_store, git_repo):
    contract = TaskContract(
        id="contract_1",
        task_id="task_1",
        project_id="project_1",
        version=1,
        task_type="ENGINEERING_CHANGE",
        goal="security evidence",
        repository_id="repo_1",
        snapshot_id="snap_1",
        scope=TaskScope(allowed_paths=["utils.py"], base_commit_sha=SHA),
        acceptance_criteria=["evidence exists"],
        constraints=TaskConstraints(),
        content_sha256=DIGEST,
    )
    diff = TaskDiff(
        id="diff_1",
        run_id="run_1",
        task_id="task_1",
        project_id="project_1",
        workspace_id="ws_1",
        base_commit_sha=SHA,
        patch_artifact_id="art_patch",
        patch_sha256="d" * 64,
        files=[DiffFile(path="utils.py", binary=False, status="modified")],
    )
    review = SimpleNamespace(findings=[], dependency_changes=[])
    report = build_security_report(
        diff=diff,
        contract=contract,
        review=review,
        artifacts=artifact_store,
        workspace=git_repo.root,
        checks=[],
    )
    assert all(item["severity"] == "INFO" for item in report["findings"])
    assert report["artifact_id"]


def test_decision_downgrade_only_for_declared_survivors():
    mutation = {
        "counts": {"SURVIVED": 1},
        "results": [
            {
                "outcome": "SURVIVED",
                "declared": True,
                "file": TEST_PATH,
                "test": "test_add",
            }
        ],
    }
    decision, limitations = apply_mutation_downgrade(
        TaskDecision.ACCEPTED, [], mutation
    )
    assert decision == TaskDecision.COMPLETED_WITH_LIMITATIONS
    assert any("SURVIVED" in item and TEST_PATH in item for item in limitations)

    decision, _ = apply_mutation_downgrade(TaskDecision.FAILED, [], mutation)
    assert decision == TaskDecision.REJECTED

    decision, limitations = apply_mutation_downgrade(
        TaskDecision.ACCEPTED, [], {"counts": {"SURVIVED": 0}, "results": []}
    )
    assert decision == TaskDecision.ACCEPTED
    assert limitations == []

    undeclared = {
        "counts": {"SURVIVED": 1},
        "results": [{"outcome": "SURVIVED", "declared": False, "file": "x", "test": "y"}],
    }
    decision, limitations = apply_mutation_downgrade(
        TaskDecision.ACCEPTED, [], undeclared
    )
    assert decision == TaskDecision.ACCEPTED
    assert limitations == []

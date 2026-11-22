"""Deterministic tests for release evidence (no network, no real pytest runs)."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import types
from pathlib import Path

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.release import evidence as ev

KNOWN_REPORT = {
    "runtime": {"python": "3.12.3", "platform": "test-platform", "uid": 1000, "privileged": False},
    "trust_profiles": {
        "ROOTLESS_CONTAINER": {
            "available": True,
            "boundary": "rootless OCI with verified cgroups",
            "reason": "verified by test double",
        }
    },
    "limits": {},
    "network_isolation": {"available": True, "mechanism": "network_namespace"},
    "cgroup_v2": {},
    "execution_backends": {
        "rootless-container": {"oci_verification": {"smoke_test": {"status": "passed"}}}
    },
    "warning": "LOCAL_TRUSTED is not a sandbox",
}

JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites tests="12" failures="2" errors="1" skipped="3">
  <testsuite name="unit" tests="12" failures="2" errors="1" skipped="3" />
</testsuites>
"""

JSON_REPORT = {
    "summary": {
        "passed": 7,
        "failed": 1,
        "skipped": 2,
        "error": 0,
        "collected": 10,
        "total": 10,
    }
}


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A tiny real Git repository with one commit and a dependency manifest."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True, timeout=30)
    return repo


@pytest.fixture()
def fixed_probe(monkeypatch: pytest.MonkeyPatch):
    """Replace the live capability probe with a known, deterministic report."""
    report = types.SimpleNamespace(as_dict=lambda: copy.deepcopy(KNOWN_REPORT))
    monkeypatch.setattr(ev, "probe_execution_capability_report", lambda: report)
    return KNOWN_REPORT


def _write_report(tmp_path: Path, name: str, content: str) -> Path:
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return target


# ------------------------------------------------------------ report parsing
def test_parse_junit_xml_uses_aggregate_and_derives_passed(tmp_path: Path) -> None:
    path = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    assert ev.parse_pytest_report(path) == {
        "status": "PROVIDED",
        "collected": 12,
        "passed": 6,
        "failed": 2,
        "skipped": 3,
        "errors": 1,
    }


def test_parse_junit_xml_sums_testsuite_elements(tmp_path: Path) -> None:
    path = _write_report(
        tmp_path,
        "suites.xml",
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0"/>'
        '<testsuite tests="2" failures="0" errors="1" skipped="1"/></testsuites>',
    )
    assert ev.parse_pytest_report(path) == {
        "status": "PROVIDED",
        "collected": 5,
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "errors": 1,
    }


def test_parse_json_report_summary_and_flat_keys(tmp_path: Path) -> None:
    path = _write_report(tmp_path, "report.json", json.dumps(JSON_REPORT))
    assert ev.parse_pytest_report(path) == {
        "status": "PROVIDED",
        "collected": 10,
        "passed": 7,
        "failed": 1,
        "skipped": 2,
        "errors": 0,
    }
    flat = _write_report(
        tmp_path,
        "flat.json",
        json.dumps({"passed": 4, "failed": 0, "skipped": 1, "errors": 0, "total": 5}),
    )
    result = ev.parse_pytest_report(flat)
    assert result["status"] == "PROVIDED"
    assert (result["collected"], result["passed"], result["skipped"]) == (5, 4, 1)


def test_malformed_reports_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ev.EvidenceError, match="does not exist"):
        ev.parse_pytest_report(tmp_path / "absent.xml")
    with pytest.raises(ev.EvidenceError, match="empty"):
        ev.parse_pytest_report(_write_report(tmp_path, "empty.xml", ""))
    with pytest.raises(ev.EvidenceError, match="non-negative"):
        ev.parse_pytest_report(_write_report(tmp_path, "negative.xml", '<testsuite tests="-1"/>'))
    with pytest.raises(ev.EvidenceError, match="non-negative integer"):
        ev.parse_pytest_report(
            _write_report(tmp_path, "bad.json", json.dumps({"summary": {"passed": "many"}}))
        )
    with pytest.raises(ev.EvidenceError, match="invalid JUnit XML"):
        ev.parse_pytest_report(_write_report(tmp_path, "broken.xml", "<testsuite>"))
    with pytest.raises(ev.EvidenceError, match="unsupported"):
        ev.parse_pytest_report(_write_report(tmp_path, "text.txt", "plain text"))
    with pytest.raises(ev.EvidenceError, match="no test counts"):
        ev.parse_pytest_report(_write_report(tmp_path, "nocounts.xml", "<testsuite name='x'/>"))


# --------------------------------------------------------- adversarial input
def test_parse_adversarial_results_tolerates_key_names(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text(
        json.dumps({"TOTAL": 4, "PASS": 3, "FAIL": 1, "ERROR": 0}), encoding="utf-8"
    )
    (tmp_path / "results-two.json").write_text(
        json.dumps({"counts": {"total": 2, "passed": 1, "failed": 0, "errors": 1}}),
        encoding="utf-8",
    )
    result = ev.parse_adversarial_results(tmp_path)
    assert result["status"] == "PROVIDED"
    assert (result["total"], result["passed"], result["failed"], result["errors"]) == (6, 4, 1, 1)
    assert result["TOTAL"] == 6 and result["PASS"] == 4
    assert result["files"] == ["results-two.json", "results.json"]


def test_parse_adversarial_results_accepts_the_projects_array_format(tmp_path: Path) -> None:
    """The shipped harnesses write a top-level JSON array of per-case records."""
    (tmp_path / "results.json").write_text(
        json.dumps(
            [
                {"id": "A1", "status": "PASS"},
                {"id": "A2", "status": "PASS"},
                {"id": "A3", "status": "FAIL"},
            ]
        ),
        encoding="utf-8",
    )
    result = ev.parse_adversarial_results(tmp_path)
    assert (result["total"], result["passed"], result["failed"], result["errors"]) == (3, 2, 1, 0)


def test_parse_adversarial_results_from_outcomes_and_not_provided(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text(
        json.dumps({"results": [{"outcome": "PASS"}, {"outcome": "FAIL"}, {"status": "ERROR"}]}),
        encoding="utf-8",
    )
    result = ev.parse_adversarial_results(tmp_path)
    assert (result["total"], result["passed"], result["failed"], result["errors"]) == (3, 1, 1, 1)
    missing = ev.parse_adversarial_results(tmp_path / "missing")
    assert missing["status"] == "NOT_PROVIDED"
    assert ev.parse_adversarial_results(None)["status"] == "NOT_PROVIDED"
    (tmp_path / "results.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ev.EvidenceError, match="invalid adversarial results"):
        ev.parse_adversarial_results(tmp_path)


# ------------------------------------------------------------------ redaction
def test_assert_no_secrets_scans_matching_environment_names() -> None:
    env = {
        "MAYA_MODEL_API_KEY": "supersecret-value-1234",
        "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE0123456789",
        "GH_TOKEN": "ghp_exampletoken123456",
        "HARMLESS_NAME": "supersecret-value-1234",
    }
    ev.assert_no_secrets({"note": "nothing sensitive"}, env=env)
    for value, name in (
        ("supersecret-value-1234", "MAYA_MODEL_API_KEY"),
        ("AKIAEXAMPLE0123456789", "AWS_ACCESS_KEY_ID"),
        ("ghp_exampletoken123456", "GH_TOKEN"),
    ):
        with pytest.raises(ev.EvidenceError, match=name):
            ev.assert_no_secrets({"command": f"run --key={value}"}, env=env)


def test_build_evidence_refuses_planted_secret(
    git_repo: Path, tmp_path: Path, fixed_probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "planted-maYa-key-9876543210"
    monkeypatch.setenv("MAYA_MODEL_API_KEY", secret)
    report = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    with pytest.raises(ev.EvidenceError, match="MAYA_MODEL_API_KEY"):
        ev.build_evidence(
            root=git_repo, pytest_report=report, test_command=f"pytest --token={secret}"
        )


# --------------------------------------------------------------- build/hashes
def test_build_evidence_reads_real_git_and_manifest(
    git_repo: Path, tmp_path: Path, fixed_probe
) -> None:
    report = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    evidence = ev.build_evidence(
        root=git_repo,
        pytest_report=report,
        test_command="python -m pytest -q --junitxml=junit.xml",
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert evidence.commit_sha == head
    assert evidence.worktree_dirty is False
    assert evidence.tests["passed"] == 6
    assert evidence.test_command == "python -m pytest -q --junitxml=junit.xml"
    assert evidence.dependency_manifest_sha256 == hashlib.sha256(
        (git_repo / "pyproject.toml").read_bytes()
    ).hexdigest()
    assert evidence.status_docs["README.md"] == hashlib.sha256(
        (git_repo / "README.md").read_bytes()
    ).hexdigest()
    assert "IMPLEMENTATION_STATUS.md" not in evidence.status_docs


def test_canonical_hash_is_deterministic_and_change_sensitive(
    git_repo: Path, tmp_path: Path, fixed_probe
) -> None:
    first_time = "2026-11-22T00:00:00+00:00"
    second_time = "2026-11-22T12:34:56+00:00"
    report = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    first = ev.build_evidence(
        root=git_repo, pytest_report=report, test_command="pytest", generated_at=first_time
    )
    second = ev.build_evidence(
        root=git_repo, pytest_report=report, test_command="pytest", generated_at=second_time
    )
    assert first.generated_at != second.generated_at
    assert first.canonical_sha256 == second.canonical_sha256
    assert ev.canonical_sha256(first) == first.canonical_sha256
    assert len(first.canonical_sha256) == 64

    changed = ev.build_evidence(
        root=git_repo, pytest_report=report, test_command="pytest -v", generated_at=first_time
    )
    assert changed.canonical_sha256 != first.canonical_sha256
    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    changed_doc = ev.build_evidence(
        root=git_repo, pytest_report=report, test_command="pytest", generated_at=first_time
    )
    assert changed_doc.canonical_sha256 != first.canonical_sha256


def test_capability_report_equals_canonical_probe(
    git_repo: Path, tmp_path: Path, fixed_probe
) -> None:
    report = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    evidence = ev.build_evidence(root=git_repo, pytest_report=report, test_command="pytest")
    assert evidence.capability_report == KNOWN_REPORT
    assert "ROOTLESS_CONTAINER is unavailable" not in " ".join(evidence.unverified)
    assert any("live-model gate is separate" in note for note in evidence.unverified)


def test_missing_report_is_not_provided(git_repo: Path, fixed_probe) -> None:
    evidence = ev.build_evidence(root=git_repo, pytest_report=None, test_command="pytest")
    assert evidence.tests == {
        "status": "NOT_PROVIDED",
        "collected": None,
        "passed": None,
        "failed": None,
        "skipped": None,
        "errors": None,
    }
    assert any("No pytest report was supplied" in note for note in evidence.unverified)


def test_rendering_helpers_are_complete(git_repo: Path, tmp_path: Path, fixed_probe) -> None:
    report = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    evidence = ev.build_evidence(root=git_repo, pytest_report=report, test_command="pytest")
    document = json.loads(ev.to_json(evidence))
    assert document["schema_version"] == "release-evidence/v1"
    assert document["canonical_sha256"] == evidence.canonical_sha256
    markdown = ev.render_markdown(evidence)
    assert "# Release evidence" in markdown
    assert evidence.commit_sha in markdown
    assert "Unverified / not exercised" in markdown


# ------------------------------------------------------------------------ CLI
def test_cli_without_report_exits_two_with_typed_error(
    git_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, fixed_probe
) -> None:
    monkeypatch.chdir(git_repo)
    code = main(["--json", "release", "evidence"])
    captured = capsys.readouterr()
    assert code == 2
    assert not captured.out.strip()
    error = json.loads(captured.err)
    assert error["code"] == "USAGE_ERROR"
    assert "--pytest-report is required" in error["error"]


def test_cli_writes_files_and_prints_json(
    git_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fixed_probe,
) -> None:
    monkeypatch.chdir(git_repo)
    report = _write_report(tmp_path, "junit.xml", JUNIT_XML)
    out = tmp_path / "release"
    code = main(
        ["--json", "release", "evidence", "--pytest-report", str(report), "--out", str(out)]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    json_path = out / "release-evidence.json"
    md_path = out / "release-evidence.md"
    assert Path(payload["json_path"]) == json_path
    assert json_path.is_file() and md_path.is_file()
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored["tests"]["passed"] == 6
    assert stored["canonical_sha256"] == payload["canonical_sha256"]
    assert "# Release evidence" in md_path.read_text(encoding="utf-8")

    code = main(["release", "evidence", "--pytest-report", str(report), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["tests"]["collected"] == 12

    code = main(["release", "evidence", "--pytest-report", str(report)])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("# Release evidence")

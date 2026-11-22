"""Environment detection and provisioning plans are deterministic and non-executing."""

from __future__ import annotations

import json

from mayaworkbench.cli.main import main
from mayaworkbench.workspace.environments import build_plan, detect


def _project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{}', encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    return tmp_path


def _steps(root):
    return build_plan(root).steps


def _argv(root):
    return [list(step.argv) for step in _steps(root)]


def test_detect_reads_only_descriptors(tmp_path):
    info = detect(_project(tmp_path))
    assert set(info.ecosystems) == {"node", "python", "rust"}
    assert info.lockfiles["python"] == ("uv.lock",)
    assert info.confidence == "HIGH"


def test_provisioning_plan_is_argv_only_and_deterministic(tmp_path):
    info = detect(_project(tmp_path))
    first = build_plan(tmp_path, detection=info)
    second = build_plan(tmp_path, detection=info)
    assert first.plan_sha256 == second.plan_sha256
    assert all(isinstance(step.argv, tuple) and all(isinstance(x, str) for x in step.argv) for step in first.steps)
    # Every approval-gated step must be an install/build step that mutates or
    # reaches the network; read-only checks are never approval-gated.
    approval = [step for step in first.steps if step.requires_approval]
    assert approval
    assert all(
        any(word in step.purpose for word in ("install", "build", "configure", "proposal"))
        for step in approval
    )


# ------------------------------------------------------------------- Python
def test_pyproject_only_never_invents_requirements_txt(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    steps = _steps(tmp_path)
    install = next(step for step in steps if step.purpose == "python-install")
    assert install.argv == ("python", "-m", "pip", "install", "-e", ".")
    assert install.requires_approval is True
    assert "request" in install.note.lower()
    assert all("requirements.txt" not in step.argv for step in steps)


def test_pyproject_dev_extra_is_used_when_declared(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n\n[project.optional-dependencies]\ndev=['pytest']\n",
        encoding="utf-8",
    )
    install = next(s for s in _steps(tmp_path) if s.purpose == "python-install")
    assert install.argv == ("python", "-m", "pip", "install", "-e", ".[dev]")


def test_uv_lock_selects_frozen_sync(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    install = next(s for s in _steps(tmp_path) if s.purpose == "python-install")
    assert install.argv == ("uv", "sync", "--frozen")
    assert install.requires_approval is True


def test_poetry_lock_selects_locked_install(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\n", encoding="utf-8")
    (tmp_path / "poetry.lock").write_text("[[package]]\n", encoding="utf-8")
    install = next(s for s in _steps(tmp_path) if s.purpose == "python-install")
    assert install.argv == ("poetry", "install", "--no-interaction")
    assert install.requires_approval is True


def test_requirements_lock_installs_detected_file(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest==8.0\n", encoding="utf-8")
    detected = detect(tmp_path).lockfiles["python"]
    assert "requirements.txt" in detected and "requirements-dev.txt" in detected
    install = next(s for s in _steps(tmp_path) if s.purpose == "python-install")
    assert install.argv[:5] == ("python", "-m", "pip", "install", "-r")
    assert install.argv[5] in detected
    assert install.requires_approval is True


def test_python_check_prefers_pytest_when_evidenced(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    check = next(s for s in _steps(tmp_path) if s.purpose == "python-check")
    assert check.argv == ("python", "-m", "compileall", "-q", ".")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    check = next(s for s in _steps(tmp_path) if s.purpose == "python-check")
    assert check.argv == ("python", "-m", "pytest", "-q")


# ---------------------------------------------------------------------- Node
def test_node_lockfiles_select_the_correct_manager(tmp_path):
    cases = [
        ("package-lock.json", ("npm", "ci")),
        ("pnpm-lock.yaml", ("pnpm", "install", "--frozen-lockfile")),
        ("yarn.lock", ("yarn", "install", "--immutable")),
    ]
    for lockfile, expected in cases:
        sub = tmp_path / lockfile.replace(".", "_")
        sub.mkdir()
        (sub / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        (sub / lockfile).write_text("lock\n", encoding="utf-8")
        install = next(s for s in _steps(sub) if s.purpose == "node-install")
        assert install.argv == expected, lockfile
        assert install.requires_approval is True


def test_node_without_lock_reports_ambiguity_and_proposes_npm_install(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    install = next(s for s in _steps(tmp_path) if s.purpose == "node-install")
    assert install.argv == ("npm", "install")
    assert "proposal" in install.note.lower()
    assert "ambiguous" in install.note.lower()
    assert install.requires_approval is True


def test_node_check_has_no_run_in_band_without_jest_evidence(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    check = next(s for s in _steps(tmp_path) if s.purpose == "node-check")
    assert check.argv == ("npm", "test")


# --------------------------------------------------------------- Rust and Go
def test_rust_and_go_plans_are_locked_and_bounded(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    argv = _argv(tmp_path)
    assert ["cargo", "check", "--locked"] in argv
    assert ["cargo", "test", "--locked"] in argv
    assert ["go", "vet", "./..."] in argv
    assert ["go", "test", "./..."] in argv


# ---------------------------------------------------------------------- Java
def test_java_maven_is_wrapper_first(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    test = next(s for s in _steps(tmp_path) if s.purpose == "java-test")
    assert test.argv == ("./mvnw", "-B", "test")
    (tmp_path / "mvnw").unlink()
    test = next(s for s in _steps(tmp_path) if s.purpose == "java-test")
    assert test.argv == ("mvn", "-B", "test")


def test_java_gradle_is_wrapper_first(tmp_path):
    (tmp_path / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    test = next(s for s in _steps(tmp_path) if s.purpose == "java-test")
    assert test.argv == ("./gradlew", "test", "--no-daemon")
    (tmp_path / "gradlew").unlink()
    test = next(s for s in _steps(tmp_path) if s.purpose == "java-test")
    assert test.argv == ("gradle", "test")


# --------------------------------------------------------------- C/C++/CUDA
def test_cmake_plan_configures_builds_and_tests(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.0)\n", encoding="utf-8")
    steps = _steps(tmp_path)
    assert any(s.argv == ("cmake", "-S", ".", "-B", "build") for s in steps)
    assert any(s.argv == ("cmake", "--build", "build") for s in steps)
    assert any(s.argv == ("ctest", "--test-dir", "build", "--output-on-failure") for s in steps)
    assert all(s.requires_approval for s in steps if s.purpose in ("cpp-configure", "cpp-build"))


def test_makefile_plan_runs_make_and_make_test_when_evidenced(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\techo ok\n", encoding="utf-8")
    assert ["make"] in _argv(tmp_path)
    assert ["make", "test"] not in _argv(tmp_path)
    (tmp_path / "Makefile").write_text("all:\n\techo ok\n\ntest:\n\techo test\n", encoding="utf-8")
    assert ["make", "test"] in _argv(tmp_path)


def test_compile_commands_alone_proposes_no_build(tmp_path):
    (tmp_path / "compile_commands.json").write_text("[]", encoding="utf-8")
    plan = build_plan(tmp_path)
    assert not any(s.purpose.startswith("cpp-build") for s in plan.steps)
    assert any("metadata" in note.lower() for note in plan.notes)


# ------------------------------------------------------------------ Container
def test_container_plan_never_emits_host_docker_build(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    plan = build_plan(tmp_path)
    for step in plan.steps:
        assert list(step.argv)[:2] != ["docker", "build"]
        assert not step.argv or step.argv[0] != "docker"
    proposal = next(s for s in plan.steps if s.purpose == "container-build-proposal")
    assert proposal.requires_approval is True
    assert proposal.argv[0] == "maya"
    assert "isolated" in proposal.note.lower()


def test_ci_only_adds_a_note_and_no_execution(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    plan = build_plan(tmp_path)
    assert any("CI workflows detected" in note for note in plan.notes)
    assert all(s.argv and s.argv[0] != "bash" for s in plan.steps)


# --------------------------------------------------------------- Fingerprint
def test_fingerprint_is_deterministic_and_tracks_lockfile_bytes(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    first = build_plan(tmp_path)
    again = build_plan(tmp_path)
    assert len(first.fingerprint_sha256) == 64
    assert first.fingerprint_sha256 == again.fingerprint_sha256
    assert first.as_dict()["fingerprint_sha256"] == first.fingerprint_sha256
    lock.write_text("version = 2\n", encoding="utf-8")
    changed = build_plan(tmp_path)
    assert changed.fingerprint_sha256 != first.fingerprint_sha256
    assert changed.plan_sha256 != first.plan_sha256


# ------------------------------------------------------------------------ CLI
def test_cli_detect_and_plan(tmp_path, capsys):
    root = _project(tmp_path)
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    assert run(["init", "--project", "envs"])[0] == 0
    code, payload = run(["env", "detect", "--path", str(root)])
    assert code == 0 and "python" in payload["ecosystems"]
    code, payload = run(["env", "plan", "--path", str(root)])
    assert code == 0 and len(payload["plan_sha256"]) == 64
    assert len(payload["fingerprint_sha256"]) == 64


def test_cli_accepts_positional_path_and_opt_path_wins(tmp_path, capsys):
    root = _project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    assert run(["init", "--project", "envs"])[0] == 0
    code, payload = run(["env", "detect", str(root)])
    assert code == 0 and "python" in payload["ecosystems"]
    code, payload = run(["env", "plan", str(root)])
    assert code == 0 and len(payload["plan_sha256"]) == 64
    code, payload = run(["env", "doctor", str(root)])
    assert code in (0, 1) and "detected" in payload
    # Both forms supplied: --path takes precedence over the positional value.
    code, payload = run(["env", "detect", str(root), "--path", str(other)])
    assert code == 0 and "container" in payload["ecosystems"]

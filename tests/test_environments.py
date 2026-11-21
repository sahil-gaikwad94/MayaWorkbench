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
    install = [step for step in first.steps if step.requires_approval]
    assert install and all(step.purpose.endswith("install") for step in install)


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

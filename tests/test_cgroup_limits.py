"""cgroup v2 process-tree limiter: verified setup, readback, cleanup, fail-closed."""

from __future__ import annotations

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.cgroups import CgroupV2Scope, probe_cgroup_v2
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner


def _fake_root(tmp_path, *, controllers="cpu pids memory "):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text(controllers, encoding="ascii")
    (root / "cgroup.procs").write_text("", encoding="ascii")
    return root


def test_probe_reports_missing_pids_controller(tmp_path):
    root = _fake_root(tmp_path, controllers="cpu memory ")
    capability = probe_cgroup_v2(root)
    assert capability.available is False
    assert capability.supports_pids is False
    assert "pids" in capability.reason


def test_scope_writes_and_reads_back_pids_limit(tmp_path):
    root = _fake_root(tmp_path)
    scope = CgroupV2Scope(root, name="test-scope")
    scope.create(8)
    assert (scope.path / "pids.max").read_text(encoding="ascii") == "8"
    (scope.path / "pids.current").write_text("3", encoding="ascii")
    assert scope.pids_current() == 3
    scope.cleanup()
    assert not scope.path.exists()


def test_runner_with_cgroup_reports_enforced_process_count(tmp_path):
    root = _fake_root(tmp_path)
    scope = CgroupV2Scope(root, name="run-scope")
    runner = CommandRunner(
        tmp_path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            max_processes=8,
        ),
        run_home=tmp_path / "run-home",
        cgroup_scope=scope,
    )
    result = runner.run(["python", "-c", "print('ok')"], timeout_seconds=10)
    assert result.exit_code == 0
    assert result.limits["process_count"].status == "enforced"
    assert result.limits["process_count"].mechanism == "cgroup-v2"
    assert not scope.path.exists()


def test_runner_blocks_when_cgroup_cannot_be_established(tmp_path):
    root = _fake_root(tmp_path, controllers="cpu memory ")
    scope = CgroupV2Scope(root, name="bad-scope")
    runner = CommandRunner(
        tmp_path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
            max_processes=8,
        ),
        run_home=tmp_path / "run-home",
        cgroup_scope=scope,
    )
    marker = tmp_path / "should-not-exist"
    with pytest.raises(PolicyError, match="Process-tree limit could not be established"):
        runner.run(
            ["python", "-c", f"open({str(marker)!r}, 'w').write('x')"], timeout_seconds=10
        )
    assert not marker.exists()

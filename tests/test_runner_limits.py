"""Resource-limit and trust-boundary regressions for the local runner (MWB-010, MW-P0-02/03).

The runner must report what the kernel actually confirmed, never a blanket
"rlimit" claim. In particular, process count is unsupported until a verified
cgroup/container backend exists: RLIMIT_NPROC is neither reliable as root nor
safe as a per-UID headroom calculation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mayaworkbench.workspace.runner as runner_module
from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.runner import (
    LIMIT_NAMES,
    CommandPolicy,
    CommandRunner,
)


def _runner(tmp_path: Path, **overrides) -> CommandRunner:
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="ALLOW_WITH_APPROVAL",
        dependency_policy="APPROVAL_REQUIRED",
        max_runtime_seconds=20,
        max_output_bytes=4096,
        **overrides,
    )
    return CommandRunner(tmp_path, policy=policy, run_home=tmp_path / "run-home")


def test_address_space_limit_stops_a_memory_hog(tmp_path):
    runner = _runner(tmp_path, memory_limit_mb=256)
    result = runner.run(
        ["python", "-c", "x = bytearray(512 * 1024 * 1024); print(len(x))"],
        timeout_seconds=10,
    )
    assert result.limits["memory"].status == "enforced"
    assert result.limits["memory"].mechanism == "rlimit-memory"
    assert result.exit_code not in (0, None)
    assert not result.timed_out


def test_cpu_limit_terminates_a_cpu_spin(tmp_path):
    runner = _runner(tmp_path, cpu_seconds=1)
    result = runner.run(
        ["python", "-c", "while True: pass"],
        timeout_seconds=20,
    )
    assert result.limits["cpu"].status == "enforced"
    assert not result.timed_out, "the wall clock, not RLIMIT_CPU, ended the command"
    assert result.exit_code is not None and result.exit_code != 0
    assert result.duration_ms < 10_000


def test_file_size_limit_stops_a_large_writer(tmp_path):
    runner = _runner(tmp_path, max_file_size_mb=1)
    result = runner.run(
        [
            "python",
            "-c",
            "open('big.bin', 'wb').write(b'x' * (4 * 1024 * 1024))",
        ],
        timeout_seconds=10,
    )
    assert result.limits["file_size"].status == "enforced"
    assert result.exit_code not in (0, None)
    assert (tmp_path / "big.bin").stat().st_size <= 1024 * 1024


def test_limits_are_reported_per_limit_and_summarised(tmp_path):
    """MW-P0-02: no aggregate claim without per-limit runtime evidence."""
    result = _runner(tmp_path).run(["python", "-c", "print('ok')"], timeout_seconds=10)
    assert set(result.limits) == set(LIMIT_NAMES)
    assert result.limits["wall_time"].status == "enforced"
    assert result.limits["wall_time"].mechanism == "supervisor"
    assert result.limits["process_count"].status == "unsupported"
    assert result.limits["process_count"].mechanism is None
    assert result.limits_enforcement == "partially_enforced"


def test_failed_setrlimit_is_reported_as_unsupported(tmp_path, monkeypatch):
    """A swallowed setrlimit error must never become an enforcement claim."""
    real_setrlimit = runner_module.resource.setrlimit

    def failing_setrlimit(resource_id, limits):
        if resource_id == runner_module.resource.RLIMIT_AS:
            raise OSError("probe: address-space limit refused")
        return real_setrlimit(resource_id, limits)

    monkeypatch.setattr(runner_module.resource, "setrlimit", failing_setrlimit)
    result = _runner(tmp_path, memory_limit_mb=256).run(
        ["python", "-c", "print('ok')"], timeout_seconds=10
    )
    assert result.limits["memory"].status == "unsupported"
    assert "probe: address-space limit refused" in result.limits["memory"].detail
    assert result.limits_enforcement == "partially_enforced"


def test_a_legitimate_child_process_is_not_blocked(tmp_path):
    """The removed RLIMIT_NPROC must not fail ordinary forks with EAGAIN."""
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'print(1)'])\n"
        "print('child', child.wait())\n"
    )
    (tmp_path / "spawn.py").write_text(script, encoding="utf-8")
    result = _runner(tmp_path, max_processes=8).run(
        ["python", "spawn.py"], timeout_seconds=10
    )
    assert result.exit_code == 0, result.stderr
    assert "child 0" in result.stdout
    assert "Resource temporarily unavailable" not in result.stderr


def test_fork_bomb_is_stopped_by_the_supervisor_and_reported_honestly(tmp_path):
    """No false PID-limit claim; the wall-clock supervisor kills the whole tree."""
    script = (
        "import os, time\n"
        "with open('children.pids', 'w') as handle:\n"
        "    for _ in range(200):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(30)\n"
        "            os._exit(0)\n"
        "        handle.write(str(pid) + '\\n')\n"
        "print('children created')\n"
    )
    (tmp_path / "fork_bomb.py").write_text(script, encoding="utf-8")
    runner = _runner(tmp_path, max_processes=8)
    result = runner.run(["python", "fork_bomb.py"], timeout_seconds=5)

    assert result.limits["process_count"].status == "unsupported"
    assert result.limits["wall_time"].status == "enforced"
    assert result.limits_enforcement == "partially_enforced"
    assert result.cleanup_required
    assert result.cleanup_succeeded
    pids = [
        int(line)
        for line in (tmp_path / "children.pids").read_text().splitlines()
        if line.strip()
    ]
    assert len(pids) == 200
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize(
    "trust_profile",
    [
        "READ_ONLY_ANALYSIS",
        "ROOTLESS_CONTAINER",
        "REMOTE_MICROVM",
        "GPU_RUNNER",
    ],
)
def test_unavailable_trust_profiles_block_before_process_creation(tmp_path, trust_profile):
    """MW-P0-03: never downgrade an untrusted task to host privileges."""
    marker = tmp_path / "must-not-exist"
    runner = _runner(tmp_path, trust_profile=trust_profile)
    with pytest.raises(PolicyError, match="not available|does not permit"):
        runner.run(
            ["python", "-c", f"open({str(marker)!r}, 'w').write('escaped')"],
            timeout_seconds=10,
        )
    assert not marker.exists()


def test_local_trusted_profile_is_explicitly_permitted(tmp_path):
    result = _runner(tmp_path, trust_profile="LOCAL_TRUSTED").run(
        ["python", "-c", "print('trusted')"], timeout_seconds=10
    )
    assert result.exit_code == 0
    assert "trusted" in result.stdout


def test_deny_by_default_requires_positive_namespace_proof(tmp_path):
    """A child that can reach the parent's loopback is NOT isolated."""
    from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner

    runner = CommandRunner(
        tmp_path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
            max_runtime_seconds=10,
        ),
        run_home=tmp_path / "run-home",
    )
    isolated, reason = runner._network_isolation()
    assert isinstance(isolated, bool)
    assert reason
    if not isolated:
        marker = tmp_path / "must-not-run"
        result = runner.run(
            ["python", "-c", f"open({str(marker)!r}, 'w').write('x')"],
            timeout_seconds=10,
        )
        assert result.blocked and "DENY_BY_DEFAULT" in (result.blocked_reason or "")
        assert not marker.exists()


def test_cpu_time_and_core_quota_are_recorded_separately(tmp_path):
    """P1-01: a 'cpu' number must never be interpreted as both budgets."""
    from mayaworkbench.domain.models import TaskConstraints

    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="ALLOW_WITH_APPROVAL",
        dependency_policy="DENY",
        cpu_seconds=30,  # legacy alias, still reported
        cpu_cores=4,
        wall_time_seconds=120,
    )
    described = policy.describe()
    assert described["cpu_time_seconds"] == 30   # cumulative CPU TIME budget
    assert described["cpu_cores"] == 4           # concurrency QUOTA
    assert described["wall_time_seconds"] == 120
    assert policy.effective_cpu_time_seconds == 30
    # The legacy alias still reports for old callers but is not the authority.
    assert policy.cpu_seconds == 30

    constraints = TaskConstraints(
        cpu_time_seconds=5,
        cpu_cores=2,
        max_runtime_seconds=90,
        compute_profile="CPU_SMOKE",
    )
    mapped = CommandPolicy.from_constraints(constraints)
    assert mapped.cpu_time_seconds == 5
    assert mapped.cpu_cores == 2
    assert mapped.wall_time_seconds == 90


def test_command_result_records_each_cpu_dimension_independently(tmp_path):
    policy = CommandPolicy(
        profile="CPU_SMOKE",
        network_policy="ALLOW_WITH_APPROVAL",
        dependency_policy="DENY",
        cpu_cores=2,
        cpu_time_seconds=15,
        max_runtime_seconds=60,
    )
    runner = CommandRunner(tmp_path, policy=policy, run_home=tmp_path / "home")
    result = runner.run(["python", "-c", "print('x')"], timeout_seconds=10)
    assert result.cpu_cores == 2
    assert result.cpu_time_seconds == 15
    # The wall deadline is the observed supervisor timeout, recorded separately.
    assert result.wall_time_seconds == 10

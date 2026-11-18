"""OS resource-limit regressions for the local runner (MWB-010)."""

from __future__ import annotations

from pathlib import Path

from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner


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
    assert result.limits_enforcement == "rlimit"
    assert result.exit_code not in (0, None)
    assert not result.timed_out


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
    assert result.exit_code not in (0, None)
    assert (tmp_path / "big.bin").stat().st_size <= 1024 * 1024

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


def test_cpu_limit_terminates_a_cpu_spin(tmp_path):
    runner = _runner(tmp_path, cpu_seconds=1)
    result = runner.run(
        ["python", "-c", "while True: pass"],
        timeout_seconds=20,
    )
    assert result.limits_enforcement == "rlimit"
    assert not result.timed_out, "the wall clock, not RLIMIT_CPU, ended the command"
    assert result.exit_code is not None and result.exit_code != 0
    assert result.duration_ms < 10_000


def test_process_limit_stops_a_fork_bomb(tmp_path):
    script = (
        "import os, time\n"
        "children = 0\n"
        "for _ in range(200):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError as exc:\n"
        "        print('blocked', exc.errno)\n"
        "        break\n"
        "    if pid == 0:\n"
        "        time.sleep(3)\n"
        "        os._exit(0)\n"
        "    children += 1\n"
        "print('children', children)\n"
    )
    # argv entries may not contain newlines, so the bomb is a real script file.
    (tmp_path / "fork_bomb.py").write_text(script, encoding="utf-8")
    runner = _runner(tmp_path, max_processes=8)
    result = runner.run(["python", "fork_bomb.py"], timeout_seconds=10)
    assert result.limits_enforcement == "rlimit"
    assert "blocked" in result.stdout
    assert result.cleanup_succeeded
    assert not result.timed_out
    reported = [
        int(line.split()[1])
        for line in result.stdout.splitlines()
        if line.startswith("children ")
    ]
    assert reported and reported[0] <= 12, result.stdout


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

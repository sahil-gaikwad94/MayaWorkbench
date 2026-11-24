"""WP2: provisioning executes approved plans and attests the result."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.environments import ProvisionStep, ProvisioningPlan
from mayaworkbench.workspace.provisioning import (
    EnvironmentProvisioner,
    ProvisionReport,
    ServiceSpec,
    canonical_sha256,
    check_service,
    package_inventory,
)
from mayaworkbench.workspace.runner import CommandResult, CommandPolicy, CommandRunner
from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace
from mayaworkbench.workspace.task_runtime import CommandRunnerTaskRuntime


def _result(code: int = 0, *, blocked: bool = False, stdout: str = "ok") -> CommandResult:
    return CommandResult(
        argv=["x"], exit_code=code, stdout=stdout, stderr="", duration_ms=5,
        timed_out=False, output_truncated=False, blocked=blocked,
        blocked_reason="blocked by policy" if blocked else None,
    )


class _FakeRuntime:
    def __init__(self):
        self.exec_calls: list[tuple] = []
        self.install_calls: list[tuple] = []

    def exec(self, argv, *, timeout_seconds, run_home=None):
        self.exec_calls.append((tuple(argv), timeout_seconds))
        return _result()

    def exec_install(self, argv, *, timeout_seconds, run_home=None):
        self.install_calls.append((tuple(argv), timeout_seconds))
        return _result()


def _plan(*steps: ProvisionStep) -> ProvisioningPlan:
    return ProvisioningPlan(
        root="/tmp/project", runtime="ROOTLESS_CONTAINER", steps=tuple(steps),
        plan_sha256="a" * 64, fingerprint_sha256="b" * 64,
    )


def test_package_inventory_is_content_addressed_and_never_invents(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "Cargo.lock").write_text("lock\n", encoding="utf-8")
    digest, inventory = package_inventory(tmp_path)
    assert set(inventory) == {"pyproject.toml", "Cargo.lock"}
    assert len(digest) == 64
    # No lockfile -> empty inventory, never a fabricated requirements.txt.
    empty = tmp_path / "empty"
    empty.mkdir()
    empty_digest, empty_inventory = package_inventory(empty)
    assert empty_inventory == {}
    assert empty_digest == package_inventory(empty)[0]


def test_service_spec_requires_loopback():
    ServiceSpec(name="api", port=8000)
    for host in ("0.0.0.0", "example.com", "192.168.1.1"):
        with pytest.raises(PolicyError, match="loopback"):
            ServiceSpec(name="api", port=8000, host=host)


def test_service_health_check_is_a_real_loopback_probe():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    health = check_service(ServiceSpec(name="api", port=port, timeout_seconds=1))
    listener.close()
    assert health.status == "HEALTHY"
    # A closed port reports unhealthy rather than being silently skipped.
    unhealthy = check_service(ServiceSpec(name="closed", port=port, timeout_seconds=0.2))
    assert unhealthy.status == "UNHEALTHY"


def test_provisioner_separates_execute_install_and_blocked_steps(tmp_path):
    plan = _plan(
        ProvisionStep("verify-runtime", ("maya", "runner", "profiles", "--json")),
        ProvisionStep("install", ("uv", "sync", "--frozen"), requires_approval=True),
        ProvisionStep("test", ("python", "-m", "pytest", "-q")),
    )
    fake = _FakeRuntime()
    report = EnvironmentProvisioner(
        plan=plan, runtime=fake, approved_purposes=frozenset({"install"}),
        run_home=tmp_path,
    ).provision()
    assert report.status == "PROVISIONED"
    assert [item.outcome for item in report.steps] == ["PASSED", "PASSED", "PASSED"]
    assert len(fake.exec_calls) == 2
    assert [call[0][0] for call in fake.install_calls] == ["uv"]
    assert report.canonical_sha256 == canonical_sha256(report.as_dict())

    blocked = EnvironmentProvisioner(
        plan=plan, runtime=fake, run_home=tmp_path,
    ).provision()
    assert blocked.status == "BLOCKED"
    assert blocked.steps[1].outcome == "BLOCKED"
    assert "approval" in blocked.steps[1].blocked_reason
    assert len(fake.install_calls) == 1  # no new install was executed


def test_provisioning_report_detects_failure_and_redacts_output(tmp_path):
    class FailingRuntime(_FakeRuntime):
        def exec(self, argv, *, timeout_seconds, run_home=None):
            return _result(code=1, stdout="TOKEN=sk-" + "A" * 40)

    report = EnvironmentProvisioner(
        plan=_plan(ProvisionStep("test", ("python", "-m", "pytest"))),
        runtime=FailingRuntime(), run_home=tmp_path,
    ).provision()
    assert report.status == "FAILED"
    assert report.steps[0].exit_code == 1
    assert "sk-" + "A" * 40 not in json.dumps(report.as_dict())
    assert "[REDACTED]" in report.steps[0].output_excerpt


def test_install_authorization_on_the_task_runtime(tmp_path):
    ws = RuntimeWorkspace.for_host(tmp_path)
    runner = CommandRunner(
        ws.path,
        policy=CommandPolicy(
            profile="CPU_SMOKE", network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
        ),
        run_home=tmp_path / "home",
    )
    unauthorized = CommandRunnerTaskRuntime(
        workspace=ws, runner=runner, trust_profile="LOCAL_TRUSTED"
    )
    with pytest.raises(PolicyError, match="not authorized"):
        unauthorized.exec_install(["pip", "--version"], timeout_seconds=10)

    authorized = CommandRunnerTaskRuntime(
        workspace=ws, runner=runner, trust_profile="LOCAL_TRUSTED",
        install_authorized=True,
    )
    result = authorized.exec_install(["pip", "--version"], timeout_seconds=30)
    assert result.exit_code == 0
    assert "pip" in result.stdout


def test_cli_env_provision_registers_and_reports(tmp_path, capsys):
    from mayaworkbench.cli.main import main

    project = tmp_path / "project"
    project.mkdir()
    code = main(["--home", str(tmp_path / "home"), "--json", "env", "provision",
                 str(project), "--out", str(tmp_path / "report.json")])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code in (0, 1)
    assert payload["schema_version"] == "environment-provisioning/v1"
    assert payload["plan_sha256"]
    assert json.loads((tmp_path / "report.json").read_text()) == payload

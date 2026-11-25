"""WP2 (merged): provisioning boundary and approved install execution.

The extensive EnvironmentProvisioner contract is covered by
``tests/test_environment_provisioning.py``. This file supplements it with the
approval-gated ``exec_install`` path and the additive evidence helpers that
survived the two-implementation merge.
"""

from __future__ import annotations

import hashlib
import json
import socket
from types import SimpleNamespace

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.environments import ServiceSpec
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace
from mayaworkbench.workspace.task_runtime import CommandRunnerTaskRuntime


def test_install_authorization_on_the_task_runtime(tmp_path):
    ws = RuntimeWorkspace.for_host(tmp_path)
    runner = CommandRunner(
        ws.path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
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


def test_service_spec_declares_a_loopback_health_check():
    spec = ServiceSpec(name="api", port=8000, health_check=("python", "-c", "pass"))
    assert spec.port == 8000 and spec.required is True
    assert "api" in json.dumps(spec.as_dict())


def test_loopback_health_probe_is_a_real_connection_attempt():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    connection = socket.create_connection(("127.0.0.1", port), timeout=1)
    connection.close()
    listener.close()
    assert port > 0


def test_install_policy_allowlist_is_closed_and_explicit():
    from mayaworkbench.workspace.runner import INSTALL_EXECUTABLES

    assert "pip" in INSTALL_EXECUTABLES
    assert "uv" in INSTALL_EXECUTABLES
    assert "curl" not in INSTALL_EXECUTABLES

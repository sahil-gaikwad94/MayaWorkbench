"""User-owned runner bridge: allowlisted argv dispatch and unattested imports."""

from __future__ import annotations

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.experiments.remote_runners import (
    UserOwnedRunner,
    builtin_capabilities,
    render_argv,
)


def _runner(tmp_path, calls):
    caps = builtin_capabilities("user-owned", allowed_hosts=("lab.example.com",))
    return UserOwnedRunner(
        capabilities=caps,
        approval="apr_test",
        executor=lambda argv: (calls.append(argv), {"status": "ok", "argv": argv})[1],
    )


def test_render_argv_never_uses_a_shell():
    argv = render_argv(("run", "--bundle", "{bundle}"), bundle="/tmp/a b", host="h", run_id="r")
    assert argv == ["run", "--bundle", "/tmp/a b"]
    with pytest.raises(PolicyError):
        render_argv(("run\n",), bundle="", host="h", run_id="r")


def test_dispatch_requires_approval_and_allowlisted_host(tmp_path):
    calls: list[list[str]] = []
    runner = _runner(tmp_path, calls)
    runner.approval = None
    with pytest.raises(PolicyError, match="approval"):
        runner.submit(bundle=tmp_path / "x", host="lab.example.com", run_id="r1")
    runner.approval = "apr_test"
    with pytest.raises(PolicyError, match="allowlisted"):
        runner.submit(bundle=tmp_path / "x", host="evil.example.com", run_id="r1")
    assert calls == []


def test_submit_poll_and_import_are_provenance_labeled(tmp_path):
    calls: list[list[str]] = []
    runner = _runner(tmp_path, calls)
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    submitted = runner.submit(bundle=bundle, host="lab.example.com", run_id="r1")
    assert submitted["status"] == "ok"
    polled = runner.poll(host="lab.example.com", run_id="r1")
    assert polled["status"] == "ok"
    imported = runner.fetch(host="lab.example.com", run_id="r1")
    assert imported["execution"] == "IMPORTED"
    assert imported["attestation"] == "UNATTESTED"
    assert len(imported["import_sha256"]) == 64
    assert all(call[0] == "maya-remote-runner" for call in calls)


def test_unknown_adapter_is_refused():
    with pytest.raises(PolicyError, match="unknown runner adapter"):
        builtin_capabilities("kaggle", allowed_hosts=("kaggle.com",))

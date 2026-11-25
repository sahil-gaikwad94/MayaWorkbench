"""One task, one runtime workspace: tools, commands and diff agree on the tree."""

from __future__ import annotations

import pytest

from mayaworkbench.errors import PolicyError
from mayaworkbench.workspace.backends import BackendCommandRunner, RuntimeRef
from mayaworkbench.workspace.runner import CommandPolicy, CommandRunner
from mayaworkbench.workspace.runtime_workspace import (
    RuntimeWorkspace,
    content_diff,
    import_diff_to_worktree,
    manifest,
    populate_overlay,
    sanitize_source_tree,
    sanitized_policy_fingerprint,
)
from mayaworkbench.workspace.task_runtime import CommandRunnerTaskRuntime


def _local_runner(path):
    return CommandRunner(
        path,
        policy=CommandPolicy(
            profile="CPU_SMOKE",
            network_policy="ALLOW_WITH_APPROVAL",
            dependency_policy="DENY",
        ),
        run_home=path.parent / "home",
    )


def test_host_workspace_is_the_same_tree_tools_and_commands_use(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    ws = RuntimeWorkspace.for_host(tmp_path)
    assert ws.kind == "host"
    assert ws.path == tmp_path.resolve()
    assert ws.base_path == ws.path
    assert ws.content_diff() == []


def test_populate_overlay_is_clean_and_content_addressed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print(1)\n", encoding="utf-8")
    (source / ".env").write_text("MAYA_MODEL_API_KEY=sk-canary\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("metadata", encoding="utf-8")
    overlay = tmp_path / "overlay"
    copied = populate_overlay(source, overlay)
    assert list(copied) == ["app.py"]
    assert (overlay / "app.py").read_bytes() == (source / "app.py").read_bytes()
    assert not (overlay / ".env").exists()
    assert not (overlay / ".git").exists()


def test_sanitize_source_tree_is_secret_free_immutable_and_matches_overlay(tmp_path):
    """P0-01: a single canonical filter produces both task trees.

    The sanitized /src reference must exclude every path and content pattern
    the overlay excludes, must be non-writable, and must describe the exact same
    files as the writable overlay.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".env").write_text("MAYA_MODEL_API_KEY=sk-canary-value\n", encoding="utf-8")
    (source / ".npmrc").write_text("//registry.npmjs.org/:_authToken=secret\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    (source / "config").mkdir()
    (source / "config" / "credentials.json").write_text('{"token": "hunter2"}', encoding="utf-8")
    (source / "notes.txt").write_text(
        "token = AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8"
    )  # credential-shaped content in a benign filename
    outside = tmp_path / "host-secret.txt"
    outside.write_text("HOST-CANARY", encoding="utf-8")
    (source / "link.txt").symlink_to(outside)

    sanitized = tmp_path / "containers" / "one" / "src"
    overlay = tmp_path / "containers" / "one" / "work"
    source_digest = sanitize_source_tree(source, sanitized)
    overlay_digest = populate_overlay(source, overlay)

    assert source_digest == overlay_digest == {"app.py": source_digest["app.py"]}
    # Path-based exclusions.
    for excluded in (".env", ".npmrc", ".git", "config/credentials.json", "link.txt"):
        assert not (sanitized / excluded).exists(), excluded
        assert not (overlay / excluded).exists(), excluded
    # Content-based exclusion (benign filename, credential-shaped content).
    assert not (sanitized / "notes.txt").exists()
    assert not (overlay / "notes.txt").exists()
    # The ordinary project source survives, intact, in both trees.
    assert (sanitized / "app.py").read_text() == "print('ok')\n"
    assert (overlay / "app.py").read_text() == "print('ok')\n"
    # The reference is immutable: no owner write bit on files or directories.
    assert not (sanitized.stat().st_mode & 0o222)
    assert not ((sanitized / "app.py").stat().st_mode & 0o222)
    # The overlay stays writable so the agent can edit the project.
    assert (overlay / "app.py").stat().st_mode & 0o200
    # The policy fingerprint binds the manifest to the filter version.
    expected = sanitized_policy_fingerprint(source_digest)
    assert expected == sanitized_policy_fingerprint(overlay_digest)
    assert expected != sanitized_policy_fingerprint({"app.py": "different-digest"})


def test_frozen_sanitized_tree_can_be_cleaned_up(tmp_path):
    """A 0555/0444 snapshot must not leak because rmtree lacks write bits."""
    import shutil

    from mayaworkbench.workspace.runtime_workspace import unfreeze_tree

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("x", encoding="utf-8")
    (source / "lib").mkdir()
    (source / "lib" / "mod.py").write_text("y", encoding="utf-8")
    frozen = tmp_path / "frozen"
    sanitize_source_tree(source, frozen)
    unfreeze_tree(frozen)
    shutil.rmtree(frozen)
    assert not frozen.exists()


def test_content_diff_reports_added_modified_and_deleted():
    base = {"a": "1", "b": "2", "c": "3"}
    candidate = {"a": "1", "b": "changed", "d": "4"}
    changes = content_diff(base, candidate)
    assert [(item["path"], item["status"]) for item in changes] == [
        ("b", "modified"),
        ("c", "deleted"),
        ("d", "added"),
    ]


def test_write_through_runtime_is_visible_to_commands_and_diff(tmp_path):
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")
    ws = RuntimeWorkspace.for_host(tmp_path)
    runner = _local_runner(ws.path)
    runtime = CommandRunnerTaskRuntime(
        workspace=ws, runner=runner, trust_profile="LOCAL_TRUSTED"
    )
    runtime.write_file("app.py", b"print('new')\n")
    result = runtime.exec(["python", "-c", "print(open('app.py').read().strip())"], timeout_seconds=10)
    assert result.exit_code == 0
    assert "new" in result.stdout
    diff = ws.content_diff()
    assert [item["path"] for item in diff] == ["app.py"]
    assert diff[0]["status"] == "modified"


def test_apply_patch_is_all_or_nothing_and_resolves_paths(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ws = RuntimeWorkspace.for_host(tmp_path)
    runtime = CommandRunnerTaskRuntime(
        workspace=ws, runner=_local_runner(ws.path), trust_profile="LOCAL_TRUSTED"
    )
    patch = (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    assert runtime.apply_patch(patch) == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    with pytest.raises(ValueError, match="escapes"):
        runtime.write_file("../outside.py", b"x")


def test_import_diff_into_worktree_is_path_checked_and_exact(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("before\n", encoding="utf-8")
    overlay = tmp_path / "overlay"
    populate_overlay(source, overlay)
    (overlay / "app.py").write_text("after\n", encoding="utf-8")
    (overlay / "new.py").write_text("new\n", encoding="utf-8")
    ws = RuntimeWorkspace("container", source, overlay, manifest(source))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("before\n", encoding="utf-8")
    changes = import_diff_to_worktree(ws, worktree)
    assert {item["path"] for item in changes} == {"app.py", "new.py"}
    assert (worktree / "app.py").read_text(encoding="utf-8") == "after\n"
    assert (worktree / "new.py").read_text(encoding="utf-8") == "new\n"


def test_container_tools_and_commands_share_one_populated_overlay(tmp_path):
    """A tool write must be visible to the next command, and vice versa."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('base')\n", encoding="utf-8")
    seen: list[tuple[str, str]] = []

    class FakeBackend:
        backend_id = "fake-container"

        def capabilities(self):  # pragma: no cover - not used here
            raise AssertionError("not called")

        def provision(self):
            overlay = tmp_path / "containers" / "one" / "work"
            populate_overlay(source, overlay)
            return RuntimeRef("one", "fake-container", source, overlay, "img", "maya-one")

        def exec(self, runtime, argv, *, timeout, cancel_requested=None):
            # Stand in for `podman run -w /work`: read the same overlay the
            # gateway writes, and write a file the artifact collector will see.
            app = (runtime.overlay / "app.py").read_text(encoding="utf-8")
            seen.append(("read", app.strip()))
            (runtime.overlay / "from_command.txt").write_text("command-output\n", encoding="utf-8")
            from mayaworkbench.workspace.runner import CommandResult

            return CommandResult(
                argv=argv,
                exit_code=0,
                stdout=app,
                stderr="",
                duration_ms=1,
                timed_out=False,
                output_truncated=False,
            )

        def stop(self, runtime, *, reason, keep_overlay=False):
            del runtime, reason, keep_overlay

        def collect_artifacts(self, runtime):
            return [p for p in runtime.overlay.rglob("*") if p.is_file()]

    ws = RuntimeWorkspace.for_container(FakeBackend(), source)  # type: ignore[arg-type]
    runner = BackendCommandRunner(FakeBackend(), runtime=ws.runtime)  # type: ignore[arg-type]
    runtime = CommandRunnerTaskRuntime(
        workspace=ws, runner=runner, trust_profile="ROOTLESS_CONTAINER"
    )
    runtime.write_file("app.py", b"print('tool-edit')\n")
    result = runtime.exec(["python", "-c", "pass"], timeout_seconds=10)
    assert result.exit_code == 0
    assert seen == [("read", "print('tool-edit')")]
    paths = {path.name for path in runtime.collect_artifacts()}
    assert {"app.py", "from_command.txt"} <= paths
    assert {item["path"] for item in ws.content_diff()} == {"app.py", "from_command.txt"}


def test_container_workspace_resume_keeps_the_exact_overlay(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print(1)\n", encoding="utf-8")
    overlay = tmp_path / "containers" / "id" / "work"
    populate_overlay(source, overlay)
    (overlay / "app.py").write_text("print(2)\n", encoding="utf-8")
    ref = RuntimeRef("id", "rootless-container", source, overlay, "python:3.12-slim", "maya-id")
    ws = RuntimeWorkspace.resume(None, ref, source)  # type: ignore[arg-type]
    assert ws.path == overlay
    assert ws.content_diff()[0]["path"] == "app.py"
    assert ws.describe()["runtime_id"] == "id"

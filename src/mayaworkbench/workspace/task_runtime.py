"""One injected execution runtime shared by the agent, checks and verification.

A role must never construct its own hidden runner. The worker provisions one
:class:`TaskExecutionRuntime`, points every file tool at its active workspace,
and hands the same object to in-loop checks and to final verification. A trust
profile cannot be downgraded by one component into host execution: the runtime
records the profile and the verifier refuses a mismatched candidate runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from mayaworkbench.workspace.patch import apply_patch
from mayaworkbench.workspace.runner import CommandResult
from mayaworkbench.workspace.runtime_workspace import RuntimeWorkspace


@runtime_checkable
class TaskExecutionRuntime(Protocol):
    """The single boundary between model-proposed actions and side effects."""

    @property
    def trust_profile(self) -> str: ...

    def read_file(self, path: str) -> bytes: ...

    def write_file(self, path: str, data: bytes) -> None: ...

    def apply_patch(self, patch: str) -> list[str]: ...

    def exec(
        self, argv: list[str], *, timeout_seconds: int, run_home: Path | None = None
    ) -> CommandResult: ...

    def exec_script(
        self, script: str, *, timeout_seconds: int, run_home: Path | None = None
    ) -> CommandResult: ...

    def collect_artifacts(self) -> list[Path]: ...

    def stop(self, *, reason: str, keep_overlay: bool = False) -> None: ...


class CommandRunnerTaskRuntime:
    """Adapter over the worker's single runner + its resolved workspace."""

    def __init__(
        self,
        *,
        workspace: RuntimeWorkspace,
        runner: object,
        trust_profile: str,
    ):
        self.workspace = workspace
        self.runner = runner  # CommandRunner or BackendCommandRunner
        self._trust_profile = trust_profile

    @property
    def trust_profile(self) -> str:
        return self._trust_profile

    def _target(self, path: str) -> Path:
        root = self.workspace.path.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"path escapes the task workspace: {path}")
        return target

    def read_file(self, path: str) -> bytes:
        return self._target(path).read_bytes()

    def write_file(self, path: str, data: bytes) -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def apply_patch(self, patch: str) -> list[str]:
        from mayaworkbench.workspace.policy import contains_secret_material

        if contains_secret_material(patch):
            raise ValueError("credential-shaped content is not writable")
        from mayaworkbench.workspace.patch import parse_patch

        files = parse_patch(patch)
        contents: dict[str, bytes] = {}
        for item in files:
            target = self._target(item.path)
            if target.exists():
                contents[item.path] = target.read_bytes()
        applied = apply_patch(patch, contents)  # all-or-nothing: computed first
        for result in applied:
            target = self._target(result.path)
            if result.deleted:
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result.data)
        return [result.path for result in applied]

    def exec(
        self, argv: list[str], *, timeout_seconds: int, run_home: Path | None = None
    ) -> CommandResult:
        del run_home  # the worker's single runner owns its run home
        return self.runner.run(argv, timeout_seconds=timeout_seconds)  # type: ignore[attr-defined]

    def exec_script(
        self, script: str, *, timeout_seconds: int, run_home: Path | None = None
    ) -> CommandResult:
        del run_home
        return self.runner.run_script(script, timeout_seconds=timeout_seconds)  # type: ignore[attr-defined]

    def collect_artifacts(self) -> list[Path]:
        return self.workspace.collect_artifacts()

    def stop(self, *, reason: str, keep_overlay: bool = False) -> None:
        self.workspace.stop(reason=reason, keep_overlay=keep_overlay)
        stop: Callable[..., None] | None = getattr(self.runner, "stop", None)
        if stop is not None:
            stop(reason=reason, keep_overlay=keep_overlay)

    def describe(self) -> dict[str, object]:
        info = self.workspace.describe()
        info.update(
            {
                "trust_profile": self._trust_profile,
                "content_diff": self.workspace.content_diff(),
            }
        )
        return info


__all__ = ["TaskExecutionRuntime", "CommandRunnerTaskRuntime"]

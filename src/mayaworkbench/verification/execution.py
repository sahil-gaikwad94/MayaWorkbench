"""Baseline and candidate execution for verification checks.

A baseline is a fresh ``git worktree`` at the pinned base commit with the
snapshot's working-tree patch applied. That reproduces the exact pre-change
content without touching the user's checkout and without trusting the agent's
workspace.

Candidate checks run in the same workspace the agent used, after the patch was
captured. Both go through :class:`CommandRunner`, so the same argv validation,
timeout, process-group cleanup and resource limits apply.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mayaworkbench.domain.enums import CheckOutcome
from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import VerificationError
from mayaworkbench.workspace.runner import CommandPolicy, CommandResult, CommandRunner


def _git_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return env


@dataclass
class BaselineWorkspace:
    """A detached worktree containing the pre-change snapshot content."""

    path: Path
    root: Path
    base_commit: str
    applied_patch: bool = False

    @classmethod
    def create(
        cls,
        *,
        repository_root: Path,
        base_commit: str,
        patch_text: str | None,
        destination: Path,
    ) -> "BaselineWorkspace":
        destination = Path(destination)
        if destination.exists():
            raise VerificationError(f"Baseline destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "worktree",
                "add",
                "--detach",
                "--force",
                str(destination),
                base_commit,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_git_env(),
        )
        if result.returncode != 0:
            raise VerificationError(
                "Could not create the baseline worktree: "
                + (result.stderr or result.stdout).strip()[:2000],
                hint="The repository may have moved or the commit may be missing.",
            )
        baseline = cls(path=destination, root=Path(repository_root), base_commit=base_commit)
        if patch_text:
            patch_file = destination.parent / f"{destination.name}.snapshot.patch"
            patch_file.write_text(patch_text, encoding="utf-8")
            apply_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(destination),
                    "apply",
                    "--whitespace=nowarn",
                    "--binary",
                    str(patch_file),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=_git_env(),
            )
            try:
                patch_file.unlink()
            except OSError:  # pragma: no cover
                pass
            if apply_result.returncode != 0:
                baseline.remove()
                raise VerificationError(
                    "The pinned snapshot patch does not apply to the base commit: "
                    + (apply_result.stderr or apply_result.stdout).strip()[:2000]
                )
            baseline.applied_patch = True
        return baseline

    def remove(self) -> None:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "worktree",
                "remove",
                "--force",
                str(self.path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=_git_env(),
        )
        if result.returncode != 0 and self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "prune"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=_git_env(),
        )

    def __enter__(self) -> "BaselineWorkspace":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.remove()


def make_runner(
    workspace: Path,
    *,
    constraints: TaskConstraints,
    run_home: Path,
) -> CommandRunner:
    return CommandRunner(
        Path(workspace),
        policy=CommandPolicy.from_constraints(constraints),
        run_home=Path(run_home),
    )


def invariant_marker(result: CommandResult) -> dict[str, Any] | None:
    """Return the structured marker an invariant script printed, if any.

    The generated scripts print one JSON object as their last output line. The
    marker is what distinguishes "the invariant does not apply here" (torch is
    not installed, the target has no such module) from "the invariant ran and
    failed". Exit codes alone would report a skip as a failure.
    """
    import json

    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith('{"status"'):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("status") in {"PASS", "FAIL", "SKIP"}:
            return payload
    return None


def invariant_reason(result: CommandResult) -> str:
    marker = invariant_marker(result)
    if marker and marker.get("reason"):
        return str(marker["reason"])
    return outcome_reason(result)


def invariant_outcome(result: CommandResult) -> CheckOutcome:
    """Map an invariant script result, honouring its explicit SKIP marker."""
    if result.blocked:
        return CheckOutcome.BLOCKED
    if result.cancelled:
        return CheckOutcome.INCONCLUSIVE
    if result.timed_out:
        return CheckOutcome.FAILED
    marker = invariant_marker(result)
    if marker is not None:
        status = marker["status"]
        if status == "PASS":
            return CheckOutcome.PASSED if result.exit_code == 0 else CheckOutcome.FAILED
        if status == "SKIP":
            return CheckOutcome.SKIPPED
        return CheckOutcome.FAILED
    # A script that printed no marker produced no verdict, even if it exited 0.
    return CheckOutcome.INCONCLUSIVE if result.exit_code == 0 else CheckOutcome.FAILED


def command_outcome(result: CommandResult) -> CheckOutcome:
    if result.blocked:
        return CheckOutcome.BLOCKED
    if result.cancelled:
        return CheckOutcome.INCONCLUSIVE
    if result.timed_out:
        return CheckOutcome.FAILED
    if result.exit_code == 0:
        return CheckOutcome.PASSED
    return CheckOutcome.FAILED


def outcome_reason(result: CommandResult) -> str:
    if result.blocked:
        return result.blocked_reason or "command was refused by policy"
    if result.cancelled:
        return "command was cancelled"
    if result.timed_out:
        return f"command timed out after {result.duration_ms} ms"
    if result.exit_code == 0:
        return "command exited 0"
    if result.exit_code is not None and result.exit_code < 0:
        return f"command was killed by signal {-result.exit_code}"
    return f"command exited {result.exit_code}"


def execute_check(
    *,
    owner: str,
    workspace: Path,
    constraints: TaskConstraints,
    run_home: Path,
    argv: list[str] | None,
    script: str | None,
    timeout_seconds: int,
    runtime: Any | None = None,
) -> CommandResult:
    """Run one declared check on one tree and record which tree it ran on.

    When ``runtime`` is supplied it is the task's single injected runtime, so a
    candidate check executes against the exact overlay the agent edited. It is
    never used for the baseline, which must stay in a separate clean tree.
    """
    del owner  # retained so callers can label the tree in their own records
    if runtime is not None:
        if script is not None:
            return runtime.exec_script(script, timeout_seconds=timeout_seconds, run_home=run_home)
        if not argv:
            raise VerificationError("A command check has no argv")
        return runtime.exec(list(argv), timeout_seconds=timeout_seconds, run_home=run_home)
    runner = make_runner(workspace, constraints=constraints, run_home=run_home)
    if script is not None:
        return runner.run_script(script, timeout_seconds=timeout_seconds)
    if not argv:
        raise VerificationError("A command check has no argv")
    return runner.run(list(argv), timeout_seconds=timeout_seconds)


def sha256_file(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VerificationError(f"Cannot read {path} for patch verification: {exc}") from exc
    return hashlib.sha256(data).hexdigest()


def verify_patch_reproduces(
    *,
    repository_root: Path,
    base_commit: str,
    patch_text: str,
    candidate_workspace: Path,
    paths: list[str],
    destination: Path,
) -> dict:
    """Apply the stored patch to a clean base tree and compare file bytes.

    Returns a record with ``ok``, ``mismatches``, ``missing`` and
    ``extra_paths``. A mismatch means the patch is not a faithful description of
    the tree the checks ran against.
    """
    baseline = BaselineWorkspace.create(
        repository_root=repository_root,
        base_commit=base_commit,
        patch_text=None,
        destination=destination,
    )
    try:
        patch_file = destination.parent / f"{destination.name}.candidate.patch"
        patch_file.write_text(patch_text, encoding="utf-8")
        apply_result = subprocess.run(
            [
                "git",
                "-C",
                str(baseline.path),
                "apply",
                "--whitespace=nowarn",
                "--binary",
                str(patch_file),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_git_env(),
        )
        try:
            patch_file.unlink()
        except OSError:  # pragma: no cover
            pass
        if apply_result.returncode != 0:
            return {
                "ok": False,
                "mismatches": [],
                "missing": [],
                "error": (
                    "the stored patch does not apply to the base commit: "
                    + (apply_result.stderr or apply_result.stdout).strip()[:2000]
                ),
            }
        mismatches: list[dict] = []
        missing: list[str] = []
        for path in sorted(set(paths)):
            expected = sha256_file(baseline.path / path)
            actual = sha256_file(candidate_workspace / path)
            if expected is None and actual is None:
                continue
            if expected is None:
                missing.append(path)
                continue
            if expected != actual:
                mismatches.append(
                    {
                        "path": path,
                        "baseline_patch_sha256": expected,
                        "candidate_sha256": actual,
                    }
                )
        return {
            "ok": not mismatches and not missing,
            "mismatches": mismatches,
            "missing": missing,
            "files_compared": len(set(paths)),
        }
    finally:
        baseline.remove()


__all__ = [
    "BaselineWorkspace",
    "make_runner",
    "command_outcome",
    "outcome_reason",
    "execute_check",
    "verify_patch_reproduces",
    "sha256_file",
]

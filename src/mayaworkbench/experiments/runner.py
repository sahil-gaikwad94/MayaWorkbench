"""Execute one bounded experiment run in an isolated checkout.

The runner is deliberately boring: create a detached worktree at the pinned
snapshot (applying the captured patch when the snapshot is a worktree), verify
every indexed file against its recorded hash, hash the declared inputs, start a
command through the same policy-enforcing :class:`CommandRunner` the task
workflow uses, ingest metrics, then remove the worktree.

What it does not do, by design:

* no shell, command chaining, network, or dependency installation;
* no execution for ``FULL_EXPERIMENT``/``LARGE_TRAINING`` profiles — those need
  external compute and are recorded as refused rather than downgraded;
* no claim that a worktree is a security sandbox. Repository code runs with the
  user's privileges; the process limits and offline policy are real, the
  filesystem boundary is not.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mayaworkbench.config import ExperimentSettings
from mayaworkbench.domain.enums import (
    ExperimentProfile,
    ExperimentRunStatus,
)
from mayaworkbench.domain.models import (
    ExperimentDefinition,
    ExperimentProtocol,
    Repository,
    RepositorySnapshot,
)
from mayaworkbench.errors import ExperimentError, PolicyError
from mayaworkbench.experiments.metrics import ExtractedMetrics, extract_metrics
from mayaworkbench.ids import default_host_id
from mayaworkbench.repositories.git import GitClient
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import FileStore
from mayaworkbench.workspace.materialize import (
    apply_snapshot_state,
    needs_patch_application,
    verify_snapshot_content,
)
from mayaworkbench.workspace.policy import normalize_rel
from mayaworkbench.workspace.runner import CommandPolicy, CommandResult, CommandRunner

TREATMENT_PLACEHOLDER = "{seed}"
EXPERIMENT_SEED_ENV = "MAYA_EXPERIMENT_SEED"
EXPERIMENT_ID_ENV = "MAYA_EXPERIMENT_ID"

_LOCAL_PROFILES = {
    ExperimentProfile.CPU_SMOKE: "CPU_SMOKE",
    ExperimentProfile.SMALL_VALIDATION: "USER_APPROVED_LOCAL",
}
_REFUSED_PROFILES = {
    ExperimentProfile.STATIC: (
        "STATIC experiments do not execute a command; record externally produced "
        "metrics with `maya experiment record`"
    ),
    ExperimentProfile.FULL_EXPERIMENT: (
        "FULL_EXPERIMENT requires user-owned compute and is not executed by the "
        "Phase 5 local runner"
    ),
    ExperimentProfile.LARGE_TRAINING: (
        "LARGE_TRAINING requires an explicit hardware plan and is not executed by "
        "the Phase 5 local runner"
    ),
}


@dataclass
class ExecutionResult:
    status: ExperimentRunStatus
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    blocked: bool = False
    cancelled: bool = False
    duration_ms: int | None = None
    cpu_user_ms: int | None = None
    cpu_system_ms: int | None = None
    max_rss_kb: int | None = None
    output_text: str = ""
    output_truncated: bool = False
    metrics: ExtractedMetrics | None = None
    metrics_error: str | None = None
    error: str | None = None
    termination_reason: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    network_isolated: bool = False
    network_enforcement: str = "unavailable"
    verified_inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    workspace_path: str | None = None
    workspace_kept: bool = False
    workspace_dirty: list[str] = field(default_factory=list)


class ExperimentRunner:
    def __init__(
        self,
        *,
        home: Path,
        settings: ExperimentSettings,
        artifacts: ArtifactStore,
        files: FileStore,
    ):
        self.home = Path(home).resolve()
        self.settings = settings
        self.artifacts = artifacts
        self.files = files
        self.root = self.home / "experiments"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ------------------------------------------------------------------ public
    def execute(
        self,
        *,
        definition: ExperimentDefinition,
        run_id: str,
        attempt: int,
        seed: int,
        repository: Repository,
        snapshot: RepositorySnapshot,
        keep_workspace: bool = False,
    ) -> ExecutionResult:
        if definition.profile not in _LOCAL_PROFILES:
            reason = _REFUSED_PROFILES.get(
                definition.profile,
                f"profile {definition.profile.value} does not execute locally",
            )
            return ExecutionResult(
                status=ExperimentRunStatus.BLOCKED,
                command=list(definition.command),
                blocked=True,
                error=reason,
                termination_reason="PROFILE_NOT_LOCAL",
                environment=self._environment(seed),
            )
        if not definition.command:
            raise ExperimentError(
                "An executable experiment needs a command",
                hint="Use `maya experiment record` for externally produced metrics.",
            )
        if snapshot.commit_sha is None:
            raise ExperimentError("The pinned snapshot has no resolved commit SHA")

        argv = [self._substitute(arg, seed=seed) for arg in definition.command]
        if len(argv) > 1 and argv[0] in {"python", "python3"} and argv[1] == "-c":
            raise ExperimentError(
                "Inline python -c commands are not allowed for experiments",
                hint="Put the benchmark in a script inside the repository so its hash is recorded.",
            )

        timeout_seconds = min(
            definition.timeout_seconds, self.settings.max_runtime_seconds
        )
        policy = CommandPolicy(
            profile=_LOCAL_PROFILES[definition.profile],
            network_policy="DENY_BY_DEFAULT",
            dependency_policy="DENY",
            max_runtime_seconds=timeout_seconds,
            max_output_bytes=self.settings.max_output_bytes,
            extra_env={
                EXPERIMENT_SEED_ENV: str(seed),
                EXPERIMENT_ID_ENV: definition.id,
                "PYTHONHASHSEED": str(seed % (2**32)),
            },
        )
        keep_requested = keep_workspace or self.settings.keep_workspaces
        run_root = self.root / normalize_rel(definition.id) / "runs" / normalize_rel(run_id)
        repo_path = run_root / "repo"
        run_home = run_root / "home"
        if run_root.exists():
            raise ExperimentError(
                f"Run directory already exists: {run_root}",
                hint="Refusing to reuse a directory that may contain earlier results.",
            )
        run_root.mkdir(parents=True, mode=0o700)
        run_home.mkdir(mode=0o700)

        repo_root = Path(repository.root_path).resolve()
        git = GitClient(repo_root)
        head_before = git.head_commit()
        status_before = git.status_porcelain()
        created = False
        try:
            git.run("worktree", "add", "--detach", str(repo_path), snapshot.commit_sha)
            created = True
            worktree_git = GitClient(repo_path)
            if worktree_git.head_commit() != snapshot.commit_sha:
                raise ExperimentError("The experiment worktree is not at the pinned commit")
            if needs_patch_application(snapshot):
                apply_snapshot_state(
                    git,
                    worktree_git,
                    snapshot,
                    artifacts=self.artifacts,
                    files=self.files,
                )
            verified_files = verify_snapshot_content(
                snapshot, repo_path, files=self.files
            )
            head_after = git.head_commit()
            status_after = git.status_porcelain()
            if head_after != head_before or status_after != status_before:
                raise ExperimentError(
                    "The original checkout changed while the experiment worktree was "
                    "created; refusing to run",
                    hint="Stop other processes writing to the repository, then retry.",
                )

            environment = self._environment(seed)
            environment["verified_files"] = verified_files
            environment["original_checkout_unchanged"] = True
            input_warnings: list[str] = []
            try:
                verified_inputs = self._verify_inputs(
                    definition.protocol, repo_path, warnings_out=input_warnings
                )
            except ExperimentError as exc:
                warnings = [*input_warnings, *getattr(exc, "warnings", [])]
                return ExecutionResult(
                    status=ExperimentRunStatus.BLOCKED,
                    command=argv,
                    blocked=True,
                    error=str(exc),
                    termination_reason="INPUT_VERIFICATION_FAILED",
                    environment=environment,
                    warnings=warnings,
                    workspace_path=str(repo_path),
                    workspace_kept=keep_requested,
                )

            runner = CommandRunner(repo_path, policy=policy, run_home=run_home)
            usage_before = _rusage_children()
            started = time.monotonic()
            result: CommandResult | None = None
            error: str | None = None
            try:
                result = runner.run(argv, timeout_seconds=timeout_seconds)
            except PolicyError as exc:
                error = str(exc)
            duration_ms = int((time.monotonic() - started) * 1000)
            usage_after = _rusage_children()

            warnings = list(verified_inputs.pop("warnings", []))
            if error is not None:
                return ExecutionResult(
                    status=ExperimentRunStatus.BLOCKED,
                    command=argv,
                    blocked=True,
                    duration_ms=duration_ms,
                    error=error,
                    termination_reason="POLICY_REFUSED",
                    environment=environment,
                    verified_inputs=verified_inputs,
                    warnings=warnings,
                    workspace_path=str(repo_path),
                    workspace_kept=keep_requested,
                )
            assert result is not None

            output_text = _combine_output(result.stdout, result.stderr)
            cpu_user_ms, cpu_system_ms, max_rss_kb = _usage_delta(
                usage_before, usage_after
            )
            status, termination = _status_for(result)
            metrics: ExtractedMetrics | None = None
            metrics_error: str | None = None
            if result.succeeded:
                try:
                    metrics = extract_metrics(
                        source=definition.metrics_source.kind,
                        stdout=result.stdout,
                        workspace=repo_path,
                        path=definition.metrics_source.path,
                        metrics=definition.metrics,
                        max_observations=min(
                            definition.budget.max_metric_observations,
                            self.settings.max_metric_observations,
                        ),
                        max_file_bytes=self.settings.max_metric_file_bytes,
                    )
                    if metrics.missing_declared:
                        metrics_error = (
                            "declared metrics were not produced: "
                            + ", ".join(metrics.missing_declared)
                        )
                except ExperimentError as exc:
                    metrics_error = str(exc)

            dirty = GitClient(repo_path).status_porcelain()
            dirty_files = [
                line[3:] if len(line) > 3 else line
                for line in dirty.splitlines()
                if line.strip()
            ]
            if dirty_files:
                warnings.append(
                    f"the experiment wrote {len(dirty_files)} path(s) inside its isolated "
                    "workspace; they are not part of any candidate patch"
                )
            return ExecutionResult(
                status=status,
                command=argv,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                blocked=result.blocked,
                cancelled=result.cancelled,
                duration_ms=duration_ms,
                cpu_user_ms=cpu_user_ms,
                cpu_system_ms=cpu_system_ms,
                max_rss_kb=max_rss_kb,
                output_text=output_text,
                output_truncated=result.output_truncated,
                metrics=metrics,
                metrics_error=metrics_error,
                error=result.blocked_reason if result.blocked else None,
                termination_reason=termination,
                environment=environment,
                network_isolated=result.network_isolated,
                network_enforcement=result.network_enforcement,
                verified_inputs=verified_inputs,
                warnings=warnings,
                workspace_path=str(repo_path),
                workspace_kept=keep_requested,
                workspace_dirty=dirty_files,
            )
        finally:
            if created:
                if not keep_requested:
                    _remove_worktree(git, repo_path)
                    shutil.rmtree(run_root, ignore_errors=True)
            else:
                shutil.rmtree(run_root, ignore_errors=True)

    # ----------------------------------------------------------------- helpers
    def _substitute(self, value: str, *, seed: int) -> str:
        if TREATMENT_PLACEHOLDER in value:
            if value.count(TREATMENT_PLACEHOLDER) > 1:
                raise ExperimentError(
                    f"an argv entry may contain {TREATMENT_PLACEHOLDER} only once"
                )
            return value.replace(TREATMENT_PLACEHOLDER, str(seed))
        return value

    def _environment(self, seed: int) -> dict[str, Any]:
        return {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "executable": str(sys.executable),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "memory_mb": _total_memory_mb(),
            "host_id": default_host_id(),
            "seed": seed,
            "cwd": "isolated worktree",
            "network_policy": "DENY_BY_DEFAULT",
            "model_calls": "none",
        }

    def _verify_inputs(
        self,
        protocol: ExperimentProtocol,
        workspace: Path,
        *,
        warnings_out: list[str],
    ) -> dict[str, Any]:
        verified: dict[str, Any] = {"warnings": warnings_out}
        if protocol.evaluation.script_path:
            digest = _hash_workspace_file(
                workspace, protocol.evaluation.script_path, label="evaluation script"
            )
            verified["evaluation_script_sha256"] = digest
            if protocol.evaluation.script_hash and digest != protocol.evaluation.script_hash:
                raise ExperimentError(
                    "The evaluation script does not match the declared hash",
                    hint="The protocol would describe different code than it ran.",
                )
        if protocol.environment.lockfile_path:
            digest = _hash_workspace_file(
                workspace, protocol.environment.lockfile_path, label="lockfile"
            )
            verified["lockfile_sha256"] = digest
            if (
                protocol.environment.lockfile_hash
                and digest != protocol.environment.lockfile_hash
            ):
                raise ExperimentError(
                    "The dependency lockfile does not match the declared hash"
                )
        if protocol.dataset.path:
            dataset = Path(protocol.dataset.path).expanduser()
            if not dataset.is_absolute():
                dataset = (workspace / dataset).resolve()
            if not dataset.exists() or not dataset.is_file():
                warnings_out.append(
                    f"dataset path is not a readable file: {protocol.dataset.path}"
                )
                verified["dataset_fingerprint_verified"] = False
            else:
                size = dataset.stat().st_size
                if size > self.settings.max_dataset_fingerprint_bytes:
                    warnings_out.append(
                        f"dataset is {size} bytes, above the fingerprint limit; its "
                        "identity was not verified"
                    )
                    verified["dataset_fingerprint_verified"] = False
                else:
                    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
                    verified["dataset_sha256"] = digest
                    verified["dataset_fingerprint_verified"] = bool(
                        protocol.dataset.fingerprint
                    )
                    if (
                        protocol.dataset.fingerprint
                        and digest != protocol.dataset.fingerprint
                    ):
                        raise ExperimentError(
                            "The dataset does not match the declared fingerprint",
                            hint="Record a new experiment definition for different data.",
                        )
        if protocol.config.resolved:
            digest = hashlib.sha256(
                json.dumps(
                    protocol.config.resolved,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            verified["config_sha256"] = digest
            if protocol.config.hash and digest != protocol.config.hash:
                raise ExperimentError(
                    "The resolved config does not match the declared config hash"
                )
        return verified


def _combine_output(stdout: str, stderr: str) -> str:
    if not stderr:
        return stdout
    if not stdout:
        return stderr
    return f"{stdout}\n--- stderr ---\n{stderr}"


def _status_for(result: CommandResult) -> tuple[ExperimentRunStatus, str]:
    if result.blocked:
        return ExperimentRunStatus.BLOCKED, "POLICY_BLOCKED"
    if result.timed_out:
        return ExperimentRunStatus.TIMED_OUT, "TIMEOUT"
    if result.cancelled:
        return ExperimentRunStatus.CANCELLED, "CANCELLED"
    if result.exit_code != 0:
        return ExperimentRunStatus.FAILED, f"EXIT_{result.exit_code}"
    return ExperimentRunStatus.COMPLETED, "EXIT_0"


def _hash_workspace_file(workspace: Path, rel: str, *, label: str) -> str:
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ExperimentError(f"{label} path must be workspace-relative: {rel}")
    root = Path(workspace).resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ExperimentError(f"{label} path escapes the workspace: {rel}")
    if not target.exists() or not target.is_file():
        raise ExperimentError(f"Declared {label} does not exist in the snapshot: {rel}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _remove_worktree(git: GitClient, path: Path) -> None:
    if not path.exists():
        return
    result = git.run("worktree", "remove", "--force", str(path), check=False)
    if result.returncode != 0:
        git.run("worktree", "prune", check=False)
        git.run("worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _total_memory_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(int(line.split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _rusage_children():  # type: ignore[no-untyped-def]
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    return resource.getrusage(resource.RUSAGE_CHILDREN)


def _usage_delta(before, after) -> tuple[int | None, int | None, int | None]:  # type: ignore[no-untyped-def]
    if before is None or after is None:
        return None, None, None
    user_ms = max(0, int((after.ru_utime - before.ru_utime) * 1000))
    system_ms = max(0, int((after.ru_stime - before.ru_stime) * 1000))
    # ``ru_maxrss`` is a high-water mark across all children of this process,
    # not a delta, so it is reported as a basis-qualified upper bound.
    return user_ms, system_ms, int(after.ru_maxrss)


__all__ = ["ExperimentRunner", "ExecutionResult", "EXPERIMENT_SEED_ENV"]

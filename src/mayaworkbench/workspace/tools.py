"""One typed gateway for every tool the engineering agent may use.

The agent never receives arbitrary authority. Each invocation:

1. is validated against a typed argument schema;
2. is recorded as a ``ToolCall`` before any side effect;
3. passes the task's path/scope policy;
4. for writes, requires a consumed ``WRITE_ACCESS`` approval whose spec hash
   matches the contract and plan;
5. for commands, requires a consumed ``COMMAND_EXECUTION`` approval whose spec
   hash matches the run's command policy, then passes the runner's allowlist;
6. is audited with redacted arguments and a content-addressed output artifact.

Tool output is untrusted data. Nothing in a file, command output or model
observation can grant permissions; approvals are durable records checked here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ApprovalStatus,
    ArtifactKind,
    EventType,
    ToolCallStatus,
)
from mayaworkbench.domain.models import (
    ApprovalRequest,
    Record,
    Task,
    TaskContract,
    ToolCall,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import ApprovalError, BudgetError, PolicyError, ToolError
from mayaworkbench.ids import new_ulid
from mayaworkbench.repositories.git import GitClient
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import EventStore, ToolCallStore
from mayaworkbench.workspace.patch import PatchError, apply_patch, parse_patch
from mayaworkbench.workspace.policy import (
    PathPolicy,
    PolicyViolation,
    contains_secret_material,
    is_secret_path,
    normalize_rel,
)
from mayaworkbench.workspace.runner import CommandPolicy, CommandResult, CommandRunner

ToolName = Literal[
    "read_file",
    "list_files",
    "search_text",
    "apply_patch",
    "write_file",
    "delete_file",
    "git_status",
    "git_diff",
    "run_command",
]

MAX_READ_BYTES = 300_000
MAX_WRITE_BYTES = 2_000_000
MAX_SEARCH_BYTES = 20_000_000
MAX_SEARCH_FILE_BYTES = 1_000_000

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
)


class ToolBlocked(PolicyError):
    """A tool is unavailable under the task's authorization or scope."""


def redact(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class ReadFileArgs(Record):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ListFilesArgs(Record):
    path: str = "."
    depth: int = Field(default=2, ge=1, le=8)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return "." if value in {"", "."} else normalize_rel(value)


class SearchTextArgs(Record):
    query: str = Field(min_length=2, max_length=200)
    path: str | None = None
    limit: int = Field(default=40, ge=1, le=100)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return None if value in {None, "", "."} else normalize_rel(str(value))


class ApplyPatchArgs(Record):
    patch: str = Field(min_length=1)


class WriteFileArgs(Record):
    path: str
    content: str
    overwrite: bool = False

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return normalize_rel(value)


class DeleteFileArgs(Record):
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return normalize_rel(value)


class GitArgs(Record):
    pass


class RunCommandArgs(Record):
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)


@dataclass
class ToolResult:
    tool: str
    status: ToolCallStatus
    summary: str
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    artifact_id: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    error: str | None = None

    def observation(self) -> str:
        payload = {
            "tool": self.tool,
            "status": self.status.value,
            "summary": self.summary,
            "output": self.output,
            "exit_code": self.exit_code,
            "error": self.error,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ToolGateway:
    def __init__(
        self,
        *,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        workspace: Path,
        policy: PathPolicy,
        runner: CommandRunner,
        calls: ToolCallStore,
        artifacts: ArtifactStore,
        events: EventStore,
        write_approval: ApprovalRequest | None,
        expected_write_spec: str,
        command_approval: ApprovalRequest | None,
        expected_command_spec: str,
        cancel_requested: Any | None = None,
    ):
        self.run = run
        self.task = task
        self.contract = contract
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.runner = runner
        self.calls = calls
        self.artifacts = artifacts
        self.events = events
        self.write_approval = write_approval
        self.expected_write_spec = expected_write_spec
        self.command_approval = command_approval
        self.expected_command_spec = expected_command_spec
        self.cancel_requested = cancel_requested
        self._cancelled = False

    # ------------------------------------------------------------------ public
    def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        if tool not in {
            "read_file",
            "list_files",
            "search_text",
            "apply_patch",
            "write_file",
            "delete_file",
            "git_status",
            "git_diff",
            "run_command",
        }:
            raise ToolError(f"unknown tool: {tool}")
        parsed = self._parse(tool, arguments)
        auditor = self._audit_arguments(tool, parsed)
        call = ToolCall(
            id="tool_" + new_ulid(),
            run_id=self.run.id,
            task_id=self.run.task_id,
            project_id=self.run.project_id,
            tool_name=tool,
            arguments=auditor,
            arguments_sha256=hashlib.sha256(
                json.dumps(parsed, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            status=ToolCallStatus.STARTED,
        )
        self.calls.create(call)
        self._event(
            EventType.TOOL_INVOKED,
            payload={"tool": tool, "call_id": call.id, "status": "STARTED"},
        )
        started = utcnow()
        try:
            result = self._dispatch(tool, parsed, call)
        except (PolicyViolation, ToolBlocked) as exc:
            result = ToolResult(
                tool=tool,
                status=ToolCallStatus.BLOCKED,
                summary=f"Blocked: {exc}",
                error=str(exc),
                call_id=call.id,
            )
        except ApprovalError:
            raise
        except (ToolError, PatchError, PolicyError, OSError, ValueError) as exc:
            result = ToolResult(
                tool=tool,
                status=ToolCallStatus.FAILED,
                summary=f"Failed: {exc}",
                error=str(exc)[:2000],
                call_id=call.id,
            )
        result.call_id = call.id
        result.output = redact(result.output)
        if result.output:
            result.artifact_id = self._store_output(
                call,
                result.output,
                kind=(
                    ArtifactKind.COMMAND_OUTPUT
                    if tool == "run_command"
                    else ArtifactKind.TOOL_OUTPUT
                ),
            )
        duration = int((utcnow() - started).total_seconds() * 1000)
        result.duration_ms = result.duration_ms or duration
        self.calls.complete(
            call.id,
            status=result.status,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            error=result.error,
            output_artifact_id=result.artifact_id,
            details={"data": result.data, "summary": result.summary},
        )
        self._event(
            EventType.TOOL_INVOKED if result.status != ToolCallStatus.BLOCKED else EventType.TOOL_BLOCKED,
            payload={
                "tool": tool,
                "call_id": call.id,
                "status": result.status.value,
                "duration_ms": result.duration_ms,
                "error": result.error,
            },
        )
        return result

    # --------------------------------------------------------------- validation
    def _parse(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        models = {
            "read_file": ReadFileArgs,
            "list_files": ListFilesArgs,
            "search_text": SearchTextArgs,
            "apply_patch": ApplyPatchArgs,
            "write_file": WriteFileArgs,
            "delete_file": DeleteFileArgs,
            "git_status": GitArgs,
            "git_diff": GitArgs,
            "run_command": RunCommandArgs,
        }
        try:
            return models[tool].model_validate(arguments).model_dump(mode="json")
        except Exception as exc:
            raise ToolError(f"invalid arguments for {tool}: {exc}") from exc

    def _audit_arguments(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "apply_patch":
            patch = args.get("patch", "")
            try:
                files = [item.path for item in parse_patch(patch)]
            except PatchError:
                files = []
            return {
                "files": files,
                "bytes": len(patch.encode("utf-8")),
                "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            }
        if tool == "write_file":
            content = str(args.get("content", ""))
            return {
                "path": args.get("path"),
                "bytes": len(content.encode("utf-8")),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "overwrite": bool(args.get("overwrite")),
            }
        if tool == "run_command":
            argv = [redact(str(item)) for item in args.get("argv", [])]
            return {"argv": argv, "timeout_seconds": args.get("timeout_seconds")}
        if tool == "search_text":
            return {**args, "query": redact(str(args.get("query", "")))}
        return dict(args)

    def _require_write(self) -> None:
        approval = self.write_approval
        if approval is None:
            raise ApprovalError("No write approval is attached to this run")
        if approval.status != ApprovalStatus.CONSUMED:
            raise ApprovalError(
                f"Write approval is {approval.status.value}, not consumed",
                hint="Approve the pending write access request before editing.",
            )
        if approval.spec_sha256 != self.expected_write_spec:
            raise ApprovalError(
                "The write approval does not match the current contract and plan",
                hint="Request a new approval after changing the contract or plan.",
            )

    def _require_command(self) -> None:
        approval = self.command_approval
        if approval is None:
            # A contract may deliberately omit COMMAND_EXECUTION. That is a
            # policy refusal, not a fatal mid-run approval failure: the agent can
            # continue with static tools or finish and explain the limitation.
            raise ToolBlocked(
                "This task contract does not authorize command execution; "
                "no command was run"
            )
        if approval.status != ApprovalStatus.CONSUMED:
            raise ApprovalError(
                f"Command approval is {approval.status.value}, not consumed",
                hint="Approve command execution before running repository code.",
            )
        if approval.spec_sha256 != self.expected_command_spec:
            raise ApprovalError("The command approval does not match this run's policy")

    # ---------------------------------------------------------------- dispatch
    def _dispatch(self, tool: str, args: dict[str, Any], call: ToolCall) -> ToolResult:
        if tool == "read_file":
            return self._read_file(args)
        if tool == "list_files":
            return self._list_files(args)
        if tool == "search_text":
            return self._search_text(args)
        if tool == "git_status":
            return self._git_status()
        if tool == "git_diff":
            return self._git_diff()
        if tool == "run_command":
            return self._run_command(args, call)
        self._require_write()
        if tool == "apply_patch":
            return self._apply_patch(args)
        if tool == "write_file":
            return self._write_file(args)
        if tool == "delete_file":
            return self._delete_file(args)
        raise ToolError(f"unknown tool: {tool}")

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self.policy.existing_file(args["path"])
        data = path.read_bytes()
        if len(data) > MAX_READ_BYTES:
            raise ToolError(
                f"file is too large to read through the gateway ({len(data)} bytes); "
                "use search_text or a line range on a smaller file"
            )
        if b"\x00" in data[:8192]:
            raise ToolError(f"refusing to read binary file: {args['path']}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not valid UTF-8: {args['path']}") from exc
        lines = text.splitlines(keepends=True)
        start = args.get("start_line") or 1
        end = args.get("end_line") or len(lines)
        if end < start:
            raise ToolError("end_line must be >= start_line")
        if start > len(lines) and lines:
            raise ToolError(f"start_line {start} is past end of file ({len(lines)} lines)")
        end = min(end, len(lines))
        selected = "".join(lines[start - 1 : end])
        if len(selected.encode("utf-8")) > MAX_READ_BYTES:
            raise ToolError("requested line range exceeds the read size limit")
        return ToolResult(
            tool="read_file",
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Read {args['path']} lines {start}-{end} of {len(lines)}",
            output=selected,
            data={
                "path": args["path"],
                "start_line": start,
                "end_line": end,
                "total_lines": len(lines),
            },
        )

    def _list_files(self, args: dict[str, Any]) -> ToolResult:
        prefix = None if args.get("path") in {None, ".", ""} else args["path"]
        paths = self.policy.visible_paths(limit=5000)
        selected: list[str] = []
        for path in paths:
            if prefix is not None and not (path == prefix or path.startswith(prefix + "/")):
                continue
            relative_depth = path.count("/") - (prefix.count("/") if prefix else 0)
            if relative_depth > args["depth"]:
                continue
            selected.append(path)
            if len(selected) >= 400:
                break
        output = "\n".join(selected)
        return ToolResult(
            tool="list_files",
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Listed {len(selected)} file(s)",
            output=output,
            data={"path": prefix or ".", "count": len(selected), "truncated": len(selected) >= 400},
        )

    def _search_text(self, args: dict[str, Any]) -> ToolResult:
        query = args["query"].lower()
        prefix = args.get("path")
        if prefix is not None:
            self.policy.check_read(prefix)
        matches: list[str] = []
        scanned = 0
        for path in self.policy.visible_paths(limit=5000):
            if prefix is not None and not (path == prefix or path.startswith(prefix + "/")):
                continue
            target = self.workspace / path
            try:
                if target.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = target.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            scanned += len(data)
            if scanned > MAX_SEARCH_BYTES:
                break
            text = data.decode("utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if query in line.lower():
                    matches.append(f"{path}:{number}: {redact(line[:400])}")
                    if len(matches) >= args["limit"]:
                        break
            if len(matches) >= args["limit"]:
                break
        return ToolResult(
            tool="search_text",
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Found {len(matches)} match(es) for {args['query']!r}",
            output="\n".join(matches),
            data={"query": args["query"], "count": len(matches), "limit": args["limit"]},
        )

    def _git_status(self) -> ToolResult:
        git = GitClient(self.workspace)
        status = git.run("status", "--porcelain", "--untracked-files=all").stdout_text
        return ToolResult(
            tool="git_status",
            status=ToolCallStatus.SUCCEEDED,
            summary="Read workspace Git status",
            output=status,
            data={"dirty": bool(status.strip())},
        )

    def _git_diff(self) -> ToolResult:
        git = GitClient(self.workspace)
        unstaged = git.run(
            "diff", "--no-ext-diff", "--no-textconv", "--", "."
        ).stdout_text
        staged = git.run(
            "diff", "--cached", "--no-ext-diff", "--no-textconv", "--", "."
        ).stdout_text
        output = staged + ("\n" if staged and unstaged else "") + unstaged
        if len(output) > 200_000:
            output = output[:200_000] + "\n[truncated]"
        return ToolResult(
            tool="git_diff",
            status=ToolCallStatus.SUCCEEDED,
            summary="Read workspace Git diff",
            output=output,
            data={"staged": bool(staged), "unstaged": bool(unstaged)},
        )

    # ----------------------------------------------------------------- editing
    def _write_file(self, args: dict[str, Any]) -> ToolResult:
        rel = args["path"]
        target = self.policy.writable_file(rel)
        content = args["content"]
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        if b"\x00" in data[:8192]:
            raise ToolError("write_file only accepts UTF-8 text")
        if contains_secret_material(content):
            raise PolicyViolation(f"credential-shaped content is not writable: {rel}", path=rel)
        existed = target.exists()
        if existed and not args.get("overwrite"):
            raise ToolError(f"file exists; pass overwrite=true to replace it: {rel}")
        self._atomic_write([(target, data, target.stat().st_mode if existed else None)])
        return ToolResult(
            tool="write_file",
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Wrote {len(data)} bytes to {rel}" + (" (overwrote)" if existed else ""),
            data={"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        )

    def _delete_file(self, args: dict[str, Any]) -> ToolResult:
        rel = args["path"]
        self.policy.deletion_allowed(rel)
        target = self.policy.existing_file(rel, for_write=True)
        target.unlink()
        return ToolResult(
            tool="delete_file",
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Deleted {rel}",
            data={"path": rel},
        )

    def _apply_patch(self, args: dict[str, Any]) -> ToolResult:
        patch_text = args["patch"]
        files = parse_patch(patch_text)
        contents: dict[str, bytes] = {}
        for file_patch in files:
            self.policy.check_write(file_patch.path)
            if file_patch.deleted:
                self.policy.deletion_allowed(file_patch.path)
            if file_patch.created:
                if self.policy.writable_file(file_patch.path).exists():
                    raise ToolError(f"file already exists: {file_patch.path}")
                continue
            target = self.policy.existing_file(file_patch.path, for_write=True)
            contents[file_patch.path] = target.read_bytes()
        applied = apply_patch(patch_text, contents)
        changes: list[tuple[Path, bytes, int | None]] = []
        deletions: list[Path] = []
        for item in applied:
            text = item.data.decode("utf-8", errors="replace")
            if contains_secret_material(text):
                raise PolicyViolation(
                    f"patched content contains credential material: {item.path}",
                    path=item.path,
                )
            target = self.policy.writable_file(item.path)
            if item.deleted:
                deletions.append(target)
            else:
                mode = target.stat().st_mode if target.exists() else None
                changes.append((target, item.data, mode))
        self._atomic_write(changes, deletions=deletions)
        summary = ", ".join(
            f"{item.path} ({'created' if item.created else 'deleted' if item.deleted else 'modified'})"
            for item in applied
        )
        return ToolResult(
            tool="apply_patch",
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Applied patch: {summary}",
            data={
                "files": [
                    {
                        "path": item.path,
                        "created": item.created,
                        "deleted": item.deleted,
                        "bytes": len(item.data),
                    }
                    for item in applied
                ]
            },
        )

    def _atomic_write(
        self,
        writes: list[tuple[Path, bytes, int | None]],
        *,
        deletions: list[Path] | None = None,
    ) -> None:
        """Write a batch with rollback. Temp files live beside their targets."""
        backups: dict[Path, tuple[bytes, int] | None] = {}
        created_dirs: list[Path] = []
        temp_paths: list[Path] = []
        try:
            for target, data, _mode in writes:
                parent = target.parent
                missing: list[Path] = []
                probe = parent
                while not probe.exists() and probe != self.workspace:
                    missing.append(probe)
                    probe = probe.parent
                for directory in reversed(missing):
                    directory.mkdir(mode=0o700)
                    created_dirs.append(directory)
                if parent.resolve() != parent or not parent.is_dir():
                    raise ToolError(f"refusing to write through a non-directory: {target}")
                handle, temp_name = tempfile.mkstemp(
                    prefix=".maya-", suffix=".tmp", dir=str(parent)
                )
                temp_path = Path(temp_name)
                temp_paths.append(temp_path)
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                backups[target] = (
                    (target.read_bytes(), target.stat().st_mode)
                    if target.exists()
                    else None
                )
                os.replace(temp_path, target)
                temp_paths.remove(temp_path)
                if _mode is not None and target.exists():
                    os.chmod(target, stat.S_IMODE(_mode))
            for target in deletions or []:
                if not target.exists():
                    continue
                backups[target] = (target.read_bytes(), target.stat().st_mode)
                target.unlink()
        except Exception:
            # Restore every target touched by this batch, newest first.
            for target, backup in reversed(list(backups.items())):
                try:
                    if backup is None:
                        if target.exists():
                            target.unlink()
                    else:
                        data, mode = backup
                        target.write_bytes(data)
                        os.chmod(target, stat.S_IMODE(mode))
                except OSError:
                    pass
            for temp_path in temp_paths:
                temp_path.unlink(missing_ok=True)
            for directory in reversed(created_dirs):
                shutil.rmtree(directory, ignore_errors=True)
            raise

    # ----------------------------------------------------------------- command
    def _run_command(self, args: dict[str, Any], call: ToolCall) -> ToolResult:
        self._require_command()
        timeout = min(args["timeout_seconds"], self.contract.constraints.max_runtime_seconds)
        details = {"argv": [redact(arg) for arg in args["argv"]], "started_at": utcnow().isoformat()}

        def on_start(pid: int, resolved: list[str]) -> None:
            details["pid"] = pid
            details["resolved_argv"] = [redact(arg) for arg in resolved]
            self.calls.note(call.id, details=details)

        result: CommandResult = self.runner.run(
            args["argv"],
            timeout_seconds=timeout,
            cancel_requested=self._cancel_check,
            on_start=on_start,
        )
        output = self._format_command_output(result)
        details.update(
            {
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "network_isolated": result.network_isolated,
                "network_enforcement": result.network_enforcement,
                "env_names": result.env_names,
                "output_truncated": result.output_truncated,
            }
        )
        self.calls.note(call.id, details=details)
        status = (
            ToolCallStatus.TIMED_OUT
            if result.timed_out
            else ToolCallStatus.FAILED
            if result.exit_code not in (0, None)
            else ToolCallStatus.SUCCEEDED
        )
        if result.cancelled:
            status = ToolCallStatus.FAILED
        summary = (
            "Command timed out"
            if result.timed_out
            else "Command cancelled"
            if result.cancelled
            else f"Command exited with code {result.exit_code}"
        )
        return ToolResult(
            tool="run_command",
            status=status,
            summary=summary,
            output=output,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            data={
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "network_isolated": result.network_isolated,
                "network_enforcement": result.network_enforcement,
                "output_truncated": result.output_truncated,
            },
        )

    def _cancel_check(self) -> bool:
        if self._cancelled:
            return True
        if self.cancel_requested is None:
            return False
        return bool(self.cancel_requested())

    def _format_command_output(self, result: CommandResult) -> str:
        return (
            f"$ {' '.join(result.argv)}\n"
            f"exit: {result.exit_code}\n"
            f"duration_ms: {result.duration_ms}\n"
            f"timed_out: {result.timed_out}\n"
            f"network_enforcement: {result.network_enforcement}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
            + ("\n[output truncated]" if result.output_truncated else "")
        )

    # ------------------------------------------------------------------- audit
    def _store_output(self, call: ToolCall, text: str, *, kind: ArtifactKind) -> str:
        artifact = self.artifacts.put_text(
            redact(text),
            kind=kind,
            media_type="text/plain; charset=utf-8",
            created_by=f"tool:{call.tool_name}",
        )
        return artifact.id

    def _event(self, event_type: EventType, *, payload: dict[str, Any]) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=self.run.project_id,
                task_id=self.run.task_id,
                run_id=self.run.id,
                snapshot_id=self.run.snapshot_id,
                entity_type="tool_call",
                entity_id=payload.get("call_id", self.run.id),
                actor_kind=ActorKind.TOOL,
                actor_name="tool_gateway",
                payload=payload,
            )
        )


__all__ = [
    "ToolGateway",
    "ToolResult",
    "ToolName",
    "ReadFileArgs",
    "ListFilesArgs",
    "SearchTextArgs",
    "ApplyPatchArgs",
    "WriteFileArgs",
    "DeleteFileArgs",
    "RunCommandArgs",
    "GitArgs",
    "redact",
    "ToolBlocked",
]

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
from typing import TYPE_CHECKING, Any, Callable, Literal

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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mayaworkbench.tasks.inloop import DeclaredCheckRunner
    from mayaworkbench.workspace.sessions import SessionManager

ToolName = Literal[
    "read_file",
    "list_files",
    "search_text",
    "find_symbol",
    "apply_patch",
    "write_file",
    "delete_file",
    "git_status",
    "git_diff",
    "run_command",
    "run_check",
    "open_session",
    "stream_session",
    "send_input",
    "stop_session",
    "session_status",
    "session_replay",
    "api_request",
    "db_query",
    "env_detect",
    "logs_read",
    "notebook_inspect",
    "skill_list",
    "skill_run",
]

#: Tools that only read repository state. ``READ_ONLY_ANALYSIS`` constraints the
#: gateway to exactly this set regardless of any contract grant — the profile is
#: enforced in code, not by a prompt or a disabled UI button.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {"read_file", "list_files", "search_text", "find_symbol", "git_status", "git_diff"}
)

#: Capability required by each tool. A non-empty contract grant list is closed.
TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "read_file": ("file.read",),
    "list_files": ("file.read",),
    "search_text": ("file.read",),
    "find_symbol": ("file.read",),
    "git_status": ("file.read",),
    "git_diff": ("file.read",),
    "apply_patch": ("file.write",),
    "write_file": ("file.write",),
    "delete_file": ("file.write",),
    "run_command": ("process.execute",),
    "run_check": ("process.execute",),
    "open_session": ("process.session",),
    "stream_session": ("process.session",),
    "send_input": ("process.session",),
    "stop_session": ("process.session",),
    "session_status": ("process.session",),
    "session_replay": ("process.session",),
    "api_request": ("tool.advanced",),
    "db_query": ("tool.advanced",),
    "env_detect": ("tool.advanced",),
    "logs_read": ("tool.advanced",),
    "notebook_inspect": ("tool.advanced",),
    "skill_list": ("skill.use",),
    "skill_run": ("skill.use",),
}

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


def _dominant_eol(data: bytes) -> str:
    """Return the line ending a file actually uses.

    A file with both conventions falls back to LF: mixed endings are already
    ambiguous, and silently converting half of them would be worse than leaving
    the content as given.
    """
    crlf = data.count(b"\r\n")
    bare_lf = data.count(b"\n") - crlf
    bare_cr = data.count(b"\r") - crlf
    if crlf and not bare_lf and not bare_cr:
        return "\r\n"
    if bare_cr and not crlf and not bare_lf:
        return "\r"
    return "\n"


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


class FindSymbolArgs(Record):
    """Search the pinned snapshot's symbol index (read-only orientation)."""

    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=30)


class RunCheckArgs(Record):
    """Run one check that the immutable task contract already declares."""

    key: str = Field(min_length=1, max_length=90)


class RunCommandArgs(Record):
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)


class OpenSessionArgs(Record):
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)
    idle_timeout: int = Field(default=300, ge=1, le=86_400)
    max_lifetime: int = Field(default=3600, ge=2, le=172_800)
    use_pty: bool = False


class SessionArgs(Record):
    session_id: str = Field(min_length=1, max_length=120)


class StreamSessionArgs(SessionArgs):
    after_seq: int = Field(default=0, ge=0)


class SendInputArgs(SessionArgs):
    data: str = Field(min_length=1, max_length=4096)


class StopSessionArgs(SessionArgs):
    reason: str = Field(default="agent requested stop", max_length=200)


class ApiRequestArgs(Record):
    url: str = Field(min_length=8, max_length=1000)
    method: Literal["GET", "POST", "PUT", "DELETE"] = "GET"
    body_json: str | None = Field(default=None, max_length=100_000)

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("api_request requires an https URL")
        return value


class DbQueryArgs(Record):
    path: str = Field(min_length=1, max_length=1000)
    sql: str = Field(min_length=1, max_length=4000)
    max_rows: int = Field(default=50, ge=1, le=500)


class LogsReadArgs(Record):
    path: str = Field(min_length=1, max_length=1000)
    lines: int = Field(default=50, ge=1, le=500)


class EnvDetectArgs(Record):
    path: str = "."


class NotebookInspectArgs(Record):
    path: str = Field(min_length=1, max_length=1000)


class SkillListArgs(Record):
    category: str | None = Field(default=None, max_length=60)


class SkillRunArgs(Record):
    skill_id: str = Field(min_length=1, max_length=120)
    context_json: str | None = Field(default=None, max_length=20_000)


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
        check_runner: "DeclaredCheckRunner | None" = None,
        session_manager: "SessionManager | None" = None,
        allowed_api_hosts: tuple[str, ...] = (),
        allowed_db_dirs: tuple[str, ...] = (),
        allowed_log_dirs: tuple[str, ...] = (),
        http: Callable[[str, str, dict[str, str], bytes | None], dict[str, Any]] | None = None,
        symbol_lookup: Callable[[str, int], list[dict[str, Any]]] | None = None,
        read_only_profile: bool = False,
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
        self.check_runner = check_runner
        self.session_manager = session_manager
        self.allowed_api_hosts = frozenset(host.lower() for host in allowed_api_hosts)
        self.allowed_db_dirs = tuple(allowed_db_dirs)
        self.allowed_log_dirs = tuple(allowed_log_dirs)
        self.http = http
        self.symbol_lookup = symbol_lookup
        self.read_only_profile = read_only_profile
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
            "run_check",
            "find_symbol",
            "open_session",
            "stream_session",
            "send_input",
            "stop_session",
            "session_status",
            "session_replay",
            "api_request",
            "db_query",
            "env_detect",
            "logs_read",
            "notebook_inspect",
            "skill_list",
            "skill_run",
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
            "run_check": RunCheckArgs,
            "find_symbol": FindSymbolArgs,
            "open_session": OpenSessionArgs,
            "stream_session": StreamSessionArgs,
            "send_input": SendInputArgs,
            "stop_session": StopSessionArgs,
            "session_status": SessionArgs,
            "session_replay": SessionArgs,
            "api_request": ApiRequestArgs,
            "db_query": DbQueryArgs,
            "env_detect": EnvDetectArgs,
            "logs_read": LogsReadArgs,
            "notebook_inspect": NotebookInspectArgs,
            "skill_list": SkillListArgs,
            "skill_run": SkillRunArgs,
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
        if tool in {"search_text", "find_symbol"}:
            return {**args, "query": redact(str(args.get("query", "")))}
        return dict(args)

    def _require_capability(self, tool: str) -> None:
        """Every invocation must pass the contract's closed capability grant.

        A legacy contract with no grants keeps the approval/path policy as the
        authority. A non-empty grant list is a closed allowlist: no prompt, file
        content, or tool output can widen it. ``READ_ONLY_ANALYSIS`` is a hard
        code-level mask that even a broader contract grant cannot widen.
        """
        if self.read_only_profile and tool not in READ_ONLY_TOOLS:
            raise ToolBlocked(
                f"tool {tool!r} is forbidden by the READ_ONLY_ANALYSIS trust profile; "
                "no writes, subprocesses, sessions, network or advanced tools are permitted"
            )
        granted = set(getattr(self.contract.constraints, "capabilities", ()) or ())
        if not granted:
            return
        required = TOOL_CAPABILITIES.get(tool, ())
        missing = [capability for capability in required if capability not in granted]
        if missing:
            raise ToolBlocked(
                f"tool {tool!r} requires capability {', '.join(missing)} which this "
                "task contract does not grant"
            )

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
        self._require_capability(tool)
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
        if tool == "find_symbol":
            return self._find_symbol(args)
        if tool == "run_command":
            return self._run_command(args, call)
        if tool == "run_check":
            return self._run_check(args)
        if tool in {
            "open_session",
            "stream_session",
            "send_input",
            "stop_session",
            "session_status",
            "session_replay",
        }:
            return self._session_tool(tool, args, call)
        if tool in {
            "api_request",
            "db_query",
            "env_detect",
            "logs_read",
            "notebook_inspect",
            "skill_list",
            "skill_run",
        }:
            return self._advanced_tool(tool, args)
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
        encoding = "utf-8"
        replaced = 0
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            # UTF-32 also starts with the UTF-16 LE BOM; decoding it as UTF-16
            # would return silent garbage instead of an honest refusal.
            raise ToolError(
                f"UTF-32 files are not supported by the gateway: {args['path']}"
            )
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            # UTF-16 text is legible to a model and contains NUL bytes by
            # construction, so the BOM is checked before the binary heuristic.
            try:
                text = data.decode("utf-16")
                encoding = "utf-16"
            except UnicodeDecodeError as exc:
                raise ToolError(f"file has a UTF-16 byte-order mark but does not decode: {args['path']}") from exc
        else:
            if b"\x00" in data[:8192]:
                raise ToolError(f"refusing to read binary file: {args['path']}")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                # A latin-1 log or a stray byte should not make a whole file
                # invisible. The lossy decode is labelled so the model never
                # trusts the non-ASCII characters or edits through apply_patch
                # (which refuses non-UTF-8 files).
                text = data.decode("utf-8", errors="replace")
                encoding = "utf-8-replaced"
                replaced = text.count("\ufffd")
        lines = text.splitlines(keepends=True)
        start = args.get("start_line") or 1
        end = args.get("end_line") or len(lines)
        if end < start:
            raise ToolError("end_line must be >= start_line")
        if start > len(lines) and lines:
            raise ToolError(f"start_line {start} is past end of file ({len(lines)} lines)")
        end = min(end, len(lines)) if lines else 0
        selected = "".join(lines[start - 1 : end])
        if len(selected.encode("utf-8")) > MAX_READ_BYTES:
            raise ToolError("requested line range exceeds the read size limit")
        summary = f"Read {args['path']} lines {start}-{end} of {len(lines)}"
        if encoding == "utf-16":
            summary += " (UTF-16)"
        elif encoding == "utf-8-replaced":
            summary += (
                f" (NOT valid UTF-8: {replaced} character(s) replaced; non-ASCII text is lossy)"
            )
        return ToolResult(
            tool="read_file",
            status=ToolCallStatus.SUCCEEDED,
            summary=summary,
            output=selected,
            data={
                "path": args["path"],
                "start_line": start,
                "end_line": end,
                "total_lines": len(lines),
                "encoding": encoding,
                "replacement_characters": replaced,
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
        if contains_secret_material(content):
            raise PolicyViolation(f"credential-shaped content is not writable: {rel}", path=rel)
        existed = target.exists()
        if existed and not args.get("overwrite"):
            raise ToolError(f"file exists; pass overwrite=true to replace it: {rel}")
        notes: list[str] = []
        eol = "lf"
        bom_preserved = False
        if existed:
            existing = target.read_bytes()
            existing_eol = _dominant_eol(existing)
            if existing_eol != "\n" and "\n" in content and "\r" not in content:
                # Rewriting a CRLF file with LF content produces a whole-file
                # diff and can change behaviour in tools that read raw bytes.
                # Preserve the file's own convention instead of guessing.
                content = content.replace("\n", existing_eol)
                notes.append("preserved " + ("CRLF" if existing_eol == "\r\n" else "CR") + " line endings")
            if existing.startswith(b"\xef\xbb\xbf") and not content.startswith("\ufeff"):
                content = "\ufeff" + content
                bom_preserved = True
                notes.append("preserved the UTF-8 byte-order mark")
            eol = {"\r\n": "crlf", "\r": "cr"}.get(existing_eol, "lf")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        if b"\x00" in data[:8192]:
            raise ToolError("write_file only accepts UTF-8 text")
        self._atomic_write([(target, data, target.stat().st_mode if existed else None)])
        summary = f"Wrote {len(data)} bytes to {rel}" + (" (overwrote)" if existed else "")
        if notes:
            summary += "; " + "; ".join(notes)
        return ToolResult(
            tool="write_file",
            status=ToolCallStatus.SUCCEEDED,
            summary=summary,
            data={
                "path": rel,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "eol": eol,
                "bom_preserved": bom_preserved,
            },
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

    # ----------------------------------------------------------- orientation
    def _find_symbol(self, args: dict[str, Any]) -> ToolResult:
        """Search the pinned index. The workspace may be ahead of the index."""
        if self.symbol_lookup is None:
            raise ToolBlocked(
                "the symbol index is not available for this run; use search_text instead"
            )
        try:
            hits = self.symbol_lookup(args["query"], args["limit"])
        except Exception as exc:  # noqa: BLE001 - index failures are tool failures
            raise ToolError(f"symbol search failed: {type(exc).__name__}: {exc}") from exc
        # Excluded paths are invisible to every other tool, so a symbol result
        # must not leak their names either.
        hits = [
            hit
            for hit in hits
            if not self.policy.is_excluded(str(hit.get("path") or ""))
            and not is_secret_path(str(hit.get("path") or ""))
        ]
        lines = []
        for hit in hits:
            lines.append(
                f"{hit.get('qualified_name')} ({hit.get('kind')}) "
                f"{hit.get('path')}:{hit.get('start_line')}-{hit.get('end_line')} "
                f"[{hit.get('match_reason', '')}] {hit.get('signature', '')}".strip()
            )
        return ToolResult(
            tool="find_symbol",
            status=ToolCallStatus.SUCCEEDED,
            summary=(
                f"Found {len(hits)} symbol(s) for {args['query']!r} in the pinned index "
                "(the worktree may be newer)"
            ),
            output="\n".join(lines),
            data={
                "query": args["query"],
                "count": len(hits),
                "source": "pinned_index",
                "results": hits,
            },
        )

    # ------------------------------------------------------------------- checks
    def _run_check(self, args: dict[str, Any]) -> ToolResult:
        if self.check_runner is None:
            raise ToolBlocked("this task contract declares no runnable checks")
        self._require_command()
        spec = self.check_runner.spec_for(args["key"])
        if spec is None:
            declared = ", ".join(item.key for item in self.check_runner.specs) or "none"
            raise ToolError(
                f"no declared check has key {args['key']!r}; declared checks: {declared}"
            )
        feedback = self.check_runner.run([spec.key])
        if not feedback.results:
            raise ToolError(f"check {spec.key!r} produced no result")
        item = feedback.results[0]
        from mayaworkbench.domain.enums import CheckOutcome

        if item.outcome == CheckOutcome.PASSED.value:
            status = ToolCallStatus.SUCCEEDED
        elif item.outcome == CheckOutcome.SKIPPED.value:
            status = ToolCallStatus.SUCCEEDED
        elif item.outcome == CheckOutcome.FAILED.value:
            status = ToolCallStatus.FAILED
        else:
            status = ToolCallStatus.BLOCKED
        output = json.dumps(item.to_payload(), ensure_ascii=False, indent=2, sort_keys=True)
        return ToolResult(
            tool="run_check",
            status=status,
            summary=f"Check {spec.key}: {item.outcome} ({item.reason})",
            output=output,
            exit_code=item.exit_code,
            duration_ms=item.duration_ms,
            data={
                "key": spec.key,
                "outcome": item.outcome,
                "infrastructure_failure": item.infrastructure,
                "in_loop": True,
            },
        )

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
                "blocked": result.blocked,
                "blocked_reason": result.blocked_reason,
                "network_isolated": result.network_isolated,
                "network_enforcement": result.network_enforcement,
                "limits_enforcement": result.limits_enforcement,
                "limits": {
                    name: item.as_dict() for name, item in sorted(result.limits.items())
                },
                "cleanup_required": result.cleanup_required,
                "cleanup_succeeded": result.cleanup_succeeded,
                "env_names": result.env_names,
                "output_truncated": result.output_truncated,
            }
        )
        self.calls.note(call.id, details=details)
        if result.blocked:
            status = ToolCallStatus.BLOCKED
        elif result.timed_out:
            status = ToolCallStatus.TIMED_OUT
        elif result.cancelled or result.exit_code not in (0, None):
            status = ToolCallStatus.FAILED
        else:
            status = ToolCallStatus.SUCCEEDED
        summary = (
            f"Command blocked: {result.blocked_reason}"
            if result.blocked
            else "Command timed out"
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
                "blocked": result.blocked,
                "blocked_reason": result.blocked_reason,
                "network_isolated": result.network_isolated,
                "network_enforcement": result.network_enforcement,
                "limits_enforcement": result.limits_enforcement,
                "limits": {
                    name: item.as_dict() for name, item in sorted(result.limits.items())
                },
                "cleanup_required": result.cleanup_required,
                "cleanup_succeeded": result.cleanup_succeeded,
                "output_truncated": result.output_truncated,
            },
        )

    def _session_tool(self, tool: str, args: dict[str, Any], call: ToolCall) -> ToolResult:
        """Typed session operations, gated like any other command execution."""
        self._require_command()
        manager = self.session_manager
        if manager is None:
            raise ToolBlocked("this runtime does not provide process sessions")
        max_runtime = self.contract.constraints.max_runtime_seconds
        if tool == "open_session":
            timeout = min(args["timeout_seconds"], max_runtime)
            ref = manager.open(
                args["argv"],
                timeout_seconds=timeout,
                idle_timeout=min(args["idle_timeout"], max_runtime),
                max_lifetime=min(args["max_lifetime"], max_runtime),
                cancel_requested=self._cancel_check,
                use_pty=bool(args.get("use_pty", False)),
            )
            data = {
                "session_id": ref.id,
                "argv": list(ref.argv),
                "cwd": ref.cwd,
                "env_sha256": ref.env_sha256,
            }
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Session {ref.id} opened",
                output=json.dumps(data, ensure_ascii=False, sort_keys=True),
                data=data,
            )
        session_id = args["session_id"]
        if tool == "stream_session":
            events = manager.stream(session_id, after_seq=args["after_seq"])
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"{len(events)} event(s) after seq {args['after_seq']}",
                output="\n".join(
                    f"[{item['seq']}] {item['kind']}: {item['data']}" for item in events
                ),
                data={"session_id": session_id, "events": events, "call_id": call.id},
            )
        if tool == "send_input":
            manager.send_input(session_id, args["data"])
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Sent {len(args['data'])} character(s) to {session_id}",
                data={"session_id": session_id},
            )
        if tool == "stop_session":
            result = manager.stop(session_id, reason=args["reason"])
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Session {session_id} stopped",
                output=json.dumps(result, ensure_ascii=False, sort_keys=True),
                data={"session_id": session_id, **result},
            )
        if tool == "session_replay":
            record = manager.export_session(session_id)
            events = manager.replay_session(record)
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Durable record for {session_id}",
                output=record,
                data={"session_id": session_id, "events": events, "note": "record replay; not a live process reattach"},
            )
        status = manager.status(session_id)
        return ToolResult(
            tool=tool,
            status=ToolCallStatus.SUCCEEDED,
            summary=f"Session {session_id}: {status['state']}",
            output=json.dumps(status, ensure_ascii=False, sort_keys=True),
            data=status,
        )

    def _advanced_tool(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Typed, bounded advanced operations. All are read-only except no side effects at all."""
        if tool == "api_request":
            import hashlib
            import ipaddress
            import socket
            from urllib.parse import urlsplit

            parsed = urlsplit(args["url"])
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or parsed.username or parsed.password:
                raise ToolBlocked("api_request requires an https URL without credentials")
            if parsed.port not in (None, 443):
                raise ToolBlocked("api_request allows only the default HTTPS port")
            if host not in self.allowed_api_hosts:
                raise ToolBlocked(
                    f"host {host!r} is not in the connector allowlist; no request was sent"
                )
            if self.http is None:
                raise ToolBlocked(
                    "api_request has no configured HTTP executor; no request was sent"
                )
            body = args.get("body_json")
            if body is not None:
                json.loads(body)  # reject malformed JSON before any network call
            # SSRF: resolve every answer and reject private/reserved addresses.
            try:
                infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            except socket.gaierror as exc:
                raise ToolBlocked(f"could not resolve {host}: {exc}") from exc
            for info in infos:
                raw = info[4][0]
                try:
                    address = ipaddress.ip_address(raw.split("%")[0])
                except ValueError as exc:
                    raise ToolBlocked(f"unparsable address for {host}: {raw}") from exc
                if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified:
                    raise ToolBlocked(
                        f"refusing api_request to non-public address {raw} for {host}"
                    )
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            request_payload = {
                "method": args["method"],
                "url": args["url"],
                "headers": headers,
                "body": body,
            }
            request_hash = hashlib.sha256(
                json.dumps(request_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            reply = self.http(
                args["method"], args["url"], headers, body.encode("utf-8") if body else None
            )
            response_bytes = bytes(reply.get("body", b""))
            cap = max(1024, self.runner.policy.max_output_bytes)
            truncated = len(response_bytes) > cap
            if truncated:
                response_bytes = response_bytes[:cap]
            text = response_bytes.decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"raw": text[:1000], "note": "response was not valid JSON"}
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"{args['method']} {host} -> {reply.get('status', 'unknown')}",
                output=text,
                data={
                    "status_code": reply.get("status"),
                    "request_sha256": request_hash,
                    "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
                    "truncated": truncated,
                    "data": data,
                },
            )
        if tool == "db_query":
            import sqlite3

            resolved = Path(args["path"]).resolve()
            if not any(
                resolved.is_relative_to(Path(directory).resolve())
                for directory in self.allowed_db_dirs
            ):
                raise ToolBlocked("database path is not in the allowed directories")
            sql = args["sql"].strip().rstrip(";")
            if not sql.lower().startswith("select") or any(
                token in sql.lower()
                for token in ("attach", "insert", "update", "delete", "drop", "pragma")
            ):
                raise ToolBlocked("only a bounded SELECT is permitted")
            connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
            try:
                cursor = connection.execute(sql)
                rows = cursor.fetchmany(args["max_rows"])
            finally:
                connection.close()
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Read {len(rows)} row(s) from {resolved.name}",
                output=json.dumps(rows, ensure_ascii=False, sort_keys=True),
                data={"rows": rows, "path": str(resolved)},
            )
        if tool == "logs_read":
            resolved = Path(args["path"]).resolve()
            if not any(
                resolved.is_relative_to(Path(directory).resolve())
                for directory in self.allowed_log_dirs
            ):
                raise ToolBlocked("log path is not in the allowed directories")
            text = redact(resolved.read_text(encoding="utf-8", errors="replace"))
            lines = text.splitlines()[-args["lines"]:]
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Read {len(lines)} redacted log line(s)",
                output="\n".join(lines),
                data={"path": str(resolved), "lines": lines},
            )
        if tool == "env_detect":
            root = (self.workspace / args["path"]).resolve()
            markers = {
                "python": "pyproject.toml",
                "node": "package.json",
                "rust": "Cargo.toml",
                "go": "go.mod",
            }
            detected = [
                {"ecosystem": name, "descriptor": marker, "present": (root / marker).is_file()}
                for name, marker in markers.items()
                if (root / marker).is_file()
            ]
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Detected {len(detected)} ecosystem(s)",
                output=json.dumps(detected, ensure_ascii=False, sort_keys=True),
                data={"detected": detected},
            )
        if tool == "notebook_inspect":
            resolved = (self.workspace / args["path"]).resolve()
            if not resolved.is_relative_to(self.workspace):
                raise ToolBlocked("notebook path is outside the workspace")
            document = json.loads(resolved.read_text(encoding="utf-8"))
            cells = document.get("cells", []) if isinstance(document, dict) else []
            summary = {
                "cells": len(cells),
                "types": [cell.get("cell_type") for cell in cells],
                "kernelspec": (document.get("metadata") or {}).get("kernelspec"),
                "outputs": sum(len(cell.get("outputs", [])) for cell in cells),
            }
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"Notebook has {summary['cells']} cell(s)",
                output=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                data=summary,
            )
        from mayaworkbench.skills.executor import execute_skill
        from mayaworkbench.skills.service import SkillsService

        service = SkillsService(self.calls.db)
        if tool == "skill_list":
            skills = service.list(self.run.project_id, category=args.get("category"))
            return ToolResult(
                tool=tool,
                status=ToolCallStatus.SUCCEEDED,
                summary=f"{len(skills)} skill(s) available",
                output="\n".join(f"{item.effective_key}: {item.title}" for item in skills),
                data={"skills": [item.model_dump(mode="json") for item in skills]},
            )
        skill = service.get(args["skill_id"])
        context = json.loads(args["context_json"]) if args.get("context_json") else {}
        result = execute_skill(skill, **context)
        status = ToolCallStatus.SUCCEEDED if result.status != "FAILED" else ToolCallStatus.FAILED
        return ToolResult(
            tool=tool,
            status=status,
            summary=f"Skill {skill.effective_key}: {result.status}",
            output=result.output,
            data=result.model_dump(mode="json"),
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
            f"blocked: {result.blocked}\n"
            f"network_enforcement: {result.network_enforcement}\n"
            f"limits_enforcement: {result.limits_enforcement}\n"
            + "\n".join(
                f"limit.{name}: {item.status}"
                + (f" ({item.mechanism})" if item.mechanism else "")
                + (f" — {item.detail}" if item.detail else "")
                for name, item in sorted(result.limits.items())
            )
            + "\n"
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
    "FindSymbolArgs",
    "RunCheckArgs",
    "ApplyPatchArgs",
    "WriteFileArgs",
    "DeleteFileArgs",
    "RunCommandArgs",
    "GitArgs",
    "redact",
    "ToolBlocked",
]

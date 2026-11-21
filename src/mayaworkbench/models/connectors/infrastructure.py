"""Static Terraform-style configuration analysis (WP12).

This connector parses a small, deterministic HCL-like subset and exposes only
``validate``, ``plan`` and ``diff``. It has deliberately **no** apply/destroy
operation: there is no process execution, no cloud API, no state mutation and
no provider download. ``plan``/``diff`` describe what a change *would* do; they
never perform it.

The file opener and path checks are injectable; tests never read a real
infrastructure directory or contact a provider.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult
from mayaworkbench.models.connectors.contracts import (
    CONNECTOR_READ_SCOPE,
    ConnectorContract,
    PathGuard,
    make_result,
    reject_inline_secrets,
)

CONTRACT = ConnectorContract(
    id="infrastructure",
    version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "infra:read"],
    risk="LOW",
    side_effects=[],
    evidence_outputs=["findings", "changes", "added", "removed", "request_hash", "response_hash"],
    never_applies=True,
    operation_scopes={
        "validate": CONNECTOR_READ_SCOPE,
        "plan": CONNECTOR_READ_SCOPE,
        "diff": CONNECTOR_READ_SCOPE,
    },
)

_BLOCK_RE = re.compile(r'^\s*(?P<kind>[A-Za-z_][\w-]*)\s*(?P<labels>(?:"[^"]*"\s*)*)\{\s*$')
_ATTR_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][\w-]*)\s*=\s*(?P<value>.+?)\s*$")
_LABEL_RE = re.compile(r'"([^"]*)"')
_EXECUTION_HOOKS = frozenset({"provisioner", "connection", "local-exec", "remote-exec"})


def _strip_comment(line: str) -> str:
    """Remove trailing # / // comments outside quoted strings."""
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "#" or (char == "/" and line[index : index + 2] == "//"):
            return line[:index]
        index += 1
    return line


def _address(block: dict[str, Any]) -> str:
    labels = ".".join(str(label) for label in block["labels"])
    return f"{block['kind']}.{labels}" if labels else str(block["kind"])


def parse_config(text: str) -> dict[str, Any]:
    """Parse a deterministic HCL-like subset into blocks and findings."""
    blocks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    depth = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        block_match = _BLOCK_RE.match(line)
        if block_match:
            labels = _LABEL_RE.findall(block_match.group("labels"))
            block = {
                "kind": block_match.group("kind"),
                "labels": labels,
                "address": f"{block_match.group('kind')}.{'.'.join(labels)}" if labels else block_match.group("kind"),
                "line": number,
                "attributes": {},
            }
            if block["kind"] in _EXECUTION_HOOKS:
                findings.append(
                    {
                        "severity": "HIGH",
                        "line": number,
                        "message": f"Execution hook {block['kind']!r} present; it is never executed by this connector",
                    }
                )
            if depth == 0:
                blocks.append(block)
            stack.append(block)
            depth += line.count("{") - line.count("}")
            continue
        attr_match = _ATTR_RE.match(line)
        if attr_match:
            if stack:
                stack[-1]["attributes"][attr_match.group("key")] = attr_match.group("value")
            else:
                findings.append(
                    {
                        "severity": "WARNING",
                        "line": number,
                        "message": "Attribute appears outside any block",
                    }
                )
        depth += line.count("{") - line.count("}")
        if depth < 0:
            findings.append({"severity": "ERROR", "line": number, "message": "Unbalanced closing brace"})
            depth = 0
    if depth > 0:
        findings.append(
            {"severity": "ERROR", "line": max(1, len(text.splitlines())), "message": "Unbalanced opening brace"}
        )
    seen: set[str] = set()
    for block in blocks:
        if block["address"] in seen:
            findings.append(
                {
                    "severity": "WARNING",
                    "line": block["line"],
                    "message": f"Duplicate block address {block['address']!r}",
                }
            )
        seen.add(block["address"])
    return {"blocks": blocks, "findings": findings}


class InfrastructureConnector:
    """Validate / plan / diff Terraform-style files, with no apply path."""

    name = "infrastructure"
    mode = "READ_ONLY"
    contract = CONTRACT

    def __init__(
        self,
        *,
        allowed_dirs: Any = None,
        open_file: Callable[..., Any] | None = None,
        resolver: Callable[[str], str] | None = None,
        exists: Callable[[str], bool] | None = None,
        is_file: Callable[[str], bool] | None = None,
        token_env: str = "MAYA_INFRASTRUCTURE_TOKEN",
        max_bytes: int = 262_144,
    ):
        self._guard = PathGuard(allowed_dirs, resolver=resolver, exists=exists, is_file=is_file)
        self._open = open_file or open
        self.token_env = token_env
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ConnectorError("max_bytes must be positive")
        self._state: dict[str, Any] = {}
        self._revoked = False

    def operations(self) -> tuple[str, ...]:
        return ("validate", "plan", "diff")

    def supports(self, operation: str) -> bool:
        return operation in self.operations()

    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        first = self._guard.check(params.get("path"), label="configuration path")
        target = "tf:" + self._guard.display(first)
        if operation == "diff":
            second = self._guard.check(params.get("compare"), label="comparison path")
            target += " vs " + self._guard.display(second)
        return target

    def revoke(self) -> None:
        self._state.clear()
        self._revoked = True

    def is_revoked(self) -> bool:
        return self._revoked

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "operations": list(self.operations()),
            "allowed_dirs": list(self._guard.allowed_dirs),
            "token_env": self.token_env,
            "writes_supported": False,
            "never_applies": True,
            "never_merges": True,
            "contract": self.contract.model_dump(mode="json"),
            "revoked": self._revoked,
            "note": (
                "Static validate/plan/diff of Terraform-style files only. No apply, "
                "destroy, provider call, process execution or state mutation exists."
            ),
        }

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError("Infrastructure connector has been revoked; re-create it before use")
        if not self.supports(operation):
            raise ConnectorError(
                f"Infrastructure connector does not support {operation!r}",
                hint=(
                    "This connector has no apply/destroy path; only validate, plan "
                    "and diff are available."
                ),
            )
        if operation == "validate":
            result = self._validate(params)
        elif operation == "plan":
            result = self._plan(params)
        else:
            result = self._diff(params)
        self._state[operation] = result
        return result

    def _read(self, path: Any) -> tuple[str, str, bytes]:
        resolved = self._guard.check(path, label="configuration path")
        display = self._guard.display(resolved)
        try:
            with self._open(resolved, "rb") as handle:
                data = handle.read(self.max_bytes + 1)
        except OSError as exc:
            raise ConnectorError(f"Cannot read the allowlisted configuration file: {exc}") from exc
        if len(data) > self.max_bytes:
            raise ConnectorError("Configuration file exceeds the connector byte limit")
        return resolved, display, data

    def _parse_one(self, path: Any) -> tuple[str, dict[str, Any], bytes]:
        _resolved, display, raw = self._read(path)
        return display, parse_config(raw.decode("utf-8", errors="replace")), raw

    def _validate(self, params: dict[str, Any]) -> ConnectorResult:
        display, parsed, raw = self._parse_one(params.get("path"))
        findings = parsed["findings"]
        valid = not any(item["severity"] == "ERROR" for item in findings)
        data = {
            "path": display,
            "blocks": parsed["blocks"],
            "findings": findings,
            "valid": valid,
            "applied": False,
        }
        return make_result(
            connector=self.name,
            operation="validate",
            target="tf:" + display,
            data=data,
            request_payload={"path": display, "operation": "validate"},
            raw_response=raw,
        )

    def _plan(self, params: dict[str, Any]) -> ConnectorResult:
        display, parsed, raw = self._parse_one(params.get("path"))
        changes = [
            {"action": "create", "address": _address(block), "line": block["line"]}
            for block in parsed["blocks"]
            if block["kind"] in {"resource", "data", "module"}
        ]
        changes.sort(key=lambda item: item["address"])
        data = {
            "path": display,
            "changes": changes,
            "findings": parsed["findings"],
            "applies": False,
            "never_applies": True,
            "note": "Static proposal only; no provider, state or side effect was touched.",
        }
        return make_result(
            connector=self.name,
            operation="plan",
            target="tf:" + display,
            data=data,
            request_payload={"path": display, "operation": "plan"},
            raw_response=raw,
        )

    def _diff(self, params: dict[str, Any]) -> ConnectorResult:
        old_display, old_parsed, old_raw = self._parse_one(params.get("path"))
        new_display, new_parsed, new_raw = self._parse_one(params.get("compare"))
        old_map = {_address(block): block["attributes"] for block in old_parsed["blocks"]}
        new_map = {_address(block): block["attributes"] for block in new_parsed["blocks"]}
        added = sorted(set(new_map) - set(old_map))
        removed = sorted(set(old_map) - set(new_map))
        changed: list[dict[str, Any]] = []
        for address in sorted(set(old_map) & set(new_map)):
            keys = set(old_map[address]) | set(new_map[address])
            differences = [
                {"key": key, "from": old_map[address].get(key), "to": new_map[address].get(key)}
                for key in sorted(keys)
                if old_map[address].get(key) != new_map[address].get(key)
            ]
            if differences:
                changed.append({"address": address, "differences": differences})
        data = {
            "path": old_display,
            "compare": new_display,
            "added": added,
            "removed": removed,
            "changed": changed,
            "applies": False,
            "never_applies": True,
        }
        return make_result(
            connector=self.name,
            operation="diff",
            target=f"tf:{old_display} vs {new_display}",
            data=data,
            request_payload={
                "path": old_display,
                "compare": new_display,
                "operation": "diff",
            },
            raw_response=old_raw + new_raw,
        )


__all__ = ["CONTRACT", "parse_config", "InfrastructureConnector"]

"""User-owned runner adapters (MW-P2-02).

The workbench does not promise free Kaggle/Colab quotas. It exports portable
bundles and can dispatch them to a *user-owned* runner through an explicit,
allowlisted argv command. Results are imported as `IMPORTED` unless a trusted
runner signature is present; no remote code is trusted as authority.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mayaworkbench.errors import PolicyError

Executor = Callable[[list[str]], dict[str, Any]]


@dataclass(frozen=True)
class RunnerCapabilities:
    name: str
    allowed_hosts: tuple[str, ...]
    submit_argv: tuple[str, ...]
    poll_argv: tuple[str, ...]
    fetch_argv: tuple[str, ...]


def render_argv(template: tuple[str, ...], *, bundle: str, host: str, run_id: str) -> list[str]:
    """Substitute only allowlisted placeholders; never invoke a shell."""
    replacements = {"{bundle}": bundle, "{host}": host, "{run_id}": run_id}
    out: list[str] = []
    for part in template:
        value = part
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        if "\x00" in value or "\n" in value or "\r" in value:
            raise PolicyError("runner argv entry contains a control character")
        out.append(value)
    return out


class UserOwnedRunner:
    """Dispatch and retrieve experiment bundles from a user-owned host."""

    def __init__(
        self,
        *,
        capabilities: RunnerCapabilities,
        executor: Executor | None = None,
        approval: str | None = None,
    ):
        self.capabilities = capabilities
        self.executor = executor
        self.approval = approval

    def _require_approval(self) -> None:
        if not self.approval:
            raise PolicyError("remote runner dispatch requires an explicit approval")

    def _check_host(self, host: str) -> None:
        if host not in self.capabilities.allowed_hosts:
            raise PolicyError(
                f"runner host {host!r} is not allowlisted",
                hint=f"Allowed: {', '.join(self.capabilities.allowed_hosts) or '(none)'}",
            )

    def submit(self, *, bundle: str | Path, host: str, run_id: str) -> dict[str, Any]:
        self._require_approval()
        self._check_host(host)
        bundle_path = str(Path(bundle).resolve())
        if not Path(bundle_path).is_file():
            raise PolicyError("bundle file does not exist")
        argv = render_argv(
            self.capabilities.submit_argv, bundle=bundle_path, host=host, run_id=run_id
        )
        return self._execute(argv)

    def poll(self, *, host: str, run_id: str) -> dict[str, Any]:
        self._require_approval()
        self._check_host(host)
        return self._execute(
            render_argv(self.capabilities.poll_argv, bundle="", host=host, run_id=run_id)
        )

    def fetch(self, *, host: str, run_id: str) -> dict[str, Any]:
        self._require_approval()
        self._check_host(host)
        result = self._execute(
            render_argv(self.capabilities.fetch_argv, bundle="", host=host, run_id=run_id)
        )
        payload = json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {
            **result,
            "execution": "IMPORTED",
            "attestation": "UNATTESTED",
            "import_sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _execute(self, argv: list[str]) -> dict[str, Any]:
        if self.executor is None:
            raise PolicyError("no runner executor is configured; dispatch is disabled")
        return self.executor(argv)


def builtin_capabilities(name: str, *, allowed_hosts: tuple[str, ...]) -> RunnerCapabilities:
    if name != "user-owned":
        raise PolicyError(f"unknown runner adapter {name!r}")
    return RunnerCapabilities(
        name=name,
        allowed_hosts=allowed_hosts,
        submit_argv=("maya-remote-runner", "submit", "--bundle", "{bundle}", "--host", "{host}"),
        poll_argv=("maya-remote-runner", "poll", "--host", "{host}", "--run", "{run_id}"),
        fetch_argv=("maya-remote-runner", "fetch", "--host", "{host}", "--run", "{run_id}"),
    )


__all__ = ["RunnerCapabilities", "UserOwnedRunner", "builtin_capabilities", "render_argv"]

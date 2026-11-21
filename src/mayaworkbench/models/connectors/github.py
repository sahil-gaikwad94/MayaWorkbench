"""GitHub READ-ONLY connector (WP7).

Exactly three read operations are exposed: ``list_issues``, ``get_issue`` and
``list_pr_checks``. There is deliberately no write path — no comments, issues,
PRs, deployments or edits.

Network discipline follows ``research/fetch.py`` (the existing SSRF boundary):
https only, exact host allowlist, no embedded credentials, no private/loopback
addresses on any redirect hop, bounded redirects and response bytes. The HTTP
function is injectable, so tests never touch the network; URL validation still
runs on the injected path, including on any reported final URL.

The token is read from the environment, used only as an outgoing header, and is
never returned in a result, description, hash or event.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from mayaworkbench.clock import to_iso, utcnow
from mayaworkbench.errors import ApprovalError, PolicyError
from mayaworkbench.models.capabilities import ConnectorError, ConnectorResult
from mayaworkbench.models.connectors.contracts import (
    APPROVAL_PARAM,
    CONNECTOR_READ_SCOPE,
    ConnectorContract,
    action_spec,
    public_params,
    reject_inline_secrets,
    spec_sha256,
)
from mayaworkbench.research.fetch import _is_public, _pinned_connection

#: Default host allowlist. Exact match only — suffixes and subdomains are not
#: accepted, so ``api.github.com.attacker.test`` cannot satisfy the guard.
ALLOWED_HOSTS: frozenset[str] = frozenset({"api.github.com"})

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

READ_CONTRACT = ConnectorContract(
    id="github",
    version="1.0",
    permissions=[CONNECTOR_READ_SCOPE, "issues:read", "checks:read"],
    policy_concept=CONNECTOR_READ_SCOPE,
    risk="LOW",
    side_effects=[],
    evidence_outputs=["request_hash", "response_hash", "retrieved_at", "target"],
    operation_scopes={
        "list_issues": CONNECTOR_READ_SCOPE,
        "get_issue": CONNECTOR_READ_SCOPE,
        "list_pr_checks": CONNECTOR_READ_SCOPE,
    },
)

ADVANCED_CONTRACT = ConnectorContract(
    id="github",
    version="1.1",
    permissions=[CONNECTOR_READ_SCOPE, "issues:read", "checks:read", "pulls:propose"],
    policy_concept=CONNECTOR_READ_SCOPE,
    approval_action="WRITE_ACCESS",
    risk="MEDIUM",
    side_effects=["create_draft_pr"],
    evidence_outputs=[
        "request_hash",
        "response_hash",
        "retrieved_at",
        "target",
        "approval_id",
    ],
    never_merges=True,
    operation_scopes={
        "list_issues": CONNECTOR_READ_SCOPE,
        "get_issue": CONNECTOR_READ_SCOPE,
        "list_pr_checks": CONNECTOR_READ_SCOPE,
    },
)


@dataclass
class HttpReply:
    """Minimal HTTP response shape used by both the real and injected paths."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    final_url: str | None = None


HttpFunction = Callable[[str, str, dict[str, str]], HttpReply]


def _sha256_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _segment(value: Any, label: str) -> str:
    text = value if isinstance(value, str) else str(value or "")
    if not _SEGMENT_RE.match(text) or ".." in text:
        raise ConnectorError(f"Invalid {label}: {text[:80]!r}")
    return text


def _ref(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    if not _REF_RE.match(text) or ".." in text:
        raise ConnectorError(f"Invalid commit ref: {text[:80]!r}")
    return text


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise ConnectorError(
            f"Missing required parameter {key!r} for this operation",
            hint="Expected owner, repo and issue/pull identifiers as appropriate.",
        )
    return params[key]


def _positive_int(value: Any, label: str, *, maximum: int = 100) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"{label} must be a positive integer") from exc
    if number <= 0:
        raise ConnectorError(f"{label} must be a positive integer")
    return min(number, maximum)


class GitHubReadOnlyConnector:
    """Read-only GitHub adapter; no writes, no ambient credentials."""

    name = "github"
    mode = "READ_ONLY"
    contract = READ_CONTRACT
    _OPERATIONS: tuple[str, ...] = ("list_issues", "get_issue", "list_pr_checks")

    def __init__(
        self,
        *,
        token: str | None = None,
        token_env: str = "MAYA_GITHUB_TOKEN",
        http: HttpFunction | None = None,
        allowed_hosts: Any = ALLOWED_HOSTS,
        api_base: str = "https://api.github.com",
        timeout_seconds: int = 15,
        max_bytes: int = 1_000_000,
        max_redirects: int = 2,
    ):
        self._http = http
        self._token_env = token_env
        self._token = token if token is not None else os.environ.get(token_env)
        self._allowed_hosts = frozenset(str(host).lower() for host in allowed_hosts)
        self._api_base = api_base.rstrip("/")
        self.timeout_seconds = int(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self._state: dict[str, Any] = {}
        self._revoked = False
        base_host = urllib.parse.urlsplit(self._api_base).hostname or ""
        if base_host.lower() not in self._allowed_hosts:
            raise PolicyError(
                f"GitHub API base host {base_host!r} is not in the connector allowlist"
            )

    # --------------------------------------------------------------- interface
    def operations(self) -> tuple[str, ...]:
        return self._OPERATIONS

    def supports(self, operation: str) -> bool:
        return operation in self._OPERATIONS

    def describe(self) -> dict[str, Any]:
        """Non-secret configuration view safe for CLI output and events."""
        return {
            "name": self.name,
            "mode": self.mode,
            "operations": list(self._OPERATIONS),
            "allowed_hosts": sorted(self._allowed_hosts),
            "api_base": self._api_base,
            "token_env": self._token_env,
            "token_present": bool(self._token),
            "writes_supported": False,
            "revoked": self._revoked,
            "note": (
                "Read-only issues and PR checks. Token is sent only as an outgoing "
                "header and never included in results, hashes or events."
            ),
        }

    def revoke(self) -> None:
        """Clear all in-memory state and credentials; refuse further calls."""
        self._token = None
        self._state.clear()
        self._revoked = True

    def is_revoked(self) -> bool:
        return self._revoked

    # ---------------------------------------------------------------- invokes
    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        """Non-secret host+path target, used by the registry for approval specs."""
        parts = urllib.parse.urlsplit(self._api_base)
        owner = _segment(_require(params, "owner"), "owner")
        repo = _segment(_require(params, "repo"), "repo")
        base = f"{parts.hostname}/repos/{owner}/{repo}"
        if operation == "get_issue":
            return f"{base}/issues/{_positive_int(_require(params, 'number'), 'number', maximum=10_000_000)}"
        if operation == "list_pr_checks":
            if params.get("ref"):
                return f"{base}/commits/{_ref(params['ref'])}/check-runs"
            return f"{base}/pulls/{_positive_int(_require(params, 'pull_number'), 'pull_number', maximum=10_000_000)}"
        return f"{base}/issues"

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError(
                "GitHub connector has been revoked; re-create it before use",
                hint="Run `maya connector revoke github` only when you intend to disconnect.",
            )
        if not self.supports(operation):
            raise ConnectorError(
                f"GitHub connector is read-only and does not support {operation!r}",
                hint=f"Allowed operations: {', '.join(self._OPERATIONS)}",
            )
        if not self._token:
            raise PolicyError(
                f"No GitHub token found in ${self._token_env}",
                hint="Export a read-only scoped token before using this connector.",
            )
        if operation == "list_issues":
            result = self._list_issues(params)
        elif operation == "get_issue":
            result = self._get_issue(params)
        else:
            result = self._list_pr_checks(params)
        self._state[operation] = result
        return result

    def _list_issues(self, params: dict[str, Any]) -> ConnectorResult:
        owner = _segment(_require(params, "owner"), "owner")
        repo = _segment(_require(params, "repo"), "repo")
        state = str(params.get("state", "open"))
        if state not in {"open", "closed", "all"}:
            raise ConnectorError("state must be open, closed or all")
        query = {"state": state, "per_page": str(_positive_int(params.get("per_page", 30), "per_page"))}
        return self._request("GET", "list_issues", f"/repos/{owner}/{repo}/issues", query)

    def _get_issue(self, params: dict[str, Any]) -> ConnectorResult:
        owner = _segment(_require(params, "owner"), "owner")
        repo = _segment(_require(params, "repo"), "repo")
        number = _positive_int(_require(params, "number"), "number", maximum=10_000_000)
        return self._request("GET", "get_issue", f"/repos/{owner}/{repo}/issues/{number}", {})

    def _list_pr_checks(self, params: dict[str, Any]) -> ConnectorResult:
        owner = _segment(_require(params, "owner"), "owner")
        repo = _segment(_require(params, "repo"), "repo")
        if params.get("ref"):
            ref = _ref(params["ref"])
        elif params.get("pull_number"):
            number = _positive_int(params["pull_number"], "pull_number", maximum=10_000_000)
            pr = self._request("GET", "list_pr_checks", f"/repos/{owner}/{repo}/pulls/{number}", {})
            head = pr.data.get("head") if isinstance(pr.data, dict) else None
            sha = head.get("sha") if isinstance(head, dict) else None
            if not sha:
                raise ConnectorError("The pull request response did not contain head.sha")
            ref = _ref(sha)
        else:
            raise ConnectorError("list_pr_checks needs a commit ref or a pull_number")
        encoded = urllib.parse.quote(ref, safe="/")
        query = {"per_page": str(_positive_int(params.get("per_page", 30), "per_page"))}
        return self._request(
            "GET", "list_pr_checks", f"/repos/{owner}/{repo}/commits/{encoded}/check-runs", query
        )

    # ----------------------------------------------------------------- request
    def _request(
        self,
        method: str,
        operation: str,
        path: str,
        query: dict[str, str],
        *,
        body: bytes | None = None,
    ) -> ConnectorResult:
        url = urllib.parse.urlunsplit(
            (urllib.parse.urlsplit(self._api_base).scheme, urllib.parse.urlsplit(self._api_base).netloc, path, urllib.parse.urlencode(sorted(query.items())), "")
        )
        self._validate_url(url)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "MayaWorkbench/0.1 (read-only connector)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/vnd.github+json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # The request hash intentionally excludes headers so a token can never
        # influence or appear in provenance. A body hash is included for writes.
        request_meta: dict[str, Any] = {"method": method, "url": url}
        if body is not None:
            request_meta["body_sha256"] = hashlib.sha256(body).hexdigest()
        request_hash = _sha256_json(request_meta)
        if self._http is not None:
            reply = (
                self._http(method, url, headers, body)
                if body is not None
                else self._http(method, url, headers)
            )
        else:
            reply = self._guarded_http(method, url, headers, body)
        if not isinstance(reply, HttpReply):
            raise ConnectorError("HTTP function returned an invalid response")
        if reply.final_url:
            self._validate_url(reply.final_url)
        if not 200 <= reply.status < 300:
            raise ConnectorError(f"GitHub returned HTTP {reply.status} for {self._target(url)}")
        try:
            data = json.loads(reply.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("GitHub returned a non-JSON response") from exc
        parsed = urllib.parse.urlsplit(url)
        return ConnectorResult(
            connector=self.name,
            operation=operation,
            target=self._target(url),
            data=data,
            request_hash=request_hash,
            response_hash=hashlib.sha256(reply.body).hexdigest(),
            retrieved_at=to_iso(utcnow()),
            read_only=True,
        )

    @staticmethod
    def _target(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return f"{parsed.hostname}{parsed.path}"

    # ------------------------------------------------------------------- guard
    def _validate_url(self, url: str) -> urllib.parse.SplitResult:
        """Reject anything outside the exact, https-only allowlist."""
        parsed = urllib.parse.urlsplit((url or "").strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            raise PolicyError("GitHub connector allows only https URLs")
        if parsed.username or parsed.password:
            raise PolicyError("URLs with embedded credentials are refused")
        if host in {"localhost", "localhost.localdomain"} or host not in self._allowed_hosts:
            raise PolicyError(
                f"Refusing GitHub connector request to non-allowlisted host {host or '<none>'!r}",
                hint=f"Allowed hosts: {', '.join(sorted(self._allowed_hosts))}",
            )
        if parsed.port not in (None, 443):
            raise PolicyError("GitHub connector allows only the default HTTPS port")
        if not parsed.path.startswith("/repos/"):
            raise PolicyError("GitHub connector only exposes /repos/ API paths")
        return parsed

    def _guarded_http(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpReply:
        """Real network path. Mirrors ``research/fetch.py``'s pinned SSRF guard."""
        current = url
        for _hop in range(self.max_redirects + 1):
            parsed = self._validate_url(current)
            host = parsed.hostname or ""
            port = parsed.port or 443
            address = self._resolve_public(host, port)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection = _pinned_connection("https", host, port, address, self.timeout_seconds)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                status = int(response.status)
                if status in _REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    response.read()
                    if not location:
                        raise PolicyError("GitHub redirect had no Location header")
                    current = urllib.parse.urljoin(current, location)
                    self._validate_url(current)
                    continue
                body = self._read_bounded(response)
                reply_headers = {
                    key: value
                    for key, value in response.getheaders()
                    if isinstance(key, str) and isinstance(value, str)
                }
                return HttpReply(status=status, body=body, headers=reply_headers, final_url=current)
            except PolicyError:
                raise
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                raise ConnectorError(f"Cannot reach the GitHub API: {exc}") from exc
            finally:
                connection.close()
        raise PolicyError("Too many redirects from the GitHub API")

    @staticmethod
    def _resolve_public(host: str, port: int) -> str:
        addresses: list[str] = []
        try:
            for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
                raw = info[4][0]
                if raw not in addresses:
                    addresses.append(raw)
        except socket.gaierror as exc:
            raise PolicyError(f"Cannot resolve {host}: {exc}") from exc
        if not addresses:
            raise PolicyError(f"{host} did not resolve to any address")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw.split("%")[0])
            except ValueError as exc:
                raise PolicyError(f"{host} resolved to an unparsable address") from exc
            if not _is_public(address):
                raise PolicyError(
                    f"Refusing GitHub connector request to non-public address {raw}",
                    hint="External connectors must not reach local or private networks.",
                )
        return addresses[0]

    def _read_bounded(self, response: Any) -> bytes:
        data = bytearray()
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            if len(data) + len(chunk) > self.max_bytes:
                raise ConnectorError("GitHub response exceeded the connector byte limit")
            data.extend(chunk)
        return bytes(data)


class GitHubConnector(GitHubReadOnlyConnector):
    """GitHub adapter with one approval-gated, proposal-only write operation.

    ``create_draft_pr`` POSTs a *draft* pull request. It never merges, never
    comments, and never edits an existing PR. The write is refused unless the
    registry injects a verified, one-time approval grant whose spec hash binds
    the exact owner/repo/head/base/title/body being proposed.
    """

    name = "github"
    mode = "READ_ONLY_PLUS_APPROVED_DRAFT_PROPOSAL"
    contract = ADVANCED_CONTRACT
    _OPERATIONS = ("list_issues", "get_issue", "list_pr_checks", "create_draft_pr")
    _MERGE_INTENT_KEYS = frozenset(
        {"merge", "merge_method", "merge_commit_sha", "auto_merge", "sha", "commit_sha", "action"}
    )

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described["mode"] = self.mode
        described["writes_supported"] = True
        described["never_merges"] = True
        described["contract"] = self.contract.model_dump(mode="json")
        described["approval_required_operations"] = ["create_draft_pr"]
        described["note"] = (
            "Read-only issues/checks plus a draft-PR proposal. Creating a draft PR "
            "requires a one-time approval and never merges. Token is sent only as "
            "an outgoing header and never included in results, hashes or events."
        )
        return described

    def describe_target(self, operation: str, params: dict[str, Any]) -> str:
        if operation == "create_draft_pr":
            parts = urllib.parse.urlsplit(self._api_base)
            owner = _segment(_require(params, "owner"), "owner")
            repo = _segment(_require(params, "repo"), "repo")
            return f"{parts.hostname}/repos/{owner}/{repo}/pulls"
        return super().describe_target(operation, params)

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        if operation == "create_draft_pr":
            return self._create_draft_pr(params)
        return super().invoke(operation, params)

    def _create_draft_pr(self, params: dict[str, Any]) -> ConnectorResult:
        reject_inline_secrets(params)
        if self._revoked:
            raise ConnectorError(
                "GitHub connector has been revoked; re-create it before use",
                hint="A revoked connector cannot propose any pull request.",
            )
        if not self._token:
            raise PolicyError(
                f"No GitHub token found in ${self._token_env}",
                hint="Export a token with pull-request write scope before proposing a draft PR.",
            )
        for key in self._MERGE_INTENT_KEYS:
            if key in params:
                raise ConnectorError(
                    f"Parameter {key!r} would request a merge or edit; this connector only proposes drafts"
                )

        owner = _segment(_require(params, "owner"), "owner")
        repo = _segment(_require(params, "repo"), "repo")
        head = _ref(_require(params, "head"))
        base = _ref(_require(params, "base"))
        title = str(params.get("title") or "").strip()
        if not title or len(title) > 500:
            raise ConnectorError("A draft PR title of 1..500 characters is required")
        body = str(params.get("body") or "")[:5000]

        grant_raw = params.get(APPROVAL_PARAM)
        if not isinstance(grant_raw, dict):
            raise ApprovalError(
                "Creating a draft PR requires an approved, one-time approval token",
                hint="Propose the exact action, approve it, then pass its approval id.",
            )
        if grant_raw.get("connector") != self.name or grant_raw.get("operation") != "create_draft_pr":
            raise ApprovalError("Approval grant does not match this connector and operation")
        target = self.describe_target("create_draft_pr", params)
        expected = spec_sha256(
            action_spec(
                connector=self.name,
                operation="create_draft_pr",
                target=target,
                params=public_params(params),
            )
        )
        if grant_raw.get("spec_sha256") != expected:
            raise ApprovalError("Approval grant does not match this exact proposed action")
        if not grant_raw.get("approval_id") or not grant_raw.get("actor"):
            raise ApprovalError("Approval grant is missing its identity fields")

        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": True,
            "maintainer_can_modify": False,
        }
        body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        result = self._request(
            "POST",
            "create_draft_pr",
            f"/repos/{owner}/{repo}/pulls",
            {},
            body=body_bytes,
        )
        proposed = result.data if isinstance(result.data, dict) else {}
        data = {
            "proposal": proposed,
            "draft": True,
            "never_merges": True,
            "proposal_only": True,
            "approval_id": grant_raw.get("approval_id"),
            "requested_title": title,
            "head": head,
            "base": base,
        }
        self._state["create_draft_pr"] = result
        return result.model_copy(update={"data": data, "read_only": False})


__all__ = [
    "ALLOWED_HOSTS",
    "ADVANCED_CONTRACT",
    "READ_CONTRACT",
    "HttpReply",
    "HttpFunction",
    "GitHubReadOnlyConnector",
    "GitHubConnector",
]

"""Strict validation of query parameters and request paths.

The control plane accepts only short, well-formed values.  Oversized queries,
duplicate parameters and path traversal are refused here, before any service is
called, so the API never becomes an accidental file or command surface.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any
from urllib.parse import ParseResult, urlsplit

from mayaworkbench.api.errors import ApiError

#: The only hostnames this control plane answers for. The browser sends the
#: hostname from the URL, so a DNS-rebinding page (``evil.example`` resolving to
#: 127.0.0.1) is rejected by the Host check below even though the TCP connection
#: is local.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Fetch metadata values that may accompany a same-origin mutation.
SAFE_FETCH_SITES = frozenset({"same-origin", "none"})

# Bounds chosen to be generous for real identifiers but far below anything that
# could be used to smuggle a payload through a query string.
MAX_QUERY_VALUE = 256
MAX_REQUEST_PATH = 4096
MAX_BODY_BYTES = 1_048_576

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
# ProjectStore.get accepts a slug OR a prefixed ULID (e.g. ``proj_01M7...``),
# whose ULID part is uppercase Crockford base32.
PROJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
SEARCH_MODES = frozenset({"auto", "lexical", "exact", "regex"})


def param(
    query: dict[str, list[str]],
    name: str,
    *,
    required: bool = False,
    default: Any = None,
    max_len: int = MAX_QUERY_VALUE,
    allow: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    pattern: re.Pattern[str] | None = None,
    as_int: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> Any:
    """Read exactly one validated query parameter."""

    values = query.get(name)
    if not values:
        if required:
            raise ApiError(
                400,
                "MISSING_PARAMETER",
                f"Missing required query parameter '{name}'.",
                hint=f"Supply ?{name}=<value>.",
            )
        return default
    if len(values) != 1:
        raise ApiError(
            400,
            "INVALID_PARAMETER",
            f"Query parameter '{name}' must be supplied exactly once.",
        )
    value = values[0]
    if len(value) > max_len:
        raise ApiError(
            400,
            "PARAMETER_TOO_LONG",
            f"Query parameter '{name}' exceeds the {max_len}-character limit.",
        )
    if allow is not None and value not in allow:
        raise ApiError(
            400,
            "INVALID_PARAMETER",
            f"Query parameter '{name}' must be one of: {', '.join(sorted(allow))}.",
        )
    if pattern is not None and not pattern.fullmatch(value):
        raise ApiError(
            400,
            "INVALID_PARAMETER",
            f"Query parameter '{name}' has an invalid format.",
        )
    if as_int:
        try:
            number = int(value)
        except ValueError:
            raise ApiError(
                400,
                "INVALID_PARAMETER",
                f"Query parameter '{name}' must be an integer.",
            )
        if minimum is not None and number < minimum:
            raise ApiError(
                400,
                "PARAMETER_OUT_OF_RANGE",
                f"Query parameter '{name}' must be at least {minimum}.",
            )
        if maximum is not None and number > maximum:
            raise ApiError(
                400,
                "PARAMETER_OUT_OF_RANGE",
                f"Query parameter '{name}' must be at most {maximum}.",
            )
        return number
    return value


def ensure_safe_path(path: str) -> str:
    """Reject encoded/unencoded traversal, null bytes and backslashes.

    Route identifiers are only ever used in parameterised storage lookups, but
    refusing suspicious paths keeps the surface explicit and testable.
    """

    if "\x00" in path or "\\" in path:
        raise ApiError(400, "INVALID_PATH", "The request path contains invalid characters.")
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise ApiError(400, "INVALID_PATH", "Path traversal is not allowed.")
    if posixpath.normpath(path) not in {path, path.rstrip("/"), "/"}:
        raise ApiError(400, "INVALID_PATH", "The request path is not canonical.")
    return path


def parse_body(raw: bytes) -> dict[str, Any]:
    """Parse a bounded JSON object body for POST routes."""

    if not raw:
        return {}
    import json

    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(400, "REQUEST_TOO_LARGE", "The request body is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "INVALID_JSON", "The request body must be a JSON object.")
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_JSON", "The request body must be a JSON object.")
    return payload


def guard_request_path(parsed: ParseResult) -> None:
    """Check the raw (still-encoded) target before unquoting it."""

    if len(parsed.path) + len(parsed.query) + 1 > MAX_REQUEST_PATH:
        raise ApiError(400, "REQUEST_TOO_LARGE", "The request target is too large.")


def host_without_port(value: str | None) -> str:
    """Normalise a Host header or an Origin netloc to a comparable hostname."""

    text = (value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("["):  # IPv6 literal: [::1]:8765
        return text.split("]", 1)[0].lstrip("[")
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    return text


def guard_local_request(
    *,
    method: str,
    host_header: str | None,
    origin: str | None,
    referer: str | None,
    sec_fetch_site: str | None,
) -> None:
    """Enforce the local-only request contract (MW-NEW-P1-09).

    Four independent checks, each of which the browser can be relied on to
    send: the Host header must be loopback (DNS-rebinding defence); a mutation
    must not carry a cross-site fetch metadata value; a mutation must not
    originate from a non-loopback Origin/Referer; and a mutation from a browser
    with no Origin at all must not be a cross-site form post. Non-browser local
    clients (the CLI, a script) send none of these headers and still work.
    """

    host = host_without_port(host_header)
    if host and host not in LOOPBACK_HOSTS:
        raise ApiError(
            403,
            "HOST_NOT_LOOPBACK",
            "This control plane answers only loopback hostnames.",
            hint="Use http://127.0.0.1:<port> or http://localhost:<port>.",
        )

    mutation = method.upper() not in {"GET", "HEAD", "OPTIONS"}
    site = (sec_fetch_site or "").strip().lower()
    if mutation and site and site not in SAFE_FETCH_SITES:
        raise ApiError(
            403,
            "CROSS_SITE_REQUEST",
            "Cross-site mutations are refused by the local security contract.",
        )
    for label, value in (("Origin", origin), ("Referer", referer)):
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            if mutation:
                raise ApiError(
                    403, "CROSS_ORIGIN_REQUEST", f"The {label} header is not a web origin."
                )
            continue
        if host_without_port(parsed.netloc) not in LOOPBACK_HOSTS and mutation:
            raise ApiError(
                403,
                "CROSS_ORIGIN_REQUEST",
                "Cross-origin mutations are refused by the local security contract.",
                hint="Drive the API from the local UI or a local client.",
            )
    if mutation and not origin and site in {"cross-site", "same-site"}:  # pragma: no cover
        raise ApiError(403, "CROSS_SITE_REQUEST", "Cross-site mutations are refused.")


# --------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_PUBLIC_HOSTS = frozenset({"0.0.0.0", "", "[::]"})
RATE_WINDOW_SECONDS = 10.0
# Generous for a local UI (a page + its NDJSON polls + approvals), still a real
# ceiling against accidental or deliberate local flooding. Tests override the
# server's limiter threshold directly to exercise the 429 path deterministically.
RATE_WINDOW_MAX = 600


def scrub_secrets(value):  # type: ignore[no-untyped-def]
    """Recursively replace credential-shaped substrings in a JSON-like value.

    The control plane must never echo secrets back in timeline, event streams,
    diagnostics or export bundles, even if a backend forgets to redact. This is
    a last-line scrubber over the serialised shape, applied at the server
    boundary; the authoritative redaction stays at the tool/storage layer.
    """
    from mayaworkbench.workspace.tools import redact

    def walk(item):  # type: ignore[no-untyped-def]
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, tuple):
            return [walk(child) for child in item]
        if isinstance(item, dict):
            out = {}
            for key, val in item.items():
                out[key if isinstance(key, str) else str(key)] = walk(val)
            return out
        if isinstance(item, float):
            return item
        return item

    return walk(value)


def check_host_header(
    host_header: str | None,
    *,
    expected_hosts: set[str],
) -> None:
    """DNS-rebinding guard: the Host must name an address we actually bound.

    A local-only API should not answer requests for arbitrary hostnames.
    """
    if not host_header:
        return
    normalized = host_header.strip().lower()
    expected = {
        item.strip().lower() for item in expected_hosts if item.strip()
    }
    if normalized in expected:
        return
    # Allow alternate loopback spellings with or without an explicit port.
    base = normalized.split(":", 1)[0] if not normalized.startswith("[") else normalized
    base = base.split("]", 1)[0].lstrip("[") if base.startswith("[") else base
    if base in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return
    raise ApiError(403, "HOST_MISMATCH", f"Unexpected Host header: {host_header[:128]}.")


def check_origin(origin_header: str | None, *, allowed_origins: set[str]) -> None:
    """Cross-origin POST guard: refuse cross-origin browser-form POSTs.

    Browsers cannot set ``Origin`` to another site, so any POST whose Origin
    names a different origin than ours is a classic CSRF attempt. Non-browser
    clients are expected to use the NDJSON/JSON API directly; they must still
    present an allowed origin or no Origin header at all.
    """
    if not origin_header:
        return
    origin = origin_header.strip().lower()
    if origin in allowed_origins:
        return
    raise ApiError(403, "ORIGIN_FORBIDDEN", f"Cross-origin request is not allowed: {origin[:128]}.")


def compute_allowed_hosts(host: str, port: int) -> set[str]:
    """The Host values the server may accept for a binding."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"0.0.0.0:{port}"}
    hosts.add(f"127.0.0.1")
    if not host or host.strip() in _PUBLIC_HOSTS:
        # Bound to all interfaces: accept the loopback names and the empty form.
        hosts.add("")
    return hosts


def compute_allowed_origins(host: str, port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}


__all__ = [
    "MAX_BODY_BYTES",
    "ID_RE",
    "SLUG_RE",
    "PROJECT_REF_RE",
    "SEARCH_MODES",
    "LOOPBACK_HOSTS",
    "SAFE_FETCH_SITES",
    "param",
    "ensure_safe_path",
    "parse_body",
    "guard_request_path",
    "guard_local_request",
    "scrub_secrets",
    "check_host_header",
    "check_origin",
    "compute_allowed_hosts",
    "compute_allowed_origins",
    "RATE_WINDOW_SECONDS",
    "RATE_WINDOW_MAX",
    "host_without_port",
]

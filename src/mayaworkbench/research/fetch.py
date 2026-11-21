"""Controlled retrieval of public research documents.

Network access is the exception in a local-first product, so this adapter is
narrow on purpose:

* only ``http``/``https``;
* credentials embedded in a URL are refused;
* the host must resolve exclusively to public addresses (no loopback, private,
  link-local, multicast or reserved ranges), checked on every redirect hop —
  this is the SSRF boundary;
* redirects, bytes and time are bounded;
* only document media types are accepted; an HTML landing page is refused with
  an actionable message rather than silently stored as unreadable text.

The fetcher is injectable so tests never touch the network.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from mayaworkbench.clock import utcnow
from mayaworkbench.config import Settings
from mayaworkbench.errors import FetchError, UsageError

#: Media types that are actually documents we can extract text from.
DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/x-markdown",
    "text/x-rst",
    "text/restructuredtext",
}

#: Metadata APIs return JSON. The same SSRF/address-pinning path is reused, but
#: only these media types are accepted on it.
JSON_CONTENT_TYPES = {"application/json", "application/vnd.api+json"}

DOI_RE = r"^10\.\d{4,9}/\S+$"


@dataclass
class FetchedDocument:
    requested_url: str
    final_url: str
    content_type: str
    data: bytes
    status: int
    retrieved_at: datetime

    @property
    def media_hint(self) -> str | None:
        content_type = self.content_type.split(";")[0].strip().lower()
        if content_type == "application/pdf" or self.data[:1024].lstrip().startswith(b"%PDF-"):
            return "PDF"
        if content_type in ("text/markdown", "text/x-markdown", "application/x-markdown"):
            return "MARKDOWN"
        path = urllib.parse.urlparse(self.final_url).path.lower()
        if path.endswith((".md", ".markdown")):
            return "MARKDOWN"
        if content_type in ("text/plain", "text/x-rst", "text/restructuredtext"):
            return "TEXT"
        return None


class DocumentFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_bytes: int = 25_000_000,
        max_redirects: int = 4,
        allow_private_network: bool = False,
        user_agent: str = "MayaWorkbench/0.1 (local research workbench)",
        opener_factory=None,  # type: ignore[no-untyped-def]
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.timeout_seconds = int(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self.allow_private_network = bool(allow_private_network)
        self.user_agent = user_agent
        self._opener_factory = opener_factory

    # ------------------------------------------------------------------ public
    def fetch(self, url: str) -> FetchedDocument:
        if self._opener_factory is not None or self.allow_private_network:
            return self._fetch_with_opener(self._validate_url(url))
        # The pinned path resolves exactly once per hop, inside _fetch_pinned;
        # resolving here as well would create a second, unused DNS answer.
        requested = self._validate_url(url, resolve=False)
        return self._fetch_pinned(requested)

    def _fetch_with_opener(self, requested: str) -> FetchedDocument:
        """Explicit opt-in path (tests or an operator-allowed private network)."""
        opener = self._build_opener()
        request = urllib.request.Request(
            requested,
            headers={
                "User-Agent": self.user_agent,
                "Accept": (
                    "application/pdf, text/markdown;q=0.9, text/plain;q=0.8, "
                    "text/x-rst;q=0.5"
                ),
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200) or 200)
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "") or ""
                data = self._read_bounded(response)
        except urllib.error.HTTPError as exc:
            raise FetchError(
                f"The server returned HTTP {exc.code} for {requested}",
                hint="Check the URL, or download the document and register the file instead.",
            ) from exc
        except urllib.error.URLError as exc:
            raise FetchError(
                f"Cannot reach {requested}: {exc.reason}",
                hint="Check network access or register a local copy instead.",
            ) from exc
        except TimeoutError as exc:
            raise FetchError(
                f"Fetching {requested} timed out after {self.timeout_seconds}s"
            ) from exc
        self._check_content_type(content_type, data, final_url)
        return FetchedDocument(
            requested_url=requested,
            final_url=final_url,
            content_type=content_type,
            data=data,
            status=status,
            retrieved_at=utcnow(),
        )

    def fetch_json(self, url: str) -> FetchedDocument:
        """Fetch a JSON metadata response through the same SSRF-bounded path."""
        requested = self._validate_url(url, resolve=False)
        return self._fetch_pinned(requested, allowed_types=JSON_CONTENT_TYPES)

    def _fetch_pinned(
        self, requested: str, *, allowed_types: set[str] | None = None
    ) -> FetchedDocument:
        """Fetch by connecting to the *validated* address, not a re-resolution.

        A hostile DNS answer can change between validation and connection. This
        path resolves once per hop, rejects any non-public address in the
        answer, and connects to one of those exact addresses while keeping the
        original host for the ``Host`` header and TLS SNI.
        """
        current = requested
        for _hop in range(self.max_redirects + 1):
            parsed = urllib.parse.urlsplit(current)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            address = self._resolve_public(host, port)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection = _pinned_connection(
                parsed.scheme, host, port, address, self.timeout_seconds
            )
            try:
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": (
                            "application/pdf, text/markdown;q=0.9, text/plain;q=0.8, "
                            "text/x-rst;q=0.5"
                        ),
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                status = int(response.status)
                if status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    response.read()
                    if not location:
                        raise FetchError(
                            f"{current} redirected without a Location header"
                        )
                    current = urllib.parse.urljoin(current, location)
                    parsed_next = urllib.parse.urlsplit(current)
                    if parsed_next.scheme not in ("http", "https"):
                        raise FetchError(
                            "A redirect changed to a non-HTTP scheme and was refused"
                        )
                    if parsed_next.username or parsed_next.password:
                        raise FetchError(
                            "A redirect contained embedded credentials and was refused"
                        )
                    continue
                content_type = response.getheader("Content-Type", "") or ""
                data = self._read_http_body(response)
            except FetchError:
                raise
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                raise FetchError(
                    f"Cannot reach {current}: {exc}",
                    hint="Check network access or register a local copy instead.",
                ) from exc
            finally:
                connection.close()
            if status >= 400:
                raise FetchError(
                    f"The server returned HTTP {status} for {current}",
                    hint="Check the URL, or download the document and register the file instead.",
                )
            self._check_content_type(content_type, data, current, allowed_types=allowed_types)
            return FetchedDocument(
                requested_url=requested,
                final_url=current,
                content_type=content_type,
                data=data,
                status=status,
                retrieved_at=utcnow(),
            )
        raise FetchError(
            f"Too many redirects (more than {self.max_redirects}) fetching {requested}"
        )

    def _read_http_body(self, response) -> bytes:  # type: ignore[no-untyped-def]
        received = 0
        data = bytearray()
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            received += len(chunk)
            if received > self.max_bytes:
                raise FetchError(
                    f"The document is larger than the {self.max_bytes} byte fetch limit",
                    hint="Raise research.max_fetch_bytes or register a trimmed copy.",
                )
            data.extend(chunk)
        if not data:
            raise FetchError("The server returned an empty response")
        return bytes(data)

    def fetch_doi(self, doi: str) -> FetchedDocument:
        cleaned = (doi or "").strip()
        if not cleaned:
            raise UsageError("A DOI is required")
        # Validate before building the URL so a malformed value cannot become a
        # request to an unrelated host.
        import re

        if not re.match(DOI_RE, cleaned):
            raise UsageError(
                f"{doi!r} is not a DOI",
                hint="Expected a value like 10.48550/arXiv.1706.03762.",
            )
        return self.fetch("https://doi.org/" + urllib.parse.quote(cleaned, safe="/"))

    # ------------------------------------------------------------------ checks
    def _build_opener(self):  # type: ignore[no-untyped-def]
        if self._opener_factory is not None:
            return self._opener_factory(self)
        guard = self._check_target_address
        max_redirects = self.max_redirects

        class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            max_redirections = max_redirects

            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                guard(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        return urllib.request.build_opener(SafeRedirectHandler())

    def _read_bounded(self, response) -> bytes:  # type: ignore[no-untyped-def]
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_bytes:
                raise FetchError(
                    f"The document is larger than the {self.max_bytes} byte fetch limit",
                    hint="Raise research.max_fetch_bytes or register a trimmed copy.",
                )
            chunks.append(chunk)
        if not chunks:
            raise FetchError("The server returned an empty response")
        return b"".join(chunks)

    def _validate_url(self, url: str, *, resolve: bool = True) -> str:
        parsed = urllib.parse.urlsplit((url or "").strip())
        if parsed.scheme not in ("http", "https"):
            raise UsageError(
                "Only http(s) URLs can be fetched",
                hint="Register a local file for anything else.",
            )
        if not parsed.hostname:
            raise UsageError(f"{url!r} has no host")
        if parsed.username or parsed.password:
            raise UsageError("URLs with embedded credentials are refused")
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            raise FetchError(
                "Refusing to fetch from localhost",
                hint="This guard exists so document retrieval cannot reach local services.",
            )
        if resolve:
            self._check_target_address(url)
        return urllib.parse.urlunsplit(parsed)

    def _check_target_address(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if not host:
            raise FetchError(f"Redirect target {url!r} has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self.allow_private_network:
            return
        self._resolve_public(host, port)

    def _resolve_public(self, host: str, port: int) -> str:
        """Resolve once, reject any non-public answer, return one address.

        The returned address is what the socket connects to, so a second DNS
        answer cannot redirect the connection to a private destination.
        """
        addresses: list[str] = []
        try:
            for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
                raw = info[4][0]
                if raw not in addresses:
                    addresses.append(raw)
        except socket.gaierror as exc:
            raise FetchError(f"Cannot resolve {host}: {exc}") from exc
        if not addresses:
            raise FetchError(f"{host} did not resolve to any address")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw.split("%")[0])
            except ValueError as exc:  # pragma: no cover - defensive
                raise FetchError(f"{host} resolved to an unparsable address") from exc
            if _is_public(address):
                continue
            raise FetchError(
                f"Refusing to fetch {host}: it resolves to the non-public address {raw}",
                hint=(
                    "Document retrieval must not reach local or private networks. "
                    "Set research.allow_private_network_fetch only if you understand the risk."
                ),
            )
        return addresses[0]

    def _check_content_type(
        self,
        content_type: str,
        data: bytes,
        url: str,
        *,
        allowed_types: set[str] | None = None,
    ) -> None:
        cleaned = content_type.split(";")[0].strip().lower()
        accepted = DOCUMENT_CONTENT_TYPES if allowed_types is None else allowed_types
        if cleaned in accepted:
            return
        if (
            allowed_types is None
            and cleaned in ("application/octet-stream", "")
            and data[:1024].lstrip().startswith(b"%PDF-")
        ):
            return
        if cleaned in ("text/html", "application/xhtml+xml"):
            raise FetchError(
                f"{url} returned an HTML page, not a document",
                hint=(
                    "Save the article as Markdown or PDF and register the file, or use its "
                    "PDF link directly."
                ),
            )
        raise FetchError(
            f"Unsupported content type {cleaned or '<none>'} from {url}",
            hint="Accepted types: " + ", ".join(sorted(accepted)) + ".",
        )


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
    )


def fetcher_from_settings(settings: Settings) -> DocumentFetcher:
    research = settings.research
    return DocumentFetcher(
        timeout_seconds=research.fetch_timeout_seconds,
        max_bytes=research.max_fetch_bytes,
        max_redirects=research.fetch_max_redirects,
        allow_private_network=research.allow_private_network_fetch,
    )


__all__ = [
    "FetchedDocument",
    "DocumentFetcher",
    "DOCUMENT_CONTENT_TYPES",
    "JSON_CONTENT_TYPES",
    "fetcher_from_settings",
]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose socket goes to a pre-validated address."""

    def __init__(self, host: str, port: int, pinned_address: str, timeout: int):
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = pinned_address

    def connect(self) -> None:  # pragma: no cover - requires network
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout
        )
        _verify_peer(self.sock, self._pinned_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an address but verified as the hostname."""

    def __init__(self, host: str, port: int, pinned_address: str, timeout: int):
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = pinned_address

    def connect(self) -> None:  # pragma: no cover - requires network
        raw = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        _verify_peer(self.sock, self._pinned_address)


def _verify_peer(sock, expected: str) -> None:  # type: ignore[no-untyped-def]
    peer = sock.getpeername()[0]
    try:
        same = ipaddress.ip_address(peer.split("%")[0]) == ipaddress.ip_address(
            expected.split("%")[0]
        )
    except ValueError:  # pragma: no cover - defensive
        same = False
    if not same:
        sock.close()
        raise FetchError(
            "The connected peer address did not match the validated address"
        )


def _pinned_connection(
    scheme: str, host: str, port: int, address: str, timeout: int
):  # type: ignore[no-untyped-def]
    if scheme == "https":
        return _PinnedHTTPSConnection(host, port, address, timeout)
    return _PinnedHTTPConnection(host, port, address, timeout)

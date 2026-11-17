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

import ipaddress
import socket
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
        requested = self._validate_url(url)
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

    def _validate_url(self, url: str) -> str:
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
        addresses: set[str] = set()
        try:
            for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
                addresses.add(info[4][0])
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

    def _check_content_type(self, content_type: str, data: bytes, url: str) -> None:
        cleaned = content_type.split(";")[0].strip().lower()
        if cleaned in DOCUMENT_CONTENT_TYPES:
            return
        if cleaned in ("application/octet-stream", "") and data[:1024].lstrip().startswith(b"%PDF-"):
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
            hint="Accepted types: PDF, Markdown, plain text and reStructuredText.",
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


__all__ = ["FetchedDocument", "DocumentFetcher", "DOCUMENT_CONTENT_TYPES", "fetcher_from_settings"]

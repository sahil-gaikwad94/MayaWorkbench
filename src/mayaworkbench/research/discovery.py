"""Scholarly metadata discovery with bounded, auditable provenance.

Discovery is deliberately narrow: a query goes to one provider with a strict
host allowlist, the request passes through the existing SSRF-bounded fetcher,
and every saved result becomes an immutable, content-addressed metadata
artifact plus an append-only index row. The in-memory provider keeps tests and
offline use entirely network-free; the Crossref provider builds its request
deterministically so the exact URL can be reviewed before any call is made.

Nothing here asserts that a hit is correct, relevant or licensed for reuse. A
discovered record is a lead with provenance, not evidence of a claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import Field, field_validator

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import ActorKind, ArtifactKind, EventType
from mayaworkbench.domain.models import (
    CLAIM_GRAPH_VERSION,
    Project,
    ResearchDocument,
    SourceMetadata,
    WorkflowEvent,
)
from mayaworkbench.errors import FetchError, UsageError
from mayaworkbench.ids import new_ulid
from mayaworkbench.research.fetch import DocumentFetcher
from mayaworkbench.research.models import Contract
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    ClaimCitationStore,
    EventStore,
    ProjectStore,
    ResearchClaimStore,
    ResearchDocumentStore,
    SourceMetadataStore,
)

#: Hosts a metadata provider is ever allowed to contact. A provider may narrow
#: this list further but never widen it.
ALLOWED_SEARCH_HOSTS = frozenset({"api.crossref.org"})

MAX_QUERY_CHARS = 300
MAX_RESULTS = 20
MAX_TITLE_CHARS = 500
MAX_AUTHORS = 50


class SourceHit(Contract):
    """One normalised metadata hit, before any persistence."""

    provider: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=500)
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    authors: list[str] = Field(default_factory=list)
    doi: str | None = None
    url: str | None = None
    published: str | None = None
    paper_version: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("doi")
    @classmethod
    def _doi_shape(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^10\.\d{4,9}/\S+$", value.strip()):
            raise ValueError("doi must look like 10.1234/abc")
        return value.strip() if value else value

    @property
    def document_key(self) -> str:
        if self.doi:
            return f"doi:{self.doi}"
        if self.url:
            return self.url
        return f"{self.provider}:{self.external_id}"


@dataclass(frozen=True)
class SearchRequest:
    """A fully built request. ``url`` is ``None`` for offline providers."""

    provider: str
    params: dict[str, str] = field(default_factory=dict)
    url: str | None = None


def enforce_host_allowlist(url: str, *, provider_hosts: frozenset[str]) -> str:
    """Validate a metadata URL before it reaches the fetcher.

    The fetcher handles DNS/address-level SSRF protection; this check is the
    tighter, static allowlist of *which* scholarly hosts discovery may use.
    """
    parsed = urllib.parse.urlsplit((url or "").strip())
    if parsed.scheme != "https":
        raise UsageError("Metadata search URLs must use https")
    if not parsed.hostname:
        raise UsageError(f"{url!r} has no host")
    if parsed.username or parsed.password:
        raise UsageError("Metadata URLs with embedded credentials are refused")
    host = parsed.hostname.lower()
    if host not in provider_hosts or host not in ALLOWED_SEARCH_HOSTS:
        raise UsageError(
            f"Metadata host {host!r} is not in the search allowlist",
            hint="Only " + ", ".join(sorted(ALLOWED_SEARCH_HOSTS)) + " is permitted.",
        )
    if parsed.port not in (None, 443):
        raise UsageError("Metadata search URLs must use the default HTTPS port")
    return urllib.parse.urlunsplit(parsed)


class SearchProvider(ABC):
    """A metadata search adapter. No network is used until ``search`` is called."""

    name: str = ""
    allowed_hosts: frozenset[str] = frozenset()

    @abstractmethod
    def build_request(self, query: str, *, limit: int) -> SearchRequest:
        """Build a deterministic request for ``query``."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int,
        fetcher: DocumentFetcher,
    ) -> list[SourceHit]:
        """Return at most ``limit`` normalised hits."""


class InMemorySearchProvider(SearchProvider):
    """Deterministic offline provider; never touches the network."""

    name = "memory"
    allowed_hosts = frozenset()

    def __init__(self, hits: Sequence[SourceHit | dict[str, Any]] = ()):
        self._hits = [
            hit if isinstance(hit, SourceHit) else SourceHit.model_validate(hit)
            for hit in hits
        ]

    def build_request(self, query: str, *, limit: int) -> SearchRequest:
        return SearchRequest(
            provider=self.name,
            params={"query": query.strip()[:MAX_QUERY_CHARS], "rows": str(limit)},
        )

    def search(
        self,
        query: str,
        *,
        limit: int,
        fetcher: DocumentFetcher,  # noqa: ARG002 - deliberately unused
    ) -> list[SourceHit]:
        needle = query.strip().casefold()
        if not needle:
            return []
        matches = [
            hit
            for hit in self._hits
            if needle in " ".join(
                [hit.title, hit.external_id, hit.doi or "", *hit.authors]
            ).casefold()
        ]
        return matches[:limit]


class CrossrefProvider(SearchProvider):
    """Crossref ``/works`` adapter with a deterministic, reviewable query."""

    name = "crossref"
    endpoint = "https://api.crossref.org/works"
    allowed_hosts = frozenset({"api.crossref.org"})

    def build_request(self, query: str, *, limit: int) -> SearchRequest:
        cleaned = query.strip()[:MAX_QUERY_CHARS]
        if not cleaned:
            raise UsageError("A search query is required")
        params = {
            "query.bibliographic": cleaned,
            "rows": str(limit),
            "select": "DOI,title,author,published,version,URL",
        }
        url = self.endpoint + "?" + urllib.parse.urlencode(params)
        return SearchRequest(provider=self.name, params=params, url=url)

    def search(
        self,
        query: str,
        *,
        limit: int,
        fetcher: DocumentFetcher,
    ) -> list[SourceHit]:
        request = self.build_request(query, limit=limit)
        assert request.url is not None  # guaranteed by build_request
        enforce_host_allowlist(request.url, provider_hosts=self.allowed_hosts)
        fetched = fetcher.fetch_json(request.url)
        try:
            payload = json.loads(fetched.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError("Crossref returned malformed JSON") from exc
        items = payload.get("message", {}).get("items", [])
        hits: list[SourceHit] = []
        for item in items[:limit]:
            external_id = str(item.get("DOI") or item.get("URL") or "")
            if not external_id:
                continue
            titles = item.get("title") or []
            authors = [
                " ".join(part for part in (author.get("given"), author.get("family")) if part)
                for author in item.get("author", [])[:MAX_AUTHORS]
            ]
            parts = item.get("published", {}).get("date-parts", [])
            published = "-".join(str(part) for part in parts[0]) if parts and parts[0] else None
            hits.append(
                SourceHit(
                    provider=self.name,
                    external_id=external_id,
                    title=str(titles[0])[:MAX_TITLE_CHARS] if titles else "",
                    authors=[name for name in authors if name],
                    doi=str(item["DOI"]) if item.get("DOI") else None,
                    url=str(item["URL"]) if item.get("URL") else None,
                    published=published,
                    paper_version=str(item.get("version")) if item.get("version") else None,
                    raw=item,
                )
            )
        return hits


@dataclass
class DiscoveryResult:
    provider: str
    query: str
    hits: list[SourceHit] = field(default_factory=list)
    saved: list[SourceMetadata] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DiscoveryService:
    """Search providers and persist immutable source metadata via an artifact."""

    def __init__(
        self,
        db: Database,
        *,
        projects: ProjectStore,
        documents: ResearchDocumentStore,
        claims: ResearchClaimStore,
        sources: SourceMetadataStore,
        artifacts: ArtifactStore,
        events: EventStore,
        fetcher: DocumentFetcher,
        citations: ClaimCitationStore | None = None,
    ):
        self.db = db
        self.projects = projects
        self.documents = documents
        self.claims = claims
        self.sources = sources
        self.artifacts = artifacts
        self.events = events
        self.fetcher = fetcher
        self._providers: dict[str, SearchProvider] = {}
        self._citations = citations

    def register_provider(self, provider: SearchProvider) -> None:
        """Register or replace a provider (tests inject seeded offline data)."""
        self._providers[provider.name] = provider

    def provider_names(self) -> list[str]:
        return sorted(self._providers)

    def default_providers(self) -> None:
        if "memory" not in self._providers:
            self.register_provider(InMemorySearchProvider())
        if "crossref" not in self._providers:
            self.register_provider(CrossrefProvider())

    def search(
        self,
        project: Project,
        query: str,
        *,
        provider: str = "memory",
        limit: int = 5,
        save: bool = False,
        document_key: str | None = None,
    ) -> DiscoveryResult:
        self.default_providers()
        cleaned = (query or "").strip()[:MAX_QUERY_CHARS]
        if not cleaned:
            raise UsageError("A search query is required")
        if limit < 1 or limit > MAX_RESULTS:
            raise UsageError(f"limit must be between 1 and {MAX_RESULTS}")
        selected = self._providers.get(provider)
        if selected is None:
            raise UsageError(
                f"Unknown search provider {provider!r}",
                hint="Available providers: " + ", ".join(self.provider_names()),
            )
        hits = selected.search(cleaned, limit=limit, fetcher=self.fetcher)
        result = DiscoveryResult(provider=selected.name, query=cleaned, hits=list(hits))
        if save:
            for hit in hits:
                result.saved.append(
                    self.save_source(
                        project,
                        hit,
                        document_key=document_key,
                        retrieval_url=selected.build_request(cleaned, limit=limit).url,
                    )
                )
        return result

    def save_source(
        self,
        project: Project,
        hit: SourceHit,
        *,
        document_key: str | None = None,
        retrieval_url: str | None = None,
    ) -> SourceMetadata:
        """Persist one hit as immutable metadata, idempotently by content hash."""
        key = (document_key or hit.document_key).strip()
        if not key:
            raise UsageError("A document key is required to save source metadata")
        payload: dict[str, Any] = {
            "provider": hit.provider,
            "external_id": hit.external_id,
            "title": hit.title,
            "authors": list(hit.authors),
            "doi": hit.doi,
            "url": hit.url,
            "published": hit.published,
            "paper_version": hit.paper_version,
            "raw": hit.raw,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self.sources.find_by_hash(
            project_id=project.id, document_key=key, metadata_sha256=digest
        )
        if existing is not None:
            return existing
        retrieved_at = utcnow()
        artifact_payload = {
            **payload,
            "document_key": key,
            "metadata_sha256": digest,
            "retrieved_at": retrieved_at.isoformat(),
            "protocol_version": CLAIM_GRAPH_VERSION,
        }
        artifact = self.artifacts.put_text(
            json.dumps(artifact_payload, indent=2, sort_keys=True, ensure_ascii=False),
            kind=ArtifactKind.SOURCE_METADATA,
            media_type="application/json",
            created_by="discovery",
        )
        metadata = SourceMetadata(
            id="src_" + new_ulid(),
            project_id=project.id,
            document_key=key,
            provider=hit.provider,
            external_id=hit.external_id,
            title=hit.title,
            authors=list(hit.authors),
            doi=hit.doi,
            url=hit.url,
            published=hit.published,
            paper_version=hit.paper_version,
            retrieval_url=retrieval_url,
            metadata_sha256=digest,
            retrieved_at=retrieved_at,
            artifact_id=artifact.id,
            protocol_version=CLAIM_GRAPH_VERSION,
            created_at=retrieved_at,
        )
        with self.db.transaction():
            self.sources.create(metadata)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.SOURCE_INGESTED,
                    project_id=project.id,
                    entity_type="source_metadata",
                    entity_id=metadata.id,
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="SYSTEM:discovery",
                    payload={
                        "provider": hit.provider,
                        "document_key": key,
                        "metadata_sha256": digest,
                        "artifact_id": artifact.id,
                        "paper_version": hit.paper_version,
                    },
                )
            )
        return metadata

    @property
    def citations(self) -> ClaimCitationStore:
        """Lazy citation store used to project which claims cite a bad source."""
        if self._citations is None:
            self._citations = ClaimCitationStore(self.db)
        return self._citations

    def _status_alert(
        self, metadata: SourceMetadata, documents: Sequence[ResearchDocument]
    ) -> dict[str, Any]:
        """Project one non-ACTIVE metadata record into affected documents/claims."""
        by_id: dict[str, Any] = {
            citation.id: citation
            for citation in self.citations.list_by_source(metadata.id)
        }
        for citation in self.citations.list_by_documents([item.id for item in documents]):
            by_id.setdefault(citation.id, citation)
        affected = sorted(by_id.values(), key=lambda item: (item.created_at, item.id))
        seen: set[tuple[str, int]] = set()
        document_refs: list[dict[str, Any]] = []
        for citation in affected:
            identity = (citation.document_id, citation.document_version)
            if identity not in seen:
                seen.add(identity)
                document_refs.append({"id": identity[0], "version": identity[1]})
        return {
            "source_metadata_id": metadata.id,
            "document_key": metadata.document_key,
            "status": metadata.status.value,
            "version_label": metadata.version_label,
            "correction_of": metadata.correction_of,
            "corrected_by": metadata.corrected_by,
            "retraction_date": metadata.retraction_date,
            "retraction_reason": metadata.retraction_reason,
            "relation_note": metadata.relation_note,
            "documents": document_refs,
            "claim_ids": sorted({citation.claim_id for citation in affected}),
        }

    def versions(
        self, project: Project, document_key: str | None = None
    ) -> dict[str, Any]:
        """Combine registered document versions with saved source metadata.

        A document key (a DOI or URL is used verbatim) narrows the projection;
        omitting it reports project-wide version alerts. Versions are returned
        newest first and carry a claim count. ``status_alerts`` lists every
        non-ACTIVE metadata record with the exact document ids/versions and
        claim ids that cite it. No external calls are made here.
        """
        cleaned = (document_key or "").strip()
        if cleaned:
            documents = self.documents.list_versions(project.id, cleaned)
            sources = self.sources.list_by_key(project.id, cleaned)
            alerting = self.sources.list_non_active_by_key(project.id, cleaned)
        else:
            documents = []
            sources = self.sources.list_by_project(project.id)
            alerting = self.sources.list_non_active(project.id)
        return {
            "key": cleaned,
            "project_id": project.id,
            "documents": [
                {
                    **document.model_dump(mode="json"),
                    "claim_count": self.claims.count(document_id=document.id),
                }
                for document in documents
            ],
            "source_metadata": [item.model_dump(mode="json") for item in sources],
            "status_alerts": [self._status_alert(item, documents) for item in alerting],
        }

    def get_source(self, metadata_id: str) -> SourceMetadata:
        return self.sources.get(metadata_id)


__all__ = [
    "ALLOWED_SEARCH_HOSTS",
    "SourceHit",
    "SearchRequest",
    "SearchProvider",
    "InMemorySearchProvider",
    "CrossrefProvider",
    "DiscoveryResult",
    "DiscoveryService",
    "enforce_host_allowlist",
]

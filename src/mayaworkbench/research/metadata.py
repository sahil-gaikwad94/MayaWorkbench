"""Bibliographic metadata extraction without fabrication.

Every field records where it came from (``sources``). If the supplied file does
not state a title, author, date, DOI or licence, the field stays empty and the
memo says so. A filename stem may be used as a display title, but it is marked as
such and never presented as the paper's title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mayaworkbench.research.pdf import PdfExtraction

MAX_TITLE_CHARS = 400
_FRONT_MATTER_FENCE = re.compile(r"^(---|\.\.\.)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)")
_LICENSE_LINE_RE = re.compile(
    r"(?im)^\s*(?:licen[cs]e|licen[cs]ed under)\s*[:\-]\s*(.{2,300})$"
)


@dataclass
class DocumentMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    organization: str | None = None
    published_at: str | None = None
    doi: str | None = None
    license: str | None = None
    source_url: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    body_start: int = 0


def parse_front_matter(text: str) -> tuple[dict[str, Any], int, list[str]]:
    """Parse a simple YAML front matter block.

    Only the small subset used by research notes is supported: ``key: value``,
    bracketed lists and ``- item`` continuation lines. This is deliberately not a
    YAML parser; unsupported syntax is reported instead of being guessed at.
    """
    warnings: list[str] = []
    if not text.startswith("---"):
        return {}, 0, warnings
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, 0, warnings
    end_line = None
    for index in range(1, min(len(lines), 400)):
        if _FRONT_MATTER_FENCE.match(lines[index].strip()):
            end_line = index
            break
    if end_line is None:
        warnings.append("FRONT_MATTER_UNTERMINATED")
        return {}, 0, warnings

    fields: dict[str, Any] = {}
    current_key: str | None = None
    for raw in lines[1:end_line]:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_key is not None:
            fields.setdefault(current_key, [])
            if not isinstance(fields[current_key], list):
                fields[current_key] = [fields[current_key]]
            fields[current_key].append(_unquote(stripped[2:].strip()))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", stripped)
        if match is None:
            warnings.append(f"FRONT_MATTER_UNPARSED_LINE: {stripped[:80]}")
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        current_key = key
        if value == "":
            fields[key] = []
            continue
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [
                _unquote(item.strip())
                for item in value[1:-1].split(",")
                if item.strip()
            ]
            continue
        fields[key] = _unquote(value)

    body_start = sum(len(line) for line in lines[: end_line + 1])
    return fields, body_start, warnings


def metadata_from_markdown(text: str, *, filename: str) -> DocumentMetadata:
    fields, body_start, warnings = parse_front_matter(text)
    body = text[body_start:]
    metadata = DocumentMetadata(title="", body_start=body_start, warnings=warnings)

    title = _first_field(fields, "title", "name")
    if title:
        metadata.title = _clean_title(str(title))
        metadata.sources["title"] = "front_matter"
    else:
        heading = _first_heading(body)
        if heading:
            metadata.title = _clean_title(heading)
            metadata.sources["title"] = "first_heading"
        else:
            metadata.title = _filename_title(filename)
            metadata.sources["title"] = "filename"
            metadata.warnings.append("TITLE_NOT_IN_SOURCE")

    authors = _first_field(fields, "authors", "author")
    if authors is not None:
        metadata.authors = split_authors(authors)
        metadata.sources["authors"] = "front_matter"

    organization = _first_field(
        fields, "organization", "organisation", "institution", "affiliation"
    )
    if organization:
        metadata.organization = _clean_title(str(organization))
        metadata.sources["organization"] = "front_matter"

    published = _first_field(
        fields, "published_at", "published", "publication_date", "date", "year"
    )
    if published:
        metadata.published_at = str(published).strip()
        metadata.sources["published_at"] = "front_matter"

    doi = _first_field(fields, "doi")
    if doi:
        metadata.doi = normalise_doi(str(doi))
        metadata.sources["doi"] = "front_matter"
    else:
        found = _DOI_RE.search(body[:8000])
        if found:
            metadata.doi = normalise_doi(found.group(1))
            metadata.sources["doi"] = "body"

    licence = _first_field(fields, "license", "licence")
    if licence:
        metadata.license = _clean_title(str(licence))
        metadata.sources["license"] = "front_matter"
    else:
        line = _LICENSE_LINE_RE.search(body)
        if line:
            metadata.license = _clean_title(line.group(1))
            metadata.sources["license"] = "body"

    url = _first_field(fields, "source_url", "url", "link")
    if url:
        metadata.source_url = str(url).strip()
        metadata.sources["source_url"] = "front_matter"

    known = {
        "title",
        "name",
        "authors",
        "author",
        "organization",
        "organisation",
        "institution",
        "affiliation",
        "published_at",
        "published",
        "publication_date",
        "date",
        "year",
        "doi",
        "license",
        "licence",
        "source_url",
        "url",
        "link",
    }
    metadata.extra = {
        key: value for key, value in fields.items() if key not in known
    }
    return metadata


def metadata_from_pdf(extraction: PdfExtraction, *, filename: str) -> DocumentMetadata:
    metadata = DocumentMetadata(title="", warnings=list(extraction.warnings))
    title = (extraction.metadata.get("title") or "").strip()
    if _plausible_pdf_title(title, filename):
        metadata.title = _clean_title(title)
        metadata.sources["title"] = "pdf_info_dictionary"
    else:
        metadata.title = _filename_title(filename)
        metadata.sources["title"] = "filename"
        metadata.warnings.append("TITLE_NOT_IN_SOURCE")

    author = (extraction.metadata.get("author") or "").strip()
    if author and len(author) <= 400:
        metadata.authors = split_authors(author)
        metadata.sources["authors"] = "pdf_info_dictionary"

    # A PDF creation timestamp is not a publication date, so it is kept as extra
    # provenance only. Claiming otherwise would fabricate bibliographic data.
    if extraction.metadata.get("created"):
        metadata.extra["pdf_created"] = extraction.metadata["created"]
    if extraction.metadata.get("subject"):
        metadata.extra["pdf_subject"] = extraction.metadata["subject"]
    return metadata


def metadata_from_text(text: str, *, filename: str) -> DocumentMetadata:
    metadata = DocumentMetadata(title=_filename_title(filename))
    metadata.sources["title"] = "filename"
    metadata.warnings.append("TITLE_NOT_IN_SOURCE")
    doi = _DOI_RE.search(text[:8000])
    if doi:
        metadata.doi = normalise_doi(doi.group(1))
        metadata.sources["doi"] = "body"
    line = _LICENSE_LINE_RE.search(text)
    if line:
        metadata.license = _clean_title(line.group(1))
        metadata.sources["license"] = "body"
    return metadata


def normalise_doi(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"(?i)^https?://(dx\.)?doi\.org/", "", cleaned)
    cleaned = re.sub(r"(?i)^doi:\s*", "", cleaned)
    return cleaned.strip().rstrip(".")


def split_authors(value: Any) -> list[str]:
    """Split an author field conservatively.

    Lists are taken as-is. Strings split on semicolons or a standalone "and";
    comma splitting only happens when every part looks like a full name, so
    "Smith, John" is not silently turned into two people.
    """
    if isinstance(value, list):
        return [_clean_title(str(item)) for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if ";" in text:
        parts = [part.strip() for part in text.split(";")]
    elif re.search(r"\s+and\s+", text):
        parts = [part.strip() for part in re.split(r"\s+and\s+", text)]
    elif "," in text:
        candidate = [part.strip() for part in text.split(",")]
        if all(len(part.split()) >= 2 and "." not in part for part in candidate):
            parts = candidate
        else:
            parts = [text]
    else:
        parts = [text]
    return [_clean_title(part) for part in parts if part]


def _first_field(fields: dict[str, Any], *names: str) -> Any | None:
    for name in names:
        if name in fields and fields[name] not in (None, "", []):
            return fields[name]
    return None


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2)
    return None


def _plausible_pdf_title(title: str, filename: str) -> bool:
    if len(title) < 3 or len(title) > MAX_TITLE_CHARS:
        return False
    lowered = title.lower()
    if lowered in {"untitled", "unknown", "title", "document", "pdf"}:
        return False
    if lowered == Path(filename).stem.lower():
        return False
    # Producer/export artefacts rather than a paper title.
    if re.match(r"^microsoft (word|powerpoint)", lowered):
        return False
    return True


def _filename_title(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    return _clean_title(stem) or "Untitled document"


def _clean_title(value: str) -> str:
    text = value.replace("\n", " ").strip()
    text = re.sub(r"^[#>*_\s]+", "", text)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text[:MAX_TITLE_CHARS].strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


__all__ = [
    "DocumentMetadata",
    "parse_front_matter",
    "metadata_from_markdown",
    "metadata_from_pdf",
    "metadata_from_text",
    "split_authors",
    "normalise_doi",
]

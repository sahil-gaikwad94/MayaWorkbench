"""Adversarial document-ingestion regressions (MWB-004, MWB-013)."""

from __future__ import annotations

import zlib

import pytest

from mayaworkbench.errors import UsageError
from mayaworkbench.research import pdf as pdf_module


def _pdf_with_content_stream(payload: bytes) -> bytes:
    header = b"%PDF-1.4\n"
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Contents 4 0 R >> endobj\n",
        (
            b"4 0 obj << /Length "
            + str(len(payload)).encode("ascii")
            + b" /Filter /FlateDecode >>\nstream\n"
            + payload
            + b"\nendstream endobj\n"
        ),
    ]
    return header + b"".join(objects) + b"trailer << /Root 1 0 R >>\n%%EOF\n"


def test_bounded_flate_refuses_an_expansion_bomb():
    bomb = zlib.compress(b"A" * 100_000)
    with pytest.raises(pdf_module._StreamTooLarge):
        pdf_module._bounded_flate(bomb, limit=4096)
    assert pdf_module._bounded_flate(zlib.compress(b"hello"), limit=1024) == b"hello"


def test_pdf_content_stream_over_budget_is_skipped_with_a_warning(monkeypatch):
    monkeypatch.setattr(pdf_module, "MAX_STREAM_BYTES", 4096)
    document = _pdf_with_content_stream(zlib.compress(b"BT (Hello) Tj ET" * 2000))
    extraction = pdf_module.extract_pdf_text(document)
    assert extraction.page_count == 1
    assert extraction.pages[0].text == ""
    assert any("decompression budget" in warning for warning in extraction.warnings)


def test_local_document_size_is_rejected_before_the_read(services, project, tmp_path):
    """MWB-013: the limit must apply to stat(), not after read_bytes()."""
    services.ingestion.settings.research.max_document_bytes = 1024
    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"x" * 4096)
    with pytest.raises(UsageError, match="above the 1024 byte"):
        services.ingestion.ingest_file(project, oversized)


def test_local_document_exactly_at_the_limit_is_read(services, project, tmp_path):
    services.ingestion.settings.research.max_document_bytes = 1024
    allowed = tmp_path / "allowed.md"
    allowed.write_text("# Heading\n\nSome evidence text.\n", encoding="utf-8")
    result = services.ingestion.ingest_file(project, allowed)
    assert result.created is True
    assert result.document.content_sha256

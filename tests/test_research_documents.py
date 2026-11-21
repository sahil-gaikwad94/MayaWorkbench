"""Adversarial document-ingestion regressions (MWB-004, MWB-013)."""

from __future__ import annotations

import zlib
from types import SimpleNamespace

import pytest

from mayaworkbench.errors import ExtractionError, UsageError
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


def _pypdf_that_fails_to_open(message="startxref not found"):
    class _Reader:
        def __init__(self, stream):
            raise ValueError(message)

    return SimpleNamespace(PdfReader=_Reader, __version__="fake-pypdf")


def test_pdf_falls_back_to_builtin_when_pypdf_rejects_the_file(monkeypatch):
    """MW-P0-01: an installed-but-failing pypdf must not block the builtin parser."""
    monkeypatch.setattr(pdf_module, "_load_pypdf", lambda: _pypdf_that_fails_to_open())
    document = _pdf_with_content_stream(zlib.compress(b"BT (Hello) Tj ET"))

    extraction = pdf_module.extract_pdf_text(document)

    assert extraction.tool == pdf_module.BUILTIN_TOOL
    assert extraction.page_count == 1
    assert "Hello" in extraction.pages[0].text
    assert any("built-in fallback used" in warning for warning in extraction.warnings)


def test_pdf_both_parsers_fail_reports_both_reasons(monkeypatch):
    monkeypatch.setattr(pdf_module, "_load_pypdf", lambda: _pypdf_that_fails_to_open())

    def _builtin_fails(data):
        raise ExtractionError("builtin boom")

    monkeypatch.setattr(pdf_module, "_extract_builtin", _builtin_fails)
    with pytest.raises(ExtractionError) as caught:
        pdf_module.extract_pdf_text(_pdf_with_content_stream(zlib.compress(b"BT (x) Tj ET")))
    message = str(caught.value)
    assert "pypdf failed" in message
    assert "built-in fallback also failed" in message
    assert "builtin boom" in message


def test_pdf_encrypted_document_is_never_fallback_extracted(monkeypatch):
    class _EncryptedReader:
        is_encrypted = True

        def __init__(self, stream):
            pass

        def decrypt(self, password):
            raise ValueError("bad password")

    monkeypatch.setattr(
        pdf_module,
        "_load_pypdf",
        lambda: SimpleNamespace(PdfReader=_EncryptedReader, __version__="fake"),
    )
    called = []

    def _must_not_run(data):
        called.append(data)
        raise AssertionError("fallback must not bypass encryption policy")

    monkeypatch.setattr(pdf_module, "_extract_builtin", _must_not_run)
    with pytest.raises(pdf_module.EncryptedPdfError, match="encrypted"):
        pdf_module.extract_pdf_text(_pdf_with_content_stream(zlib.compress(b"BT (x) Tj ET")))
    assert called == []


def test_pdf_no_text_from_pypdf_is_not_a_parser_failure(monkeypatch):
    """A scanned/image-only page is a valid no-text result, not a parse error."""

    class _EmptyPage:
        def extract_text(self):
            return ""

    class _EmptyReader:
        is_encrypted = False
        pages = [_EmptyPage()]
        metadata = None

        def __init__(self, stream):
            pass

    monkeypatch.setattr(
        pdf_module,
        "_load_pypdf",
        lambda: SimpleNamespace(PdfReader=_EmptyReader, __version__="fake"),
    )
    called = []
    monkeypatch.setattr(
        pdf_module, "_extract_builtin", lambda data: called.append(data) or None
    )

    extraction = pdf_module.extract_pdf_text(
        _pdf_with_content_stream(zlib.compress(b"BT (x) Tj ET"))
    )
    assert extraction.tool == "pypdf"
    assert extraction.pages[0].text == ""
    assert any("no text" in warning for warning in extraction.pages[0].warnings)
    assert called == []


def test_pdf_fallback_keeps_the_decompression_ceiling(monkeypatch):
    monkeypatch.setattr(pdf_module, "MAX_STREAM_BYTES", 4096)
    monkeypatch.setattr(pdf_module, "_load_pypdf", lambda: _pypdf_that_fails_to_open())
    document = _pdf_with_content_stream(zlib.compress(b"BT (Hello) Tj ET" * 2000))

    extraction = pdf_module.extract_pdf_text(document)

    assert extraction.tool == pdf_module.BUILTIN_TOOL
    assert extraction.pages[0].text == ""
    assert any("decompression budget" in warning for warning in extraction.warnings)


def test_builtin_parser_refuses_an_encrypted_document():
    document = _pdf_with_content_stream(zlib.compress(b"BT (x) Tj ET"))
    document += b"5 0 obj << /Filter /Standard >> endobj\ntrailer << /Encrypt 5 0 R >>\n"
    with pytest.raises(pdf_module.EncryptedPdfError, match="encrypted"):
        pdf_module._extract_builtin(document)


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

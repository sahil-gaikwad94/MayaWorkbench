"""OCR is explicit, provenance-bearing, and never presented as an exact quote."""

from __future__ import annotations

import json

from mayaworkbench.errors import ExtractionError
from mayaworkbench.research.ocr import (
    OcrPage,
    OcrResult,
    TesseractOcrAdapter,
    UnavailableOcrAdapter,
    _parse_tsv,
)

TSV = "level\tpage_num\tconf\ttext\n5\t1\t95.5\tHello\n5\t1\t80.0\tworld\n"


def test_unavailable_adapter_fails_closed():
    adapter = UnavailableOcrAdapter()
    assert adapter.available() is False
    try:
        adapter.extract(__import__("pathlib").Path("/tmp/none"))
    except ExtractionError:
        pass
    else:
        raise AssertionError("unavailable OCR must fail")


def test_tsv_parse_records_confidence():
    text, confidence = _parse_tsv(TSV)
    assert text == "Hello world"
    assert confidence == 87.75


def test_tesseract_adapter_uses_injected_runner(tmp_path):
    calls: list[list[str]] = []

    def runner(argv: list[str]):
        calls.append(argv)
        if "-png" in argv:
            prefix = argv[-1]
            (tmp_path / f"{__import__('pathlib').Path(prefix).name}-1.png").write_bytes(b"png")
            return b""
        return TSV.encode("utf-8")

    adapter = TesseractOcrAdapter(
        runner=runner, pdftoppm="pdftoppm", tesseract="tesseract", work_dir=tmp_path
    )
    assert adapter.available() is True
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    result = adapter.extract(source)
    assert len(result.pages) == 1
    assert "Hello world" in result.pages[0].text
    assert result.pages[0].source == "OCR"
    assert all(call[0] in {"pdftoppm", "tesseract"} for call in calls)


def test_ocr_result_serialisation_labels_provenance():
    result = OcrResult(
        pages=(OcrPage(1, "text", 0.9, "tesseract"),),
        tool="tesseract",
        version="1.0",
    )
    payload = json.loads(json.dumps(result.as_dict()))
    assert "never treat as an exact quote" in payload["note"]

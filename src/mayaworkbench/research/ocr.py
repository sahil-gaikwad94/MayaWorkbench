"""Explicit opt-in OCR for scanned documents (MW-P1-04).

OCR output is *never* merged into exact quotes. It is stored as a separate,
provenance-bearing artifact with page-level confidence, so downstream claims can
cite it only as OCR-derived evidence, never as an exact paper quote.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mayaworkbench.errors import ExtractionError

MAX_PAGES = 100
MAX_TEXT_CHARS = 2_000_000


@dataclass(frozen=True)
class OcrPage:
    number: int
    text: str
    mean_confidence: float
    engine: str
    source: str = "OCR"


@dataclass(frozen=True)
class OcrResult:
    pages: tuple[OcrPage, ...]
    tool: str
    version: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": [page.__dict__ for page in self.pages],
            "tool": self.tool,
            "version": self.version,
            "warnings": list(self.warnings),
            "note": "OCR-derived text; never treat as an exact quote from the source.",
        }


class OcrAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    def extract(self, document: Path, *, max_pages: int = MAX_PAGES) -> OcrResult: ...


class UnavailableOcrAdapter:
    """Fail-closed adapter used when no OCR engine is configured."""

    name = "unavailable"

    def available(self) -> bool:
        return False

    def extract(self, document: Path, *, max_pages: int = MAX_PAGES) -> OcrResult:
        raise ExtractionError(
            "No OCR engine is configured",
            hint="Install pdftoppm and tesseract, or supply an injected adapter.",
        )


class TesseractOcrAdapter:
    """External OCR engine driven by bounded argv (pdftoppm + tesseract TSV)."""

    name = "tesseract"

    def __init__(
        self,
        *,
        runner: Any = None,
        pdftoppm: str | None = None,
        tesseract: str | None = None,
        work_dir: Path | None = None,
    ):
        self.runner = runner
        self.pdftoppm = pdftoppm or shutil.which("pdftoppm") or ""
        self.tesseract = tesseract or shutil.which("tesseract") or ""
        self.work_dir = Path(work_dir) if work_dir else Path("/tmp")

    def available(self) -> bool:
        return bool(self.pdftoppm and self.tesseract)

    def _run(self, argv: list[str]) -> None:
        if self.runner is not None:
            self.runner(argv)
            return
        subprocess.run(argv, check=True, capture_output=True, timeout=60)

    def extract(self, document: Path, *, max_pages: int = MAX_PAGES) -> OcrResult:
        if not self.available():
            raise ExtractionError("OCR engine binaries are not available")
        source = Path(document).resolve()
        if not source.is_file():
            raise ExtractionError("document is missing")
        prefix = self.work_dir / f"ocr-{abs(hash(str(source)))}"
        self._run(
            [
                self.pdftoppm,
                "-f", "1",
                "-l", str(max(1, min(max_pages, MAX_PAGES))),
                "-png",
                "-r", "150",
                str(source),
                str(prefix),
            ]
        )
        pages: list[OcrPage] = []
        warnings: list[str] = []
        images = sorted(self.work_dir.glob(f"{prefix.name}-*.png"))[:max_pages]
        for index, image in enumerate(images, start=1):
            tsv = self._run_tsv(image)
            text, confidence = _parse_tsv(tsv)
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
                warnings.append(f"page {index} OCR text was truncated")
            pages.append(
                OcrPage(
                    number=index,
                    text=text,
                    mean_confidence=confidence,
                    engine=self.name,
                )
            )
        if not pages:
            warnings.append("OCR produced no page images; the document may be unreadable")
        return OcrResult(
            pages=tuple(pages),
            tool=self.name,
            version="1.0",
            warnings=tuple(warnings),
        )

    def _run_tsv(self, image: Path) -> str:
        if self.runner is not None:
            # Injected runners return bytes or str via a captured side channel.
            result = self.runner(
                [self.tesseract, str(image), "stdout", "tsv"]
            )
            return result.decode("utf-8", errors="replace") if isinstance(result, bytes) else str(result or "")
        completed = subprocess.run(
            [self.tesseract, str(image), "stdout", "tsv"],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return completed.stdout.decode("utf-8", errors="replace")


def _parse_tsv(tsv: str) -> tuple[str, float]:
    lines = [line for line in tsv.splitlines() if line.strip()]
    if not lines:
        return "", 0.0
    header = lines[0].split("\t")
    try:
        text_index = header.index("text")
        conf_index = header.index("conf")
    except ValueError:
        return "", 0.0
    words: list[str] = []
    confidences: list[float] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) <= max(text_index, conf_index):
            continue
        word = fields[text_index].strip()
        if not word:
            continue
        words.append(word)
        try:
            confidences.append(float(fields[conf_index]))
        except ValueError:
            continue
    text = " ".join(words)
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return text, round(confidence, 3)


__all__ = [
    "OcrPage",
    "OcrResult",
    "OcrAdapter",
    "UnavailableOcrAdapter",
    "TesseractOcrAdapter",
]

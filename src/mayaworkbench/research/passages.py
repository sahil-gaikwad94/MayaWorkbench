"""Structure-aware passage segmentation.

Every passage is a contiguous slice of the stored extracted text. ``char_start``
and ``char_end`` therefore double as verification anchors: re-reading the
artifact and normalising ``text[char_start:char_end]`` must reproduce the
passage's ``quote_hash``. Nothing is paraphrased, joined across a page boundary,
or reflowed.

Segmentation is intentionally conservative:

* Markdown structure (headings, fences, tables, lists, display maths) is read
  from explicit markers.
* Plain text and PDF text get heuristic headings and equations, and the caller
  records that structure detection was heuristic.
* Oversized blocks are split at sentence, then word, boundaries — never inside a
  word — and each chunk keeps an exact range in the source text.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field

from mayaworkbench.domain.enums import DocumentMediaKind, PassageKind

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_CAPTION_RE = re.compile(r"^(?:Figure|Fig\.|Table|Algorithm|Listing|Equation)\s+\d+", re.I)
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+([A-Z][^.]{2,90})$")
_CAPS_HEADING_RE = re.compile(r"^\s*([A-Z][A-Z0-9 ,&:()'\-/]{2,70})\s*$")
_EQUATION_LABEL_RE = re.compile(r"\\label\{([^}]{1,80})\}")
_EQUATION_NUMBER_RE = re.compile(r"^\s*\((\d+(?:\.\d+)*)\)")
_EQUATION_TEXT_RE = re.compile(r"\bEq(?:uation)?\.?\s*\(?(\d+(?:\.\d+)*)\)?", re.I)
_MATH_CHARS = set("=+±×÷∑∏√∫∂∇≈≤≥≠∞∈∉⊂⊆∪∩→←↔αβγδεθλμσϕω")
_MD_INLINE_RE = re.compile(r"[*_`]+")

_REFERENCE_SECTIONS = re.compile(
    r"(?i)\b(references|bibliography|acknowledg(e)?ments?|appendix|appendices)\b"
)


@dataclass
class ChunkedPassage:
    kind: PassageKind
    text: str
    char_start: int
    char_end: int
    page: int | None = None
    page_end: int | None = None
    section_path: str = ""
    section_title: str | None = None
    heading_level: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    equation_label: str | None = None
    chunk_index: int = 0
    ordinal: int = 0

    @property
    def is_reference_section(self) -> bool:
        return is_reference_section(self.section_title)


def is_reference_section(section: str | None) -> bool:
    """True for bibliography/acknowledgement/appendix-style sections.

    Claim extraction skips these: a citation or acknowledgement sentence is not
    evidence of a method, and treating it as one would fabricate claims.
    """
    return bool(section and _REFERENCE_SECTIONS.search(section))


@dataclass
class SegmentationResult:
    passages: list[ChunkedPassage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    section_count: int = 0
    page_count: int | None = None


def normalize_quote(value: str) -> str:
    """Normalise whitespace for quote comparison without changing wording."""
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\u00ad", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def quote_hash(value: str) -> str:
    return hashlib.sha256(normalize_quote(value).encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _Layout:
    """Precomputed offsets for one extracted text."""

    text: str
    lines: list[str]
    starts: list[int]
    ends: list[int]
    page_starts: list[int]
    page_ends: list[int]

    @classmethod
    def build(cls, text: str) -> "_Layout":
        lines = text.split("\n")
        starts: list[int] = []
        ends: list[int] = []
        offset = 0
        for line in lines:
            starts.append(offset)
            offset += len(line)
            ends.append(offset)
            offset += 1
        page_starts: list[int] = []
        page_ends: list[int] = []
        cursor = 0
        for part in text.split("\f"):
            page_starts.append(cursor)
            cursor += len(part)
            page_ends.append(cursor)
            cursor += 1
        return cls(
            text=text,
            lines=lines,
            starts=starts,
            ends=ends,
            page_starts=page_starts,
            page_ends=page_ends,
        )

    @property
    def page_count(self) -> int:
        return len(self.page_starts)

    def page_for(self, offset: int) -> int | None:
        if self.page_count <= 1:
            return None
        index = bisect_right(self.page_starts, offset) - 1
        return max(1, min(index + 1, self.page_count))

    def line_for(self, offset: int) -> int:
        return max(1, bisect_right(self.starts, offset))

    def separate_pages(self) -> list[str]:
        return self.text.split("\f")


def segment_document(
    text: str,
    media_kind: DocumentMediaKind,
    *,
    max_passage_chars: int = 4000,
    min_passage_chars: int = 24,
    body_start: int = 0,
    max_passages: int = 50_000,
) -> SegmentationResult:
    if not text.strip():
        return SegmentationResult(warnings=["DOCUMENT_HAS_NO_TEXT"])
    layout = _Layout.build(text)
    if media_kind == DocumentMediaKind.MARKDOWN:
        result = _segment_markdown(
            layout,
            max_chars=max_passage_chars,
            min_chars=min_passage_chars,
            body_start=body_start,
        )
    else:
        result = _segment_flat(layout, max_chars=max_passage_chars, min_chars=min_passage_chars)
        if media_kind == DocumentMediaKind.PDF:
            result.warnings.append(
                "PDF structure (headings, equations, captions) is detected heuristically from "
                "the extracted text layer."
            )
            lines = layout.lines
            hyphenated = sum(1 for line in lines if line.rstrip().endswith("-"))
            if hyphenated > max(5, len(lines) // 40):
                result.warnings.append(
                    f"{hyphenated} extracted lines end with a hyphen; words split across lines "
                    "are preserved verbatim and may not match in-text spelling."
                )
    if len(result.passages) > max_passages:
        dropped = len(result.passages) - max_passages
        result.passages = result.passages[:max_passages]
        result.warnings.append(
            f"PASSAGE_LIMIT_REACHED: {dropped} passage(s) were not stored. "
            "Raise research.max_passages_per_document or split the document."
        )
    for ordinal, passage in enumerate(result.passages):
        passage.ordinal = ordinal
    return result


# --------------------------------------------------------------------- markdown
def _segment_markdown(
    layout: _Layout, *, max_chars: int, min_chars: int, body_start: int
) -> SegmentationResult:
    lines = layout.lines
    result = SegmentationResult()
    stack: list[str] = []
    index = bisect_right(layout.starts, body_start) - 1 if body_start else 0
    index = max(0, min(index, len(lines)))
    blocks: list[ChunkedPassage] = []

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            closing = _find_fence_close(lines, index + 1, fence.group(1))
            end = closing if closing is not None else len(lines) - 1
            if closing is None:
                result.warnings.append("UNCLOSED_CODE_FENCE")
            blocks.extend(
                _block_passages(
                    layout,
                    index,
                    end,
                    kind=PassageKind.CODE,
                    section_path=" > ".join(stack),
                    section_title=stack[-1] if stack else None,
                    max_chars=max_chars,
                )
            )
            index = end + 1
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = _MD_INLINE_RE.sub("", heading.group(2)).strip()
            stack = stack[: level - 1] + [title]
            blocks.extend(
                _block_passages(
                    layout,
                    index,
                    index,
                    kind=PassageKind.HEADING,
                    section_path=" > ".join(stack),
                    section_title=title,
                    max_chars=max_chars,
                    heading_level=level,
                )
            )
            result.section_count += 1
            index += 1
            continue

        end = index
        while end + 1 < len(lines):
            following = lines[end + 1]
            if (
                not following.strip()
                or _FENCE_RE.match(following)
                or _MD_HEADING_RE.match(following)
            ):
                break
            end += 1
        block_text = "\n".join(lines[index : end + 1])
        blocks.extend(
            _block_passages(
                layout,
                index,
                end,
                kind=_classify_block(block_text, markdown=True),
                section_path=" > ".join(stack),
                section_title=stack[-1] if stack else None,
                max_chars=max_chars,
            )
        )
        index = end + 1

    result.passages = _merge_small_paragraphs(blocks, layout=layout, min_chars=min_chars)
    if not result.passages:
        result.warnings.append("DOCUMENT_HAS_NO_TEXT")
    return result


def _find_fence_close(lines: list[str], start: int, fence: str) -> int | None:
    for index in range(start, len(lines)):
        if lines[index].strip().startswith(fence):
            return index
    return None


# ------------------------------------------------------------------------- flat
def _segment_flat(layout: _Layout, *, max_chars: int, min_chars: int) -> SegmentationResult:
    result = SegmentationResult()
    if layout.page_count > 1:
        result.page_count = layout.page_count
    lines = layout.lines
    stack: list[str] = []
    blocks: list[ChunkedPassage] = []

    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or not stripped.strip("\f"):
            index += 1
            continue
        end = index
        while end + 1 < len(lines):
            following = lines[end + 1].strip()
            if not following or not following.strip("\f"):
                break
            end += 1
        block_text = "\n".join(lines[index : end + 1])
        heading = _detect_heading(lines, index, end)
        if heading is not None:
            level, title = heading
            stack = stack[: level - 1] + [title]
            blocks.extend(
                _block_passages(
                    layout,
                    index,
                    end,
                    kind=PassageKind.HEADING,
                    section_path=" > ".join(stack),
                    section_title=title,
                    max_chars=max_chars,
                    heading_level=level,
                )
            )
            result.section_count += 1
            index = end + 1
            continue
        blocks.extend(
            _block_passages(
                layout,
                index,
                end,
                kind=_classify_block(block_text, markdown=False),
                section_path=" > ".join(stack),
                section_title=stack[-1] if stack else None,
                max_chars=max_chars,
            )
        )
        index = end + 1

    result.passages = _merge_small_paragraphs(blocks, layout=layout, min_chars=min_chars)
    if not result.passages and layout.text.strip():
        result.warnings.append("DOCUMENT_HAS_NO_TEXT")
    return result


# ---------------------------------------------------------------------- blocks
def _block_passages(
    layout: _Layout,
    first_line: int,
    last_line: int,
    *,
    kind: PassageKind,
    section_path: str,
    section_title: str | None,
    max_chars: int,
    heading_level: int | None = None,
) -> list[ChunkedPassage]:
    start = layout.starts[first_line]
    end = min(layout.ends[last_line], len(layout.text))
    while end > start and layout.text[end - 1] in "\n\r":
        end -= 1
    if end <= start:
        return []
    equation_label = _equation_label(layout.text, start, end) if kind == PassageKind.EQUATION else None
    page = layout.page_for(start)
    page_end = layout.page_for(max(start, end - 1))
    passages: list[ChunkedPassage] = []
    for chunk_index, (chunk_start, chunk_end) in enumerate(
        _split_range(layout.text, start, end, max_chars)
    ):
        body = layout.text[chunk_start:chunk_end]
        if not body.strip():
            continue
        passages.append(
            ChunkedPassage(
                kind=kind,
                text=body,
                char_start=chunk_start,
                char_end=chunk_end,
                page=page,
                page_end=page_end,
                section_path=section_path,
                section_title=section_title,
                heading_level=heading_level,
                line_start=layout.line_for(chunk_start),
                line_end=layout.line_for(max(chunk_start, chunk_end - 1)),
                equation_label=equation_label,
                chunk_index=chunk_index,
            )
        )
    return passages


def _split_range(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    if max_chars <= 0 or end - start <= max_chars:
        return [(start, end)]
    ranges: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_chars:
        window_end = cursor + max_chars
        cut = _sentence_break(text, cursor, window_end)
        if cut is None:
            cut = text.rfind(" ", cursor + max_chars // 2, window_end)
        if cut is None or cut <= cursor:
            cut = window_end
        ranges.append((cursor, cut))
        cursor = cut
        while cursor < end and text[cursor] in " \n\t\r":
            cursor += 1
    if cursor < end:
        ranges.append((cursor, end))
    return ranges


def _sentence_break(text: str, start: int, end: int) -> int | None:
    window = text[start:end]
    last = None
    for match in re.finditer(r"[.!?](?=\s)", window):
        last = start + match.end()
    if last is not None and last > start + (end - start) // 3:
        return last
    return None


def _merge_small_paragraphs(
    blocks: list[ChunkedPassage], *, layout: _Layout, min_chars: int
) -> list[ChunkedPassage]:
    merged: list[ChunkedPassage] = []
    for block in blocks:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and block.kind == PassageKind.PARAGRAPH
            and previous.kind == PassageKind.PARAGRAPH
            and previous.section_path == block.section_path
            and len(previous.text) < min_chars
            and previous.chunk_index == 0
            and block.chunk_index == 0
            and block.char_start >= previous.char_end
        ):
            previous.text = layout.text[previous.char_start : block.char_end]
            previous.char_end = block.char_end
            previous.line_end = block.line_end
            previous.page_end = block.page_end
            continue
        merged.append(block)
    return [block for block in merged if block.text.strip()]


def _classify_block(block: str, *, markdown: bool) -> PassageKind:
    stripped = block.strip()
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return PassageKind.PARAGRAPH
    if markdown:
        table_lines = [line for line in lines if "|" in line]
        if len(lines) >= 2 and len(table_lines) == len(lines):
            return PassageKind.TABLE
        if len(lines) >= 2 and _TABLE_SEPARATOR_RE.match(lines[1]) and "|" in lines[0]:
            return PassageKind.TABLE
    list_lines = [line for line in lines if _LIST_RE.match(line)]
    if len(list_lines) == len(lines) and len(lines) >= 2:
        return PassageKind.LIST
    if _CAPTION_RE.match(stripped):
        return PassageKind.CAPTION
    if _looks_like_equation(stripped):
        return PassageKind.EQUATION
    return PassageKind.PARAGRAPH


def _looks_like_equation(block: str) -> bool:
    stripped = block.strip()
    if not stripped or len(stripped) > 800:
        return False
    if stripped.startswith("$$") or stripped.startswith("\\[") or stripped.startswith("\\begin{"):
        return True
    if "\\begin{" in stripped and "\\end{" in stripped:
        return True
    if _EQUATION_NUMBER_RE.match(stripped):
        return True
    if not any(char in _MATH_CHARS for char in stripped):
        return False
    if stripped.endswith((".", ":", "?", "。")):
        return False
    non_space = sum(1 for char in stripped if not char.isspace())
    letters = sum(1 for char in stripped if char.isalpha())
    if non_space == 0:
        return False
    return letters / non_space < 0.6


def _equation_label(text: str, start: int, end: int) -> str | None:
    excerpt = text[start:end]
    label = _EQUATION_LABEL_RE.search(excerpt)
    if label:
        return label.group(1)
    numbered = _EQUATION_NUMBER_RE.match(excerpt.strip())
    if numbered:
        return f"Eq. {numbered.group(1)}"
    inline = _EQUATION_TEXT_RE.search(excerpt[:200])
    if inline:
        return f"Eq. {inline.group(1)}"
    return None


def _detect_heading(lines: list[str], first: int, last: int) -> tuple[int, str] | None:
    """Detect a heading from a single-line block in extracted flat text."""
    if first != last:
        return None
    line = lines[first].strip()
    if not line or len(line) > 120 or line.endswith("."):
        return None
    numbered = _NUMBERED_HEADING_RE.match(line)
    if numbered:
        return len(numbered.group(1).split(".")), _clean_heading(numbered.group(2))
    caps = _CAPS_HEADING_RE.match(line)
    if caps and not any(char.isdigit() for char in caps.group(1)):
        title = _clean_heading(caps.group(1))
        if title.isupper() and len(title.split()) <= 12:
            return 1, title.title()
    return None


def _clean_heading(value: str) -> str:
    text = _MD_INLINE_RE.sub("", value).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" :")


__all__ = [
    "ChunkedPassage",
    "SegmentationResult",
    "segment_document",
    "normalize_quote",
    "quote_hash",
    "text_sha256",
]

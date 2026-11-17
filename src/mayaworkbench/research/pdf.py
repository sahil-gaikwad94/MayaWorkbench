"""PDF text extraction with explicit capability limits.

Two extraction paths exist, in this order:

1. ``pypdf`` if it is installed (``pip install "mayaworkbench[pdf]"``). It
   handles font encodings and ``ToUnicode`` maps properly.
2. A conservative builtin parser for uncompressed and Flate-compressed content
   streams. It reads the PDF's text layer with a small content-stream
   interpreter and is honest about what it cannot do: scanned pages, embedded
   object streams with unsupported filters, and fonts without usable encodings
   produce warnings, and a page with no extractable text is reported as a
   missing page rather than guessed at.

Nothing here fabricates text. A PDF whose text layer cannot be read raises
:class:`ExtractionError` (or yields warnings plus empty pages) so the caller can
store a failed extraction instead of a plausible-looking lie.
"""

from __future__ import annotations

import io
import re
import zlib
from dataclasses import dataclass, field

from mayaworkbench.errors import ExtractionError

#: Marker prefixed to tool names so a memo can state exactly what produced its quotes.
BUILTIN_TOOL = "maya-pdf-text"
BUILTIN_VERSION = "1.0.0"

_OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.S)
_REF_RE = re.compile(rb"(\d+)\s+(\d+)\s+R")
_KIDS_RE = re.compile(rb"/Kids\s*\[(.*?)\]", re.S)
_PAGE_TYPE_RE = re.compile(rb"/Type\s*/Page(?![s\w])")
_PAGES_TYPE_RE = re.compile(rb"/Type\s*/Pages(?![s\w])")
_TYPE0_FONT_RE = re.compile(rb"/Subtype\s*/Type0")
_TO_UNICODE_RE = re.compile(rb"/ToUnicode")

#: Content operators that end a text-showing operation.
_SHOW_OPERATORS = {"Tj", "TJ", "'", '"'}
_NEWLINE_OPERATORS = {"T*"}


@dataclass
class PdfPage:
    number: int
    text: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class PdfExtraction:
    pages: list[PdfPage]
    tool: str
    version: str
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    encrypted: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page_text(self, number: int) -> str:
        for page in self.pages:
            if page.number == number:
                return page.text
        return ""


def extract_pdf_text(data: bytes) -> PdfExtraction:
    """Extract page text from PDF bytes.

    Raises :class:`ExtractionError` when the bytes are not a PDF or when every
    extraction path refuses the file (for example, an encrypted PDF).
    """
    if b"%PDF-" not in data[:1024]:
        raise ExtractionError(
            "The supplied file does not start with a PDF header",
            hint="Register it as a text or Markdown document, or check the file.",
        )
    pypdf = _load_pypdf()
    if pypdf is not None:
        return _extract_with_pypdf(pypdf, data)
    return _extract_builtin(data)


def _load_pypdf():  # type: ignore[no-untyped-def]
    try:  # pragma: no cover - exercised only when the optional extra is installed
        import pypdf  # type: ignore
    except ImportError:
        return None
    return pypdf


# --------------------------------------------------------------------------- pypdf
def _extract_with_pypdf(pypdf, data: bytes) -> PdfExtraction:  # type: ignore[no-untyped-def]
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - library raises many shapes
        raise ExtractionError(f"pypdf could not open this PDF: {exc}") from exc

    warnings: list[str] = []
    encrypted = bool(getattr(reader, "is_encrypted", False))
    if encrypted:
        try:
            reader.decrypt("")
            warnings.append(
                "The PDF is encrypted; extraction used the empty user password."
            )
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(
                "The PDF is encrypted and cannot be read without a password",
                hint="Decrypt the document first; MayaWorkbench never stores PDF passwords.",
            ) from exc

    pages: list[PdfPage] = []
    for index, page in enumerate(reader.pages, start=1):
        page_warnings: list[str] = []
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            text = ""
            page_warnings.append(f"Page {index} could not be extracted: {exc}")
        if not text.strip():
            page_warnings.append(
                f"Page {index} produced no text; it may be a scanned image or empty."
            )
        pages.append(PdfPage(index, clean_pdf_text(text), page_warnings))

    metadata: dict[str, str] = {}
    info = getattr(reader, "metadata", None)
    if info is not None:
        for source, target in (
            ("title", "title"),
            ("author", "author"),
            ("creation_date", "created"),
            ("subject", "subject"),
        ):
            try:
                value = info.get(source)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - metadata objects are inconsistent
                value = None
            if isinstance(value, str) and value.strip():
                metadata[target] = value.strip()

    return PdfExtraction(
        pages=pages,
        tool="pypdf",
        version=str(getattr(pypdf, "__version__", "unknown")),
        warnings=warnings,
        metadata=metadata,
        encrypted=encrypted,
    )


# ------------------------------------------------------------------------- builtin
def _extract_builtin(data: bytes) -> PdfExtraction:
    objects = _parse_objects(data)
    if not objects:
        raise ExtractionError(
            "No PDF objects could be parsed",
            hint="The file may be truncated or use an unsupported PDF structure.",
        )
    warnings: list[str] = []
    page_refs = _page_order(objects)
    if not page_refs:
        raise ExtractionError("No PDF pages were found in this file")

    # Document-level encoding signal: Type0 fonts without ToUnicode usually mean
    # glyph ids rather than characters, which the builtin decoder cannot map.
    if _TYPE0_FONT_RE.search(data) and not _TO_UNICODE_RE.search(data):
        warnings.append(
            "This PDF uses Type0 fonts without ToUnicode maps; extracted characters may be "
            "incomplete. Install the optional pypdf extra for better text extraction."
        )

    pages: list[PdfPage] = []
    seen_encodings: set[str] = set()
    for index, reference in enumerate(page_refs, start=1):
        body = objects.get(reference, b"")
        content = _page_content(body, objects)
        page_warnings: list[str] = []
        text = clean_pdf_text(_content_stream_to_text(content, page_warnings))
        if not text.strip():
            if b"/XObject" in body:
                page_warnings.append(
                    f"Page {index} has no text layer; it likely contains an image."
                )
            else:
                page_warnings.append(f"Page {index} produced no text.")
        for warning in page_warnings:
            if warning not in seen_encodings:
                seen_encodings.add(warning)
        pages.append(PdfPage(index, text, page_warnings))

    metadata = _document_metadata(data, objects)
    return PdfExtraction(
        pages=pages,
        tool=BUILTIN_TOOL,
        version=BUILTIN_VERSION,
        warnings=warnings,
        metadata=metadata,
    )


def _parse_objects(data: bytes) -> dict[int, bytes]:
    objects: dict[int, bytes] = {}
    for match in _OBJ_RE.finditer(data):
        objects[int(match.group(1))] = match.group(3)
    # PDF 1.5+ object streams: recover objects that only exist compressed.
    for body in list(objects.values()):
        if b"/ObjStm" not in body:
            continue
        payload = _stream_payload(body)
        if payload is None:
            continue
        try:
            count_text, first_text = _objstm_header(body)
            count = int(count_text)
            first = int(first_text)
        except (TypeError, ValueError):
            continue
        header = payload[:first].split()
        numbers = header[0::2]
        offsets = header[1::2]
        if len(numbers) < count or len(offsets) < count:
            continue
        for position in range(count):
            try:
                number = int(numbers[position])
                start = first + int(offsets[position])
            except ValueError:
                continue
            end = (
                first + int(offsets[position + 1])
                if position + 1 < count
                else len(payload)
            )
            objects.setdefault(number, payload[start:end])
    return objects


def _objstm_header(body: bytes) -> tuple[bytes, bytes]:
    dict_end = body.find(b"stream")
    header = body[: dict_end if dict_end > 0 else len(body)]
    count_match = re.search(rb"/N\s+(\d+)", header)
    first_match = re.search(rb"/First\s+(\d+)", header)
    if not count_match or not first_match:
        raise ValueError("object stream header missing /N or /First")
    return count_match.group(1), first_match.group(1)


def _stream_payload(body: bytes) -> bytes | None:
    stream = _STREAM_RE.search(body)
    if stream is None:
        return None
    dictionary = body[: stream.start()]
    payload = stream.group(1)
    filters = re.findall(rb"/(\w*Decode)\b", dictionary)
    for name in filters:
        if name != b"FlateDecode":
            return None
        try:
            payload = zlib.decompress(payload)
        except zlib.error:
            try:
                payload = zlib.decompressobj().decompress(payload)
            except zlib.error:
                return None
    return payload


def _resolve(value: bytes, objects: dict[int, bytes], depth: int = 0) -> bytes | None:
    match = _REF_RE.fullmatch(value.strip())
    if match and depth < 4:
        return objects.get(int(match.group(1)))
    return None


def _page_order(objects: dict[int, bytes]) -> list[int]:
    root: int | None = None
    for number, body in objects.items():
        if b"/Type" in body and re.search(rb"/Type\s*/Catalog", body):
            pages = re.search(rb"/Pages\s+(\d+)\s+0\s+R", body)
            if pages:
                root = int(pages.group(1))
                break
    order: list[int] = []
    if root is not None:
        _walk_pages(root, objects, order, set())
    if order:
        return order
    return [number for number in sorted(objects) if _PAGE_TYPE_RE.search(objects[number])]


def _walk_pages(number: int, objects: dict[int, bytes], order: list[int], seen: set[int]) -> None:
    if number in seen:
        return
    seen.add(number)
    body = objects.get(number)
    if body is None:
        return
    if _PAGE_TYPE_RE.search(body):
        order.append(number)
        return
    kids = _KIDS_RE.search(body)
    if kids is None:
        return
    for match in _REF_RE.finditer(kids.group(1)):
        _walk_pages(int(match.group(1)), objects, order, seen)


def _page_content(body: bytes, objects: dict[int, bytes]) -> bytes:
    direct = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
    content = b""
    if direct is not None:
        for match in _REF_RE.finditer(direct.group(1)):
            payload = _stream_payload(objects.get(int(match.group(1)), b""))
            if payload:
                content += payload + b"\n"
        return content
    single = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
    if single is not None:
        return _stream_payload(objects.get(int(single.group(1)), b"")) or b""
    return content


def _document_metadata(data: bytes, objects: dict[int, bytes]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    info = re.search(rb"/Info\s+(\d+)\s+0\s+R", data)
    if info is None:
        return metadata
    body = objects.get(int(info.group(1)), b"")
    for key, target in ((b"Title", "title"), (b"Author", "author"), (b"CreationDate", "created")):
        match = re.search(rb"/" + key + rb"\s*(\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)", body)
        if match is None:
            continue
        text = _decode_pdf_string(match.group(1))
        if text.strip():
            metadata[target] = text.strip()
    return metadata


# ------------------------------------------------------------- content stream
def _content_stream_to_text(content: bytes, warnings: list[str]) -> str:
    tokens = _tokenize_content(content)
    output: list[str] = []
    operands: list[object] = []
    last_y: float | None = None
    replacement_ratio = 0
    characters = 0

    def emit(text: str) -> None:
        nonlocal characters, replacement_ratio
        if not text:
            return
        characters += len(text)
        replacement_ratio += text.count("\ufffd")
        output.append(text)

    def newline() -> None:
        if output and not output[-1].endswith("\n"):
            output.append("\n")

    for kind, value in tokens:
        if kind == "string":
            operands.append(value)
            continue
        if kind == "number":
            operands.append(value)
            continue
        if kind == "array":
            operands.append(value)
            continue
        if kind != "operator":
            continue
        operator = value
        if operator == "Tj" and operands:
            emit(str(operands[-1]))
        elif operator == "TJ" and operands and isinstance(operands[-1], list):
            for part in operands[-1]:
                if isinstance(part, str):
                    emit(part)
                elif isinstance(part, (int, float)) and part <= -120:
                    emit(" ")
        elif operator in ("'", '"') and operands:
            newline()
            emit(str(operands[-1]))
        elif operator in _NEWLINE_OPERATORS:
            newline()
        elif operator in ("Td", "TD") and len(operands) >= 2:
            try:
                ty = float(operands[-1])
            except (TypeError, ValueError):
                ty = 0.0
            if abs(ty) > 0.5:
                newline()
        elif operator == "Tm" and len(operands) >= 6:
            try:
                y = float(operands[-1])
            except (TypeError, ValueError):
                y = None
            if y is not None and last_y is not None and abs(y - last_y) > 0.5:
                newline()
            last_y = y
        elif operator == "ET":
            newline()
        elif operator == "BT":
            last_y = None
        operands = []

    text = "".join(output)
    if characters > 40 and replacement_ratio / characters > 0.05:
        warnings.append(
            "Extracted text contains many unreadable characters; the PDF font encoding is "
            "probably unsupported by the builtin extractor."
        )
    return text


def _tokenize_content(content: bytes) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    index = 0
    length = len(content)
    array_stack: list[list[object]] = []

    def push(value: object) -> None:
        if array_stack:
            array_stack[-1].append(value)
        else:
            tokens.append(("value", value))

    while index < length:
        byte = content[index : index + 1]
        if byte in b" \t\r\n\f\x00":
            index += 1
            continue
        if byte == b"%":
            newline = content.find(b"\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if byte == b"(":
            text, index = _read_literal_string(content, index + 1)
            push(text)
            continue
        if byte == b"<" and content[index : index + 2] != b"<<":
            end = content.find(b">", index)
            if end < 0:
                break
            raw = re.sub(rb"\s", b"", content[index + 1 : end])
            push(_decode_hex_string(raw))
            index = end + 1
            continue
        if bytes(content[index : index + 2]) == b"<<":
            depth = 0
            start = index
            while index < length:
                if content[index : index + 2] == b"<<":
                    depth += 1
                    index += 2
                    continue
                if content[index : index + 2] == b">>":
                    depth -= 1
                    index += 2
                    if depth == 0:
                        break
                    continue
                index += 1
            push(content[start:index])
            continue
        if byte == b"[":
            array_stack.append([])
            index += 1
            continue
        if byte == b"]":
            if array_stack:
                completed = array_stack.pop()
                push(completed)
            index += 1
            continue
        if byte == b"/":
            start = index + 1
            index = start
            while index < length and content[index : index + 1] not in b" \t\r\n\f/[]<>()%":
                index += 1
            push("/" + content[start:index].decode("latin-1"))
            continue
        if byte in b"+-.0123456789":
            match = re.match(rb"[+-]?(?:\d+\.?\d*|\.\d+)", content[index:])
            if match:
                raw = match.group(0)
                push(float(raw))
                index += len(raw)
                continue
        match = re.match(rb"[A-Za-z'\"*]+", content[index:])
        if match:
            operator = match.group(0).decode("latin-1")
            index += len(operator)
            if array_stack:
                array_stack[-1].append(operator)
            else:
                tokens.append(("operator", operator))
            continue
        index += 1

    resolved: list[tuple[str, object]] = []
    for kind, value in tokens:
        if kind == "operator":
            resolved.append((kind, value))
        elif isinstance(value, str):
            resolved.append(("string", value))
        elif isinstance(value, (int, float)):
            resolved.append(("number", value))
        elif isinstance(value, list):
            resolved.append(("array", value))
        else:
            resolved.append(("other", value))
    return resolved


def _read_literal_string(content: bytes, index: int) -> tuple[str, int]:
    out = bytearray()
    depth = 1
    length = len(content)
    while index < length:
        byte = content[index]
        if byte == 0x5C:  # backslash
            index += 1
            if index >= length:
                break
            escaped = content[index]
            mapping = {
                ord("n"): b"\n",
                ord("r"): b"\r",
                ord("t"): b"\t",
                ord("b"): b"\b",
                ord("f"): b"\f",
                ord("("): b"(",
                ord(")"): b")",
                ord("\\"): b"\\",
            }
            if escaped in mapping:
                out += mapping[escaped]
                index += 1
                continue
            if 0x30 <= escaped <= 0x37:
                digits = content[index : index + 3]
                octal = re.match(rb"[0-7]{1,3}", digits)
                if octal:
                    out.append(int(octal.group(0), 8) & 0xFF)
                    index += len(octal.group(0))
                    continue
            index += 1
            continue
        if byte == 0x28:
            depth += 1
        elif byte == 0x29:
            depth -= 1
            if depth == 0:
                index += 1
                break
        out.append(byte)
        index += 1
    return _decode_pdf_string_bytes(bytes(out)), index


def _decode_hex_string(raw: bytes) -> str:
    if len(raw) % 2:
        raw += b"0"
    try:
        data = bytes.fromhex(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if len(data) >= 2 and data[0] == 0xFE and data[1] == 0xFF:
        return data[2:].decode("utf-16-be", errors="replace")
    if len(data) % 2 == 0:
        candidate = data.decode("utf-16-be", errors="replace")
        if candidate and sum(char.isprintable() for char in candidate) / len(candidate) > 0.85:
            return candidate
    return _decode_pdf_string_bytes(data)


def _decode_pdf_string(token: bytes) -> str:
    if token.startswith(b"<"):
        return _decode_hex_string(re.sub(rb"\s", b"", token[1:-1]))
    text, _index = _read_literal_string(token[1:], 0)
    return text


def _decode_pdf_string_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def clean_pdf_text(text: str) -> str:
    """Normalise extractor output without changing wording.

    Form feeds are preserved because the segmenter uses them as page separators.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00ad", "")
    text = "".join(
        char for char in text if char in "\n\t\f" or (char.isprintable() or char == "\ufffd")
    )
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n")


__all__ = ["PdfPage", "PdfExtraction", "extract_pdf_text", "clean_pdf_text"]

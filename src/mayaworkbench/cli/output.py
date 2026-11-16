"""CLI output.

Human output is optimised for supervision: compact blocks, aligned tables, no
decorative animation. ``--json`` mode prints exactly one JSON document and no
human text, so the CLI is safe to script.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Iterable, Sequence


class Console:
    def __init__(
        self,
        *,
        json_mode: bool = False,
        color: bool | None = None,
        width: int | None = None,
        stream: Any = None,
    ):
        self.json_mode = json_mode
        self.stream = stream or sys.stdout
        if color is None:
            color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.color = color
        self.width = width or shutil.get_terminal_size((100, 24)).columns

    # ------------------------------------------------------------------ basics
    def print(self, text: str = "") -> None:
        if not self.json_mode:
            print(text, file=self.stream)

    def json(self, payload: Any) -> None:
        print(json.dumps(payload, indent=2, sort_keys=False, default=str), file=self.stream)

    def error(
        self, message: str, *, hint: str | None = None, code: str | None = None
    ) -> None:
        if self.json_mode:
            payload: dict[str, object] = {"error": str(message), "hint": hint}
            if code:
                payload["code"] = code
            print(json.dumps(payload, indent=2), file=sys.stderr)
            return
        prefix = self._paint("error:", "31") if self.color else "error:"
        print(f"maya {prefix} {message}", file=sys.stderr)
        if hint:
            print(f"  hint: {hint}", file=sys.stderr)

    def warning(self, message: str) -> None:
        if self.json_mode:
            return
        prefix = self._paint("warning:", "33") if self.color else "warning:"
        print(f"maya {prefix} {message}", file=sys.stderr)

    def note(self, message: str) -> None:
        if self.json_mode:
            return
        print(self._paint(message, "2"), file=self.stream)

    # --------------------------------------------------------------- structure
    def heading(self, text: str) -> None:
        if self.json_mode:
            return
        print(self._paint(text, "1"), file=self.stream)

    def section(self, text: str) -> None:
        if self.json_mode:
            return
        print("", file=self.stream)
        print(self._paint(text, "1;36"), file=self.stream)

    def rule(self, char: str = "─") -> None:
        if self.json_mode:
            return
        print(char * min(self.width, 100), file=self.stream)

    def kv(self, pairs: Sequence[tuple[str, Any]], *, indent: int = 0) -> None:
        if self.json_mode:
            return
        key_width = max((len(key) for key, _ in pairs), default=0)
        pad = " " * indent
        for key, value in pairs:
            rendered = "" if value is None else str(value)
            print(f"{pad}{key.ljust(key_width)}  {rendered}", file=self.stream)

    def bullet(self, text: str, *, indent: int = 0) -> None:
        if self.json_mode:
            return
        print(f"{' ' * indent}- {text}", file=self.stream)

    def table(
        self,
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        max_column: int = 70,
    ) -> None:
        if self.json_mode:
            return
        rendered = [[_truncate(str(cell), max_column) for cell in row] for row in rows]
        if not rendered:
            print(self._paint("(none)", "2"), file=self.stream)
            return
        widths = [len(header) for header in headers]
        for row in rendered:
            for index, cell in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], len(cell))
        total = sum(widths) + 2 * (len(widths) - 1)
        if total > self.width and widths:
            overflow = total - self.width
            widest = max(range(len(widths)), key=lambda index: widths[index])
            widths[widest] = max(12, widths[widest] - overflow)
        header_line = "  ".join(
            self._paint(header.ljust(widths[index]), "1") for index, header in enumerate(headers)
        )
        print(header_line, file=self.stream)
        print(self._paint("─" * min(sum(widths) + 2 * (len(widths) - 1), self.width), "2"), file=self.stream)
        for row in rendered:
            cells = []
            for index, cell in enumerate(row[: len(widths)]):
                cells.append(cell.ljust(widths[index]))
            print("  ".join(cells).rstrip(), file=self.stream)

    def code(self, text: str, *, indent: int = 2) -> None:
        if self.json_mode:
            return
        pad = " " * indent
        for line in text.splitlines() or [""]:
            print(f"{pad}{line}", file=self.stream)

    # ------------------------------------------------------------------ paint
    def _paint(self, text: str, code: str) -> str:
        if not self.color:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").replace("\t", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def format_bytes(count: int | None) -> str:
    if count is None:
        return "?"
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover


def format_duration(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "?"
    seconds = milliseconds / 1000
    if seconds < 1:
        return f"{milliseconds} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def format_ratio(part: int, total: int) -> str:
    if total <= 0:
        return "0"
    return f"{part}/{total} ({part / total:.0%})"


__all__ = ["Console", "format_bytes", "format_duration", "format_ratio"]

"""Fixture service with hand-labelled symbols."""
import re
from pathlib import Path

def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

def count_tokens(text: str) -> int:
    return len(normalize(text).split())

def build_context(path: Path) -> str:
    return path.read_text(encoding="utf-8")

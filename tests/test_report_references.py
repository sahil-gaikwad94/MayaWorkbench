"""Release guard: phase reports may only cite test files that exist (MWB-011)."""

from __future__ import annotations

import re
from pathlib import Path

REPORTS = (
    Path("docs/phase-2-report.md"),
    Path("docs/phase-3-report.md"),
)


def test_reports_only_reference_present_test_files():
    missing: list[str] = []
    for report in REPORTS:
        text = report.read_text(encoding="utf-8")
        for name in sorted(set(re.findall(r"test_[a-z0-9_]+\.py", text))):
            if not (Path("tests") / name).is_file():
                missing.append(f"{report}: {name}")
    assert not missing, "Phase reports cite absent test files: " + ", ".join(missing)

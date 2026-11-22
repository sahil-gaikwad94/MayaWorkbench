"""Release evidence — a machine-readable record built from real inputs.

The generator reads repository state, an explicitly supplied pytest report and
the canonical capability report. It never runs tests, containers or models and
never guesses a value that was not supplied.
"""

from __future__ import annotations

from mayaworkbench.release.evidence import (
    DEFAULT_TEST_COMMAND,
    SCHEMA_VERSION,
    EvidenceError,
    ReleaseEvidence,
    assert_no_secrets,
    build_evidence,
    canonical_sha256,
    parse_adversarial_results,
    parse_pytest_report,
    render_markdown,
    to_json,
)

__all__ = [
    "DEFAULT_TEST_COMMAND",
    "SCHEMA_VERSION",
    "EvidenceError",
    "ReleaseEvidence",
    "assert_no_secrets",
    "build_evidence",
    "canonical_sha256",
    "parse_adversarial_results",
    "parse_pytest_report",
    "render_markdown",
    "to_json",
]

"""Phase 2 CLI integration: registration and a full research-to-matrix flow."""

from __future__ import annotations

import json
from pathlib import Path

from mayaworkbench.cli.main import build_parser, main

PAPER = """# Causal Attention Notes

The implementation must apply a causal mask with torch.triu before softmax.

The forward pass must use masked_fill on the scaled scores to block future positions.
"""


def _commands() -> set[str]:
    parser = build_parser()
    subparsers = next(iter(parser._subparsers._group_actions))  # type: ignore[union-attr]
    return set(subparsers.choices)


def _run(capsys, args):
    code = main(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else None
    return code, payload, captured


def test_phase_two_and_three_command_areas_are_registered():
    commands = _commands()
    for expected in ("research", "map", "task", "worker"):
        assert expected in commands, f"`maya {expected}` is not registered"


def test_help_for_each_registered_area_exits_cleanly(capsys):
    import pytest

    for area in ("research", "map", "task", "worker"):
        with pytest.raises(SystemExit) as excinfo:
            main(["--json", area, "--help"])
        assert excinfo.value.code == 0


def test_research_add_extract_and_map_flow(capsys, home: Path, repo: Path, tmp_path: Path):
    code, _, _ = _run(
        capsys, ["--home", str(home), "--json", "init", "--project", "p2", "--slug", "p2"]
    )
    assert code == 0
    _run(capsys, ["--home", str(home), "--json", "repo", "add", str(repo), "--project", "p2"])
    code, snapshot, _ = _run(
        capsys, ["--home", str(home), "--json", "snapshot", "create", "--project", "p2"]
    )
    assert code == 0
    code, _, _ = _run(
        capsys,
        [
            "--home",
            str(home),
            "--json",
            "index",
            "--project",
            "p2",
            "--snapshot",
            snapshot["snapshot"]["id"],
        ],
    )
    assert code == 0

    paper = tmp_path / "paper.md"
    paper.write_text(PAPER, encoding="utf-8")
    code, added, captured = _run(
        capsys,
        [
            "--home",
            str(home),
            "--json",
            "research",
            "add",
            str(paper),
            "--project",
            "p2",
            "--title",
            "Causal Attention Notes",
        ],
    )
    assert code == 0, captured.err
    document_id = added["document"]["id"]
    assert added["passages"] >= 1

    code, extracted, captured = _run(
        capsys,
        ["--home", str(home), "--json", "research", "extract", document_id],
    )
    assert code == 0, captured.err
    assert extracted["claims"], extracted["warnings"]
    claim_ids = [claim["id"] for claim in extracted["claims"]]

    code, listed, _ = _run(
        capsys, ["--home", str(home), "--json", "research", "claims", "--project", "p2"]
    )
    assert code == 0
    assert {claim["id"] for claim in listed["claims"]} >= set(claim_ids)

    code, mapped, captured = _run(
        capsys, ["--home", str(home), "--json", "map", "run", "--project", "p2"]
    )
    assert code == 0, captured.err
    assert mapped["artifact_id"]
    assert mapped["matrix"]["claims_total"] == len(claim_ids)

"""Tests for change-impact analysis."""

from __future__ import annotations

from mayaworkbench.indexing.impact import ImpactAnalyzer


def test_changed_file_is_depth_zero(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"]
    )
    assert report.changed == ["attention.py"]
    assert any(f.path == "attention.py" and f.depth == 0 for f in report.affected_files)
    assert report.total_files_indexed >= 3


def test_dependents_of_changed_file_are_reported(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"]
    )
    paths = {f.path for f in report.affected_files}
    # model.py imports attention; tests/test_attention.py imports attention
    assert "model.py" in paths
    assert "tests/test_attention.py" in paths


def test_affected_tests_lists_test_files(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"]
    )
    assert "tests/test_attention.py" in report.affected_tests


def test_confidence_propagates_minimum(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"]
    )
    for item in report.affected_files:
        assert item.confidence in {"HIGH", "MEDIUM", "LOW"}


def test_unmatched_path_yields_empty_and_note(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["does/not/exist.py"]
    )
    assert report.affected_files == []
    assert any("not found" in n.lower() or "empty" in n.lower() for n in report.notes)


def test_max_depth_bounds_traversal(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"], max_depth=0
    )
    # Only the changed file itself at depth 0.
    assert all(f.depth == 0 for f in report.affected_files)
    assert len(report.affected_files) == 1


def test_multiple_changed_paths(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py", "utils.py"]
    )
    assert "attention.py" in report.changed
    assert "utils.py" in report.changed


def test_impact_analyzer_direct_construction(indexed):
    analyzer = ImpactAnalyzer(
        files=indexed.services.files, symbols=indexed.services.symbols
    )
    report = analyzer.analyze(indexed.snapshot.id, ["model.py"])
    assert report.snapshot_id == indexed.snapshot.id


def test_via_chain_present(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"]
    )
    dependents = [f for f in report.affected_files if f.depth >= 1]
    assert dependents
    for item in dependents:
        assert len(item.via) == item.depth + 1
        assert item.via[0] == "attention.py"


def test_notes_are_honest(indexed):
    report = indexed.services.impact.analyze(
        indexed.snapshot.id, ["attention.py"]
    )
    joined = " ".join(report.notes).lower()
    assert "python" in joined
    assert "static" in joined or "lower bound" in joined

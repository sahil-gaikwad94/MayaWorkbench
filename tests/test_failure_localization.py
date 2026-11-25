"""Automatic failure localisation: unstructured check output -> concrete locations."""

from mayaworkbench.tasks.failure_localization import (
    LocalizedFailure,
    localize_failures,
)
from mayaworkbench.tasks.failure_localisation import localize_failures as localize_br


def test_python_traceback_is_high_confidence():
    text = '''Traceback (most recent call last):
  File "src/model.py", line 42, in forward
    raise ValueError("bad")
ValueError: bad
'''
    result = localize_failures(text)
    assert result
    assert result[0].path == "src/model.py"
    assert result[0].line == 42
    assert result[0].function == "forward"
    assert result[0].confidence == "high"
    assert result[0].source == "traceback"


def test_pytest_failing_id_maps_to_file():
    text = "FAILED tests/test_model.py::test_forward_shape - AssertionError\n"
    result = localize_failures(text)
    assert any(item.path == "tests/test_model.py" and item.test == "test_forward_shape" for item in result)


def test_cargo_location():
    text = "error[E0308]: mismatched types\n --> src/lib.rs:12:5\n"
    result = localize_failures(text)
    assert result[0].path == "src/lib.rs"
    assert result[0].line == 12
    assert result[0].column == 5
    assert result[0].source == "cargo"


def test_tsc_location():
    text = "src/x.ts(7,3): error TS2322: Type 'number' is not assignable.\n"
    result = localize_failures(text)
    assert result[0].path == "src/x.ts"
    assert result[0].line == 7
    assert result[0].column == 3


def test_dedups_identical_locations():
    text = '''  File "a.py", line 3, in f
  File "a.py", line 3, in f
'''
    result = localize_failures(text)
    assert len(result) == 1


def test_empty_or_unparseable_returns_empty():
    assert localize_failures("") == []
    assert localize_failures("all good, no failures here") == []


def test_alias_module_reexports():
    assert localize_br("FAILED t.py::test_x") is not None


def test_as_dict_is_json_friendly():
    text = "  File \"a.py\", line 3, in f\n"
    payload = localize_failures(text)[0].as_dict()
    assert set(payload) >= {"path", "line", "confidence", "source"}


def test_symbol_mapping_optional(monkeypatch):
    class FakeSymbol:
        def __init__(self, qn, start, end):
            self.qualified_name = qn
            self.start_line = start
            self.end_line = end
            self.path = "src/model.py"

    class FakeStore:
        def list_by_snapshot(self, snapshot_id):
            return [FakeSymbol("Model.forward", 40, 50)]

    text = '  File "src/model.py", line 42, in forward\n'
    result = localize_failures(text, snapshot_id="snap", symbols=FakeStore())
    assert result[0].symbol == "Model.forward"
    assert result[0].confidence == "high"
"""Checker-specific exit-code semantics (non-pytest runners)."""

from __future__ import annotations

from mayaworkbench.tasks.checkers import (
    FAMILIES,
    classify_exit,
    detect_family,
    failing_tests,
    test_summary as checker_summary,
)


def test_detect_family_pytest_variants():
    assert detect_family(["pytest", "-q"]).name == "pytest"
    assert detect_family(["python", "-m", "pytest", "tests"]).name == "pytest"
    assert detect_family(["python3", "-m", "pytest"]).name == "pytest"


def test_detect_family_cargo_go_js():
    assert detect_family(["cargo", "test"]).name == "cargo"
    assert detect_family(["go", "test", "./..."]).name == "go"
    assert detect_family(["npm", "test"]).name == "jest"
    assert detect_family(["npx", "jest"]).name == "jest"
    assert detect_family(["vitest", "run"]).name == "jest"


def test_detect_family_linters_and_build():
    assert detect_family(["tsc", "--noEmit"]).name == "tsc"
    assert detect_family(["eslint", "."]).name == "eslint"
    assert detect_family(["ruff", "check", "."]).name == "ruff"
    assert detect_family(["mypy", "src"]).name == "mypy"
    assert detect_family(["flake8", "."]).name == "flake8"
    assert detect_family(["make", "test"]).name == "make"
    assert detect_family(["gradle", "test"]).name == "jvm"


def test_pytest_classification_unchanged():
    assert classify_exit(["pytest"], 0, "")[0] is False
    assert classify_exit(["pytest"], 1, "")[0] is False
    assert classify_exit(["pytest"], 3, "")[0] is True
    assert classify_exit(["pytest"], 4, "")[0] is True
    assert classify_exit(["pytest"], 5, "")[0] is True
    assert classify_exit(["pytest"], 6, "")[0] is True
    assert classify_exit(["pytest"], 127, "pytest: command not found")[0] is True
    assert classify_exit(["pytest"], 1, "No module named pytest")[0] is True


def test_cargo_classification():
    assert classify_exit(["cargo", "test"], 0, "")[0] is False
    # a real test failure is code, not apparatus
    assert classify_exit(["cargo", "test"], 101, "test result: FAILED. 1 passed; 1 failed")[0] is False
    # compile error is still a code failure the implementer can repair
    assert classify_exit(["cargo", "test"], 101, "error[E0308]: mismatched types\ncould not compile")[0] is False
    # usage / missing subcommand is apparatus
    assert classify_exit(["cargo", "test"], 101, "error: no such subcommand: 'tset'")[0] is True
    assert classify_exit(["cargo", "test"], 127, "cargo: command not found")[0] is True


def test_go_classification():
    assert classify_exit(["go", "test", "./..."], 0, "")[0] is False
    assert classify_exit(["go", "test", "./..."], 1, "--- FAIL: TestFoo")[0] is False
    assert classify_exit(["go", "test", "./..."], 1, "no Go files in ...")[0] is True
    assert classify_exit(["go", "test", "./..."], 1, "unrecognized import path")[0] is True


def test_js_classification():
    assert classify_exit(["npm", "test"], 0, "")[0] is False
    assert classify_exit(["npm", "test"], 1, "Tests: 1 failed, 4 passed")[0] is False
    assert classify_exit(["jest"], 1, "No tests found")[0] is True
    assert classify_exit(["jest"], 2, "Unknown option --foo")[0] is True


def test_tsc_eslint_lint_classification():
    assert classify_exit(["tsc", "--noEmit"], 0, "")[0] is False
    assert classify_exit(["tsc", "--noEmit"], 1, "src/a.ts:3:5 - error TS2322")[0] is False
    assert classify_exit(["tsc", "--noEmit"], 2, "error TS6053: File not found")[0] is True

    assert classify_exit(["eslint", "."], 0, "")[0] is False
    assert classify_exit(["eslint", "."], 1, "  3:5  error  no-unused-vars")[0] is False
    assert classify_exit(["eslint", "."], 2, "ESLint couldn't find a configuration file")[0] is True

    assert classify_exit(["ruff", "check", "."], 0, "")[0] is False
    assert classify_exit(["ruff", "check", "."], 1, "src/a.py:1:1: F401")[0] is False
    assert classify_exit(["ruff", "check", "."], 2, "error: unexpected argument")[0] is True


def test_mypy_flake8_classification():
    assert classify_exit(["mypy", "src"], 0, "")[0] is False
    assert classify_exit(["mypy", "src"], 1, "src/a.py:3: error: Incompatible types")[0] is False
    assert classify_exit(["mypy", "src"], 2, "usage: mypy ...")[0] is True

    assert classify_exit(["flake8", "."], 0, "")[0] is False
    assert classify_exit(["flake8", "."], 1, "src/a.py:1:1: F401")[0] is False


def test_generic_fallback_for_unknown_checker():
    assert classify_exit(["some-tool", "run"], 0, "")[0] is False
    assert classify_exit(["some-tool", "run"], 1, "")[0] is False
    assert classify_exit(["some-tool", "run"], 127, "some-tool: command not found")[0] is True
    assert classify_exit(["some-tool", "run"], 1, "No module named foo")[0] is True
    assert classify_exit(["some-tool", "run"], None, "")[0] is True


def test_failing_tests_pytest_backward_compat():
    output = "FAILED tests/test_a.py::test_count_tokens - assert 2 == 1\n"
    assert failing_tests(output) == ["tests/test_a.py::test_count_tokens"]


def test_failing_tests_cargo():
    output = "test foo::bar ... FAILED\ntest result: FAILED. 1 passed; 1 failed\n"
    result = failing_tests(output, argv=["cargo", "test"])
    assert "foo::bar" in result


def test_failing_tests_go():
    output = "--- FAIL: TestFoo (0.00s)\n"
    result = failing_tests(output, argv=["go", "test", "./..."])
    assert "TestFoo" in result


def test_failing_tests_js():
    output = "  ✕ should count tokens\n"
    result = failing_tests(output, argv=["jest"])
    assert any("should count tokens" in item for item in result)


def test_failing_tests_lint_extracts_locations():
    output = "src/a.py:1:1: F401 `os` imported but unused\n"
    result = failing_tests(output, argv=["ruff", "check", "."])
    assert result and "src/a.py:1" in result[0]


def test_checker_summary_back_compat():
    output = "=========================== 1 failed, 4 passed in 0.12s ===========================\n"
    # pytest summary regex expects a leading line; ensure no crash
    assert checker_summary(output) is None or isinstance(checker_summary(output), str)


def test_every_family_has_callable_summary():
    for family in FAMILIES:
        assert callable(family.summary)
        assert callable(family.classify)
        assert callable(family.failing)
        assert callable(family.detect)


def test_classify_never_raises_on_weird_input():
    for argv in ([], ["x"], [None], [123]):
        try:
            classify_exit(argv, 1, "boom")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"raised {exc!r}") from exc

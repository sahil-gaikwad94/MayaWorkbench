"""AST test-integrity analysis (§13.3).

This is a structural warning system, not a proof. It compares the base revision
with the candidate workspace and reports detectable weakening:

* deleted or renamed test functions;
* newly added skip/xfail decorators or inline skips;
* assertion-count reductions for a test that still exists;
* tautological assertions (``assert True``, ``assert x == x``);
* inflated numerical tolerances;
* changed fixtures and test configuration;
* disabled collection.

Findings are ``POTENTIAL_*`` style warnings with exact locations. An empty
result means "nothing detectable was found", never "the tests cannot be gamed".
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Mapping

from mayaworkbench.domain.enums import FindingSeverity
from mayaworkbench.domain.models import VerificationFinding
from mayaworkbench.tasks.review import is_test_path

CONFIG_PATHS = {
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "conftest.py",
}
CONFIG_PATTERNS = (
    re.compile(r"^\s*addopts\s*=.*(--ignore|--deselect|-k\s|--maxfail|testpaths)", re.I),
    re.compile(r"^\s*collect_ignore\s*=", re.I),
    re.compile(r"^\s*filterwarnings\s*=.*ignore", re.I),
    re.compile(r"^\s*norecursedirs\s*=", re.I),
)
COLLECTION_DISABLED = re.compile(r"^\s*__test__\s*=\s*False\s*$", re.M)
TOLERANCE_CALLS = {"approx", "allclose", "assert_allclose", "isclose"}


@dataclass
class _TestInfo:
    name: str
    line: int
    assertions: int
    skip_markers: list[str] = field(default_factory=list)
    tautologies: list[int] = field(default_factory=list)
    tolerances: list[float] = field(default_factory=list)


def _decorator_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is available on 3.11+
        return ""


def _is_skip_decorator(text: str) -> bool:
    return bool(
        re.search(r"(?:^|\.)skip(?:if)?\s*(?:\(|$)", text)
        or re.search(r"(?:^|\.)xfail\s*(?:\(|$)", text)
        or text.strip() in {"skip", "skipif", "xfail"}
    )


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _walk_without_nested(nodes: list[ast.AST]):
    """Yield nodes in a function body but do not descend into nested defs."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            stack.append(child)


def _analyse_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> _TestInfo:
    info = _TestInfo(name=node.name, line=node.lineno, assertions=0)
    for decorator in node.decorator_list:
        text = _decorator_name(decorator)
        if _is_skip_decorator(text):
            info.skip_markers.append(f"decorator {text}")
    for child in _walk_without_nested(node.body):
        if isinstance(child, ast.Assert):
            info.assertions += 1
            test = child.test
            if isinstance(test, ast.Constant) and bool(test.value):
                info.tautologies.append(child.lineno)
            elif isinstance(test, ast.Compare) and len(test.ops) == 1:
                if (
                    isinstance(test.ops[0], (ast.Eq, ast.LtE, ast.GtE))
                    and ast.dump(test.left) == ast.dump(test.comparators[0])
                ):
                    info.tautologies.append(child.lineno)
        elif isinstance(child, ast.Call):
            name = _call_name(child)
            if name.startswith("assert"):
                info.assertions += 1
                if any(
                    isinstance(arg, ast.Constant) and bool(arg.value)
                    for arg in child.args
                ):
                    info.tautologies.append(child.lineno)
            if name in {"skip", "xfail", "skipTest", "importorskip"}:
                info.skip_markers.append(f"call {name} at line {child.lineno}")
            if name in TOLERANCE_CALLS:
                for arg in list(child.args):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                        info.tolerances.append(float(arg.value))
                for keyword in child.keywords:
                    if keyword.arg in {"atol", "rtol", "tol", "abs", "rel"} and isinstance(
                        keyword.value, ast.Constant
                    ) and isinstance(keyword.value.value, (int, float)):
                        info.tolerances.append(float(keyword.value.value))
    return info


def _test_functions(source: str) -> tuple[dict[str, _TestInfo], list[int]]:
    """Return test functions and lines with syntax errors."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, [0]
    found: dict[str, _TestInfo] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                found[node.name] = _analyse_function(node)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test"):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        child.name.startswith("test")
                    ):
                        key = f"{node.name}.{child.name}"
                        found[key] = _analyse_function(child)
    return found, []


def _fixture_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                text = _decorator_name(decorator)
                if text.rstrip("()") == "pytest.fixture" or "fixture" in text:
                    names.add(node.name)
    return names


def _tolerances_inflated(before: list[float], after: list[float]) -> tuple[bool, float, float]:
    if not before or not after:
        return False, 0.0, 0.0
    old = max(before)
    new = max(after)
    if old == 0.0:
        return new > 0.0, old, new
    return new > old * 2.0, old, new


def analyze_test_integrity(
    baseline: Mapping[str, str],
    candidate: Mapping[str, str],
) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []
    paths = sorted(
        path
        for path in set(baseline) | set(candidate)
        if is_test_path(path) and path.endswith(".py")
    )
    for path in paths:
        old_source = baseline.get(path)
        new_source = candidate.get(path)
        if old_source is None:
            # A brand-new test file is not weakening, but disabled collection is.
            if new_source and COLLECTION_DISABLED.search(new_source):
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="COLLECTION_DISABLED",
                        message="A new test file disables pytest collection with `__test__ = False`.",
                        path=path,
                    )
                )
            continue
        if new_source is None:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="TEST_FILE_DELETED",
                    message="A test file present in the base revision was deleted.",
                    path=path,
                )
            )
            continue
        if COLLECTION_DISABLED.search(new_source) and not COLLECTION_DISABLED.search(old_source):
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="COLLECTION_DISABLED",
                    message="The candidate disables pytest collection with `__test__ = False`.",
                    path=path,
                )
            )
        old_tests, old_errors = _test_functions(old_source)
        new_tests, new_errors = _test_functions(new_source)
        if old_errors and not new_errors:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.WARNING,
                    code="TEST_FILE_PREVIOUSLY_UNPARSEABLE",
                    message=(
                        "The baseline test file had a syntax error and the candidate "
                        "parses; test-integrity comparison is limited for this file."
                    ),
                    path=path,
                )
            )
        if new_errors:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.WARNING,
                    code="TEST_FILE_UNPARSEABLE",
                    message="The candidate test file does not parse; its tests cannot be compared.",
                    path=path,
                )
            )
            continue
        removed = sorted(set(old_tests) - set(new_tests))
        added = sorted(set(new_tests) - set(old_tests))
        for name in removed:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.ERROR,
                    code="TEST_DELETED",
                    message=(
                        f"Test `{name}` exists in the base revision and is absent from the "
                        "candidate. A rename is reported the same way until reviewed."
                    ),
                    path=path,
                    line=old_tests[name].line,
                )
            )
        if removed and added and len(removed) == len(added):
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.INFO,
                    code="TEST_RENAME_SUSPECTED",
                    message=(
                        "The same number of tests disappeared and appeared; check whether "
                        "this is a rename that drops coverage."
                    ),
                    path=path,
                )
            )
        for name, old in old_tests.items():
            new = new_tests.get(name)
            if new is None:
                continue
            if len(new.skip_markers) > len(old.skip_markers):
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="SKIP_ADDED",
                        message=(
                            f"Test `{name}` gained {len(new.skip_markers) - len(old.skip_markers)} "
                            "skip/xfail marker(s): "
                            + "; ".join(sorted(set(new.skip_markers) - set(old.skip_markers)))
                        ),
                        path=path,
                        line=new.line,
                    )
                )
            if new.assertions < old.assertions:
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.WARNING,
                        code="ASSERTIONS_REDUCED",
                        message=(
                            f"Test `{name}` has {new.assertions} assertion(s) in the candidate "
                            f"and {old.assertions} in the base revision."
                        ),
                        path=path,
                        line=new.line,
                        details={"baseline": old.assertions, "candidate": new.assertions},
                    )
                )
            if len(new.tautologies) > len(old.tautologies):
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="TAUTOLOGICAL_ASSERTION",
                        message=(
                            f"Test `{name}` adds an assertion that cannot fail by "
                            "construction."
                        ),
                        path=path,
                        line=new.tautologies[-1],
                    )
                )
            inflated, old_tol, new_tol = _tolerances_inflated(
                old.tolerances, new.tolerances
            )
            if inflated:
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.ERROR,
                        code="TOLERANCE_INFLATED",
                        message=(
                            f"Test `{name}` loosens a numerical tolerance from {old_tol:g} "
                            f"to {new_tol:g}."
                        ),
                        path=path,
                        line=new.line,
                        details={"baseline": old_tol, "candidate": new_tol},
                    )
                )
        old_fixtures = _fixture_names(old_source)
        new_fixtures = _fixture_names(new_source)
        for name in sorted(old_fixtures & new_fixtures):
            if _function_source(old_source, name) != _function_source(new_source, name):
                findings.append(
                    VerificationFinding(
                        severity=FindingSeverity.WARNING,
                        code="FIXTURE_CHANGED",
                        message=(
                            f"Fixture `{name}` changed. Fixture changes can weaken tests "
                            "indirectly; review the diff."
                        ),
                        path=path,
                    )
                )
        removed_fixtures = sorted(old_fixtures - new_fixtures)
        for name in removed_fixtures:
            findings.append(
                VerificationFinding(
                    severity=FindingSeverity.WARNING,
                    code="FIXTURE_REMOVED",
                    message=f"Fixture `{name}` was removed.",
                    path=path,
                )
            )

    # Test configuration: only additions that narrow or silence collection.
    for path in sorted(CONFIG_PATHS):
        old_source = baseline.get(path)
        new_source = candidate.get(path)
        if new_source is None or new_source == old_source:
            continue
        old_lines = set((old_source or "").splitlines())
        for number, line in enumerate(new_source.splitlines(), start=1):
            if line in old_lines:
                continue
            if not any(pattern.search(line) for pattern in CONFIG_PATTERNS):
                continue
            severity = (
                FindingSeverity.ERROR
                if "collect_ignore" in line
                else FindingSeverity.WARNING
            )
            findings.append(
                VerificationFinding(
                    severity=severity,
                    code="TEST_CONFIG_WEAKENED",
                    message=(
                        "Test configuration changed in a way that can narrow or silence "
                        f"collection: {line.strip()}"
                    ),
                    path=path,
                    line=number,
                )
            )
    findings.sort(key=lambda item: (item.path or "", item.line or 0, item.code))
    return findings[:200]


def _function_source(source: str, name: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            try:
                return ast.unparse(node)
            except Exception:  # pragma: no cover
                return ""
    return ""


def integrity_outcome(findings: list[VerificationFinding]) -> str:
    if any(item.severity == FindingSeverity.ERROR for item in findings):
        return "FAILED"
    return "PASSED"


__all__ = ["analyze_test_integrity", "integrity_outcome", "CONFIG_PATHS"]

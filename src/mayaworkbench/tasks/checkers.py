"""Checker-specific exit-code and output semantics for in-loop declared checks.

``tasks/inloop.py`` historically knew only ``pytest``'s exit codes (3/4/5/6 are
apparatus failures, not code failures). A workbench that runs real projects
also runs ``cargo test``, ``go test``, ``jest``, ``tsc``, ``eslint``, ``ruff``,
``mypy`` and friends, each of which uses a different convention for "the tool
could not produce a verdict" versus "the code is wrong". Treating every
non-zero exit as a code failure sends the implementer into pointless repair
loops when the checker itself is broken; treating every non-zero exit as an
apparatus failure hides real regressions.

This module is the single place where those conventions live. It is deliberately
*data*, not policy: it classifies a result, it never decides whether a check
should run, and it never invents a verdict. Unknown checkers fall through to
the conservative generic family.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

__all__ = [
    "CheckerFamily",
    "CheckerClassification",
    "detect_family",
    "classify_exit",
    "failing_tests",
    "test_summary",
    "FAMILIES",
]


@dataclass(frozen=True)
class CheckerClassification:
    is_infrastructure: bool
    reason: str
    family: str


def _argv_strings(argv: Iterable[str]) -> list[str]:
    return [str(item) for item in argv]


def _executable_name(argv: Sequence[str]) -> str:
    if not argv:
        return ""
    return Path(str(argv[0])).name.lower()


def _joined(argv: Sequence[str]) -> str:
    return " ".join(str(item) for item in argv).lower()


def _contains_module(argv: Sequence[str], module: str) -> bool:
    args = [str(item) for item in argv]
    for index, item in enumerate(args):
        if item == "-m" and index + 1 < len(args) and args[index + 1] == module:
            return True
    return False


def _broken_markers(output: str) -> bool:
    lowered = (output or "").lower()
    markers = (
        "no module named",
        "modulenotfounderror",
        "importerror",
        "command not found",
        "not recognized as an internal or external command",
        "no such file or directory",
        "unrecognized arguments",
        "no such option",
        "missing script",
        "could not find",
        "no go files",
        "cannot find package",
    )
    return any(marker in lowered for marker in markers)


# --------------------------------------------------------------------------- families


def _detect_pytest(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    if name in {"pytest", "py.test"}:
        return True
    if name.startswith("python") and _contains_module(argv, "pytest"):
        return True
    joined = _joined(argv)
    return "pytest" in joined and ("-m" in joined or name in {"pytest", "py.test"})


def _classify_pytest(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "KeyboardInterrupt" in (output or ""):
        return True, "the check was interrupted"
    if exit_code == 1:
        return False, "tests were collected and some failed"
    if exit_code == 2:
        return False, "pytest was interrupted or collection failed (exit 2)"
    if exit_code == 3:
        return True, "pytest reported an internal error (exit 3); the checker is broken, not the code"
    if exit_code == 4:
        return True, "pytest reported a usage error (exit 4); the declared command cannot run"
    if exit_code == 5:
        return True, "pytest collected no tests (exit 5); the check proves nothing"
    if exit_code == 6:
        return True, "pytest exceeded its warning ceiling (exit 6); tests passed but the run is not clean"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, f"pytest exited with {exit_code}"


def _failing_pytest(output: str) -> list[str]:
    found: list[str] = []
    for pattern in (
        re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M),
        re.compile(r"^_{3,}\s+(\S+)\s+_{3,}$", re.M),
    ):
        for match in pattern.finditer(output or ""):
            candidate = match.group(1).strip()
            if not candidate or len(candidate) > 300:
                continue
            duplicate = False
            for index, item in enumerate(found):
                if candidate == item or item.endswith(candidate):
                    duplicate = True
                    break
                if candidate.endswith(item):
                    found[index] = candidate
                    duplicate = True
                    break
            if duplicate:
                continue
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


def _summary_pytest(output: str) -> str | None:
    matches = re.findall(
        r"^(?:=+\s*)?(?P<counts>\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings)"
        r"(?:,\s*\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings))*.*)$",
        output or "",
        re.M,
    )
    if not matches:
        return None
    return str(matches[-1]).strip()[:300]


# --- javascript runners ------------------------------------------------------


def _detect_js_test(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    joined = _joined(argv)
    if name in {"jest", "vitest", "mocha", "jasmine"}:
        return True
    if name in {"npm", "pnpm", "yarn", "npx", "bun"} and ("test" in joined or "jest" in joined or "vitest" in joined or "mocha" in joined):
        return True
    return any(token in joined for token in ("jest", "vitest", "mocha"))


def _classify_js_test(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "no tests found" in lowered or "no test files found" in lowered or "no tests" in lowered:
        return True, "the test runner collected no tests; the check proves nothing"
    if exit_code == 2:
        # jest uses 2 for configuration/usage errors
        if "configuration" in lowered or "unknown option" in lowered or "invalid" in lowered:
            return True, "the test runner reported a configuration or usage error (exit 2)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "tests were collected and some failed"


def _failing_js(output: str) -> list[str]:
    found: list[str] = []
    patterns = (
        re.compile(r"^\s*[✕×x]\s+(.+)$", re.M),
        re.compile(r"^\s*●\s+(.+)$", re.M),
        re.compile(r"^FAIL\s+(.+)$", re.M),
        re.compile(r"^\s*-\s+(.+?)\s*$", re.M),
    )
    for pattern in patterns:
        for match in pattern.finditer(output or ""):
            candidate = match.group(1).strip()
            if not candidate or len(candidate) > 300 or candidate.startswith("("):
                continue
            if candidate in found:
                continue
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


def _summary_js(output: str) -> str | None:
    # jest/vitest: "Tests:  1 failed, 4 passed, 5 total"
    for line in (output or "").splitlines()[::-1]:
        stripped = line.strip()
        if re.search(r"\bTests?:", stripped, re.I) or re.search(r"\bTest Files", stripped, re.I):
            return stripped[:300]
    return None


# --- cargo ------------------------------------------------------------------


def _detect_cargo(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    joined = _joined(argv)
    return name in {"cargo", "cargo.exe"} and "test" in joined


def _classify_cargo(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "no such subcommand" in lowered or "could not find" in lowered or "usage:" in lowered:
        return True, "cargo reported a usage or configuration error; the declared command cannot run"
    if "error[e" in lowered or "could not compile" in lowered:
        # A compile error is a code failure the implementer can repair, not an
        # apparatus failure — but only when the toolchain itself ran.
        return False, "the project failed to compile"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "tests were collected and some failed"


def _failing_cargo(output: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"test\s+(\S+)\s+\.\.\.\s+FAILED", output or ""):
        candidate = match.group(1).strip()
        if candidate and candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    for match in re.finditer(r"^test result: FAILED", output or "", re.M):
        pass
    # fallback: lines "FAILED: name"
    for match in re.finditer(r"FAILED[:\s]+(\S+)", output or ""):
        candidate = match.group(1).strip()
        if candidate and candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


def _summary_cargo(output: str) -> str | None:
    for line in (output or "").splitlines()[::-1]:
        stripped = line.strip()
        if stripped.startswith("test result:"):
            return stripped[:300]
    return None


# --- go test ----------------------------------------------------------------


def _detect_go(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    joined = _joined(argv)
    return name in {"go", "go.exe"} and "test" in joined


def _classify_go(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "no go files" in lowered or "no test files" in lowered:
        return True, "go test collected no tests; the check proves nothing"
    if "unrecognized" in lowered or "usage:" in lowered or "invalid" in lowered:
        return True, "go test reported a usage or configuration error"
    if "vet:" in lowered and "error" in lowered:
        return False, "go vet reported an issue"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "tests were collected and some failed"


def _failing_go(output: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"^---\s+FAIL:\s+(\S+)", output or "", re.M):
        candidate = match.group(1).strip()
        if candidate and candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


def _summary_go(output: str) -> str | None:
    for line in (output or "").splitlines()[::-1]:
        stripped = line.strip()
        if stripped.startswith("ok ") or stripped.startswith("FAIL") or "PASS" in stripped or "FAIL" in stripped:
            if stripped:
                return stripped[:300]
    return None


# --- typescript / tsc -------------------------------------------------------


def _detect_tsc(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    joined = _joined(argv)
    return name in {"tsc", "tsc.exe"} or "tsc" in joined.split()


def _classify_tsc(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "cannot find" in lowered or "tsconfig" in lowered and "not found" in lowered:
        return True, "tsc reported a configuration or project error"
    if exit_code == 2:
        return True, "tsc reported a configuration error (exit 2)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "type errors were reported"


def _failing_tsc(output: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"(\S+\.tsx?):(\d+):(\d+)\s*-\s*error", output or ""):
        candidate = f"{match.group(1)}:{match.group(2)}"
        if candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


# --- eslint -----------------------------------------------------------------


def _detect_eslint(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    joined = _joined(argv)
    return name in {"eslint", "eslint.js"} or "eslint" in joined


def _classify_eslint(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "couldn't find" in lowered or "no such file" in lowered or "config" in lowered and "error" in lowered:
        if "config" in lowered and ("not found" in lowered or "invalid" in lowered):
            return True, "eslint reported a configuration error"
    if exit_code == 2:
        return True, "eslint reported a configuration or runtime error (exit 2)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "lint violations were reported"


def _failing_eslint(output: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"(\S+\.jsx?):(\d+):(\d+)", output or ""):
        candidate = f"{match.group(1)}:{match.group(2)}"
        if candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


# --- python linters / type checkers -----------------------------------------


def _detect_ruff(argv: Sequence[str]) -> bool:
    return "ruff" in _joined(argv)


def _classify_ruff(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "usage:" in lowered or "invalid" in lowered or "error:" in lowered and "rule" not in lowered:
        # ruff prints "error:" for usage problems; lint findings are listed per file
        if "no such file" in lowered or "unexpected argument" in lowered:
            return True, "ruff reported a usage or configuration error"
    if exit_code == 2:
        return True, "ruff reported a usage or configuration error (exit 2)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "lint violations were reported"


def _detect_mypy(argv: Sequence[str]) -> bool:
    return "mypy" in _joined(argv)


def _classify_mypy(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "usage:" in lowered or "error:" in lowered and "note:" not in lowered and ":" not in (output or "")[:0]:
        pass
    if exit_code == 2:
        return True, "mypy reported a usage or configuration error (exit 2)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "type errors were reported"


def _detect_flake8(argv: Sequence[str]) -> bool:
    joined = _joined(argv)
    return "flake8" in joined or "pylint" in joined


def _classify_flake8(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "usage:" in lowered or "no such file" in lowered:
        return True, "the linter reported a usage or configuration error"
    if exit_code >= 2 and exit_code not in (1,):
        # pylint uses bitmasks; treat high codes as tool failure only when
        # accompanied by usage markers, otherwise as lint findings.
        if "usage" in lowered or "error:" in lowered and "warning" not in lowered:
            return True, "the linter reported a runtime or configuration error"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "lint violations were reported"


def _failing_lint(output: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"(\S+\.py):\d+:\d+", output or ""):
        candidate = match.group(0)
        if candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    # generic file:line
    for match in re.finditer(r"(\S+\.\w+):(\d+)", output or ""):
        candidate = f"{match.group(1)}:{match.group(2)}"
        if candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


# --- gradle / maven ---------------------------------------------------------


def _detect_jvm_build(argv: Sequence[str]) -> bool:
    joined = _joined(argv)
    return any(tok in joined for tok in ("gradle", "gradlew", "maven", "mvn", "mvnw"))


def _classify_jvm_build(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "build successful" in lowered:
        return False, "command exited 0"
    if "could not find" in lowered or "unknown task" in lowered or "invalid" in lowered:
        return True, "the build tool reported a configuration or usage error"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "build or tests failed"


# --- make -------------------------------------------------------------------


def _detect_make(argv: Sequence[str]) -> bool:
    name = _executable_name(argv)
    return name in {"make", "gmake", "nmake"}


def _classify_make(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    lowered = (output or "").lower()
    if "no rule to make target" in lowered or "missing separator" in lowered:
        return True, "make reported a Makefile error; the declared command cannot run"
    if exit_code == 2:
        return True, "make reported a usage or Makefile error (exit 2)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, "build or tests failed"


# --- generic ----------------------------------------------------------------


def _detect_generic(argv: Sequence[str]) -> bool:
    return True


def _classify_generic(exit_code: int | None, output: str) -> tuple[bool, str]:
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    lowered = (output or "").lower()
    if _broken_markers(output):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, f"command exited {exit_code}"


def _failing_generic(output: str) -> list[str]:
    # Best-effort: lines that look like file:line references.
    found: list[str] = []
    for match in re.finditer(r"(\S+\.\w+):(\d+)", output or ""):
        candidate = f"{match.group(1)}:{match.group(2)}"
        if candidate not in found:
            found.append(candidate)
            if len(found) >= 12:
                return found
    return found


# --------------------------------------------------------------------------- registry

@dataclass(frozen=True)
class CheckerFamily:
    name: str
    detect: Callable[[Sequence[str]], bool]
    classify: Callable[[int | None, str], tuple[bool, str]]
    failing: Callable[[str], list[str]]
    summary: Callable[[str], str | None]


def _null_summary(_output: str) -> str | None:
    return None


FAMILIES = [
    CheckerFamily("pytest", _detect_pytest, _classify_pytest, _failing_pytest, _summary_pytest),
    CheckerFamily("cargo", _detect_cargo, _classify_cargo, _failing_cargo, _summary_cargo),
    CheckerFamily("go", _detect_go, _classify_go, _failing_go, _summary_go),
    CheckerFamily("jest", _detect_js_test, _classify_js_test, _failing_js, _summary_js),
    CheckerFamily("tsc", _detect_tsc, _classify_tsc, _failing_tsc, _null_summary),
    CheckerFamily("eslint", _detect_eslint, _classify_eslint, _failing_eslint, _null_summary),
    CheckerFamily("ruff", _detect_ruff, _classify_ruff, _failing_lint, _null_summary),
    CheckerFamily("mypy", _detect_mypy, _classify_mypy, _failing_lint, _null_summary),
    CheckerFamily("flake8", _detect_flake8, _classify_flake8, _failing_lint, _null_summary),
    CheckerFamily("jvm", _detect_jvm_build, _classify_jvm_build, _failing_generic, _null_summary),
    CheckerFamily("make", _detect_make, _classify_make, _failing_generic, _null_summary),
    CheckerFamily("generic", _detect_generic, _classify_generic, _failing_generic, _null_summary),
]


def detect_family(argv: Iterable[str]) -> CheckerFamily:
    args = list(argv)
    for family in FAMILIES:
        if family.name == "generic":
            continue
        try:
            if family.detect(args):
                return family
        except Exception:  # noqa: BLE001 - detection must never raise
            continue
    return FAMILIES[-1]


def classify_exit(argv: Iterable[str], exit_code: int | None, output: str = "") -> tuple[bool, str]:
    """Backward-compatible wrapper returning ``(is_infrastructure, reason)``."""
    family = detect_family(argv)
    is_infra, reason = family.classify(exit_code, output)
    return is_infra, reason


def failing_tests(output: str, argv: Iterable[str] | None = None) -> list[str]:
    """Extract failing test identifiers.

    Backward compatible: ``failing_tests(text)`` uses the pytest extractor.
    Pass ``argv`` to select the checker family.
    """
    if argv is None:
        return _failing_pytest(output)
    family = detect_family(argv)
    try:
        return family.failing(output)
    except Exception:  # noqa: BLE001
        return _failing_pytest(output)


def test_summary(output: str, argv: Iterable[str] | None = None) -> str | None:
    if argv is None:
        return _summary_pytest(output)
    family = detect_family(argv)
    try:
        result = family.summary(output)
        if result is not None:
            return result
    except Exception:  # noqa: BLE001
        pass
    return _summary_pytest(output)

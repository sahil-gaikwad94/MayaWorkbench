"""Mutation probes for declared critical tests (audit §11.2).

The AST test-integrity check in :mod:`~mayaworkbench.verification.integrity`
reports *structural* weakening. That is necessary but not sufficient: a test
can look intact while no longer exercising the property it claims to guard.
This module adds a second, behavioural layer for a small, declared set of
critical tests:

1. read the CANDIDATE test file;
2. apply a source-level mutation to one of its assertions (invert a comparison,
   change an expected constant, or replace the assertion with a forced-failure
   sentinel);
3. run *only that test* on an in-memory copy of the candidate tree through the
   real :class:`~mayaworkbench.workspace.runner.CommandRunner` (via
   :func:`~mayaworkbench.verification.execution.execute_check`);
4. record ``KILLED`` when the test fails, ``SURVIVED`` when it still passes, and
   ``SKIPPED`` with an explicit reason whenever the probe could not run.

A declared critical test is pinned by the sha256 of its file at the base
revision. The pin is recorded in verification details and compared with the
candidate so a change that removes or weakens the test before probing is
flagged rather than silently accepted.

Honest limitations
------------------
* This is **property-level evidence, not a proof**. A killed mutant shows that
  one assertion in one declared test is load-bearing for one small fault; it
  does not show semantic equivalence, absence of other gaming, or coverage of
  undeclared tests.
* A survived mutant can mean a real blind spot, but also a skipped/filtered
  test, a swallowed ``AssertionError``, or a mutation that missed the relevant
  branch. The probe never upgrades a survival into a pass.
* The probe mutates the *test*, not production code. It asks "does this
  assertion still decide the outcome?", not "does this test catch the original
  bug?" (the latter remains an independent-review question).
* ``neutralize`` is a reachability probe: it replaces the assertion with
  ``assert False``. If the test still passes, its failure was not observable.

No model is involved. All outcomes come from recorded process exit codes.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from mayaworkbench.domain.enums import (
    ArtifactKind,
    CheckOutcome,
    FindingSeverity,
)
from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import MayaError
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.verification import execution, integrity

MUTATION_KINDS = ("invert_comparison", "replace_constant", "neutralize")
PROBE_OUTCOMES = ("KILLED", "SURVIVED", "SKIPPED")
DEFAULT_MAX_PROBES = 8
DEFAULT_MAX_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 30

SourceReader = Callable[[str, str], str | None]
"""Callable ``(path, tree) -> source`` where ``tree`` is ``base`` or ``candidate``."""

_INVERT = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.GtE: ast.Lt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_NO_REPLACEMENT = object()
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", "*.pyc", ".venv", "node_modules"
)


@dataclass(frozen=True)
class CriticalTest:
    """One declared critical test function and its preferred mutation."""

    path: str
    test: str
    mutation: str = "auto"
    check_key: str | None = None

    @property
    def nodeid(self) -> str:
        return f"{self.path}::{self.test}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "test": self.test,
            "mutation": self.mutation,
            "check_key": self.check_key,
        }


@dataclass
class ProbeResult:
    """One executed (or explicitly skipped) mutation probe."""

    mutation_id: str
    file: str
    line: int
    test: str
    kind: str
    outcome: str
    reason: str
    artifact_id: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    command: list[str] = field(default_factory=list)
    declared: bool = True
    baseline_sha256: str | None = None
    candidate_sha256: str | None = None
    bypass_detected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "file": self.file,
            "line": self.line,
            "test": self.test,
            "kind": self.kind,
            "outcome": self.outcome,
            "reason": self.reason,
            "artifact_id": self.artifact_id,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "declared": self.declared,
            "baseline_sha256": self.baseline_sha256,
            "candidate_sha256": self.candidate_sha256,
            "bypass_detected": self.bypass_detected,
        }


@dataclass
class MutationResult:
    """Aggregate evidence for one verification attempt."""

    results: list[ProbeResult]
    baseline: dict[str, Any]
    declared: list[dict[str, Any]]
    bypass_details: list[dict[str, Any]]
    artifact_id: str | None = None
    note: str = (
        "Mutation is property-level evidence, not semantic proof. SKIPPED is "
        "never a pass; SURVIVED marks a blind spot for declared critical tests."
    )

    @property
    def bypass_detected(self) -> bool:
        return bool(self.bypass_details)

    def counts(self) -> dict[str, int]:
        return {
            outcome: sum(1 for item in self.results if item.outcome == outcome)
            for outcome in PROBE_OUTCOMES
        }

    @property
    def survived_declared(self) -> list[ProbeResult]:
        return [item for item in self.results if item.outcome == "SURVIVED" and item.declared]

    def as_dict(self) -> dict[str, Any]:
        return {
            "results": [item.as_dict() for item in self.results],
            "declared": self.declared,
            "baseline": self.baseline,
            "bypass_details": self.bypass_details,
            "bypass_detected": self.bypass_detected,
            "counts": self.counts(),
            "artifact_id": self.artifact_id,
            "note": self.note,
        }


# ------------------------------------------------------------------ declarations
def parse_critical_tests(declarations: Iterable[Any]) -> tuple[list[CriticalTest], list[str]]:
    """Parse declarations into :class:`CriticalTest` entries.

    Accepted forms: ``"tests/test_x.py::test_foo"``,
    ``{"path": ..., "test": ..., "mutation": ...}`` or a ``file``/``name`` alias.
    Invalid declarations are returned as reasons so a caller can record
    ``SKIPPED`` instead of silently dropping them.
    """
    parsed: list[CriticalTest] = []
    errors: list[str] = []
    for index, raw in enumerate(declarations):
        prefix = f"declaration[{index}]"
        if isinstance(raw, str):
            parts = [part for part in raw.split("::") if part]
            if len(parts) < 2 or any(part.startswith("/") or ".." in Path(part).parts for part in parts[:1]):
                errors.append(f"{prefix} is not a valid <path>::<test> node id: {raw!r}")
                continue
            parsed.append(CriticalTest(path=parts[0], test="::".join(parts[1:])))
            continue
        if not isinstance(raw, Mapping):
            errors.append(f"{prefix} must be a string or a mapping")
            continue
        path = raw.get("path") or raw.get("file") or raw.get("file_path")
        test = raw.get("test") or raw.get("name") or raw.get("function")
        if not test and (raw.get("node") or raw.get("nodeid")):
            node = str(raw.get("node") or raw.get("nodeid"))
            pieces = [piece for piece in node.split("::") if piece]
            if len(pieces) >= 2:
                path = path or pieces[0]
                test = test or "::".join(pieces[1:])
        mutation = raw.get("mutation") or raw.get("kind") or "auto"
        if not isinstance(path, str) or not path or not isinstance(test, str) or not test:
            errors.append(f"{prefix} needs a non-empty path and test name")
            continue
        if path.startswith("/") or ".." in Path(path).parts or "\x00" in path:
            errors.append(f"{prefix} has an unsafe path: {path!r}")
            continue
        mutation = str(mutation)
        if mutation != "auto" and mutation not in MUTATION_KINDS:
            errors.append(f"{prefix} has an unknown mutation {mutation!r}")
            continue
        parsed.append(
            CriticalTest(
                path=path,
                test=test,
                mutation=mutation,
                check_key=raw.get("check_key"),
            )
        )
    return parsed, errors


def critical_tests_from_specs(specs: Iterable[Any]) -> tuple[list[CriticalTest], list[str]]:
    """Read ``details.critical_tests`` from check specs (default empty).

    Using the existing ``CheckSpec.details`` extension field keeps older
    contracts valid: an absent declaration simply produces no probes.
    """
    declarations: list[Any] = []
    for spec in specs or []:
        details = getattr(spec, "details", None) or {}
        raw = details.get("critical_tests") if isinstance(details, Mapping) else None
        if not raw:
            continue
        if isinstance(raw, (str, Mapping)):
            raw = [raw]
        for item in raw:
            if isinstance(item, Mapping):
                item = dict(item)
                item.setdefault("check_key", getattr(spec, "key", None))
            declarations.append(item)
    return parse_critical_tests(declarations)


def critical_baseline(
    critical_tests: Sequence[CriticalTest],
    source_reader: SourceReader,
) -> dict[str, Any]:
    """Pin every declared critical test file at the base revision."""
    files: dict[str, Any] = {}
    for item in critical_tests:
        if item.path in files:
            continue
        source = source_reader(item.path, "base")
        files[item.path] = {
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()
            if source is not None
            else None,
            "available": source is not None,
        }
    return {
        "files": files,
        "declared": [item.as_dict() for item in critical_tests],
        "note": "sha256 of each declared critical test file at the pinned base commit",
    }


def detect_bypass(
    path: str,
    base_source: str | None,
    candidate_source: str | None,
    test_name: str,
) -> dict[str, Any] | None:
    """Flag candidate changes that could evade mutation probing."""
    if base_source is None or candidate_source is None or base_source == candidate_source:
        return None
    old_tests, _ = integrity._test_functions(base_source)
    new_tests, _ = integrity._test_functions(candidate_source)
    if test_name in old_tests and test_name not in new_tests:
        return {
            "path": path,
            "test": test_name,
            "codes": ["CRITICAL_TEST_REMOVED"],
            "reason": "the declared critical test is absent from the candidate file",
        }
    findings = [
        finding
        for finding in integrity.analyze_test_integrity(
            {path: base_source}, {path: candidate_source}
        )
        if finding.severity in {FindingSeverity.ERROR, FindingSeverity.WARNING}
    ]
    if findings:
        return {
            "path": path,
            "test": test_name,
            "codes": sorted({finding.code for finding in findings}),
            "reason": "; ".join(finding.message for finding in findings[:5]),
        }
    return None


# ------------------------------------------------------------------- AST mutation
def _replacement_constant(value: Any) -> Any:
    if isinstance(value, bool):  # bool is a subclass of int
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + "_mutated"
    if isinstance(value, bytes):
        return value + b"_mutated"
    return _NO_REPLACEMENT


def _mutate_assert(node: ast.Assert, kind: str) -> ast.Assert | None:
    mutated = copy.deepcopy(node)
    if kind == "neutralize":
        mutated.test = ast.Constant(value=False)
        mutated.msg = ast.Constant(value="mutation:neutralize")
        return mutated
    if kind == "invert_comparison":
        if not isinstance(mutated.test, ast.Compare) or not mutated.test.ops:
            return None
        inverted: list[ast.cmpop] = []
        for op in mutated.test.ops:
            replacement = _INVERT.get(type(op))
            if replacement is None:
                return None
            inverted.append(replacement())
        mutated.test.ops = inverted
        return mutated
    if kind == "replace_constant":
        if not isinstance(mutated.test, ast.Compare):
            return None
        comparators = list(mutated.test.comparators)
        candidates = comparators + [mutated.test.left]
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, ast.Constant):
                continue
            replacement = _replacement_constant(candidate.value)
            if replacement is _NO_REPLACEMENT:
                continue
            if index == len(comparators):
                mutated.test.left = ast.Constant(value=replacement)
            else:
                mutated.test.comparators[index] = ast.Constant(value=replacement)
            return mutated
        return None
    return None


class _Mutator(ast.NodeTransformer):
    def __init__(self, target: ast.Assert, kind: str) -> None:
        self.target = target
        self.kind = kind
        self.applied = False

    def visit_Assert(self, node: ast.Assert) -> ast.AST:  # noqa: N802 - ast API
        if node is not self.target:
            return self.generic_visit(node)
        mutated = _mutate_assert(node, self.kind)
        if mutated is None:
            return node
        self.applied = True
        return mutated


def _find_test_node(tree: ast.Module, test: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    wanted = test.split("::")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        parts = [node.name]
        for parent in ast.walk(tree):
            if isinstance(parent, ast.ClassDef) and any(
                child is node for child in parent.body
            ):
                parts.insert(0, parent.name)
        if parts == wanted or ([parts[-1]] == wanted):
            return node
    return None


def _assertions(node: ast.AST) -> list[ast.Assert]:
    return [
        child
        for child in integrity._walk_without_nested([node])
        if isinstance(child, ast.Assert)
    ]


def _applicable_mutations(
    assertions: Sequence[ast.Assert], requested: str
) -> list[tuple[str, ast.Assert]]:
    kinds = MUTATION_KINDS if requested == "auto" else (requested,)
    available: list[tuple[str, ast.Assert]] = []
    for kind in kinds:
        for assertion in assertions:
            probe = _mutate_assert(assertion, kind)
            if probe is not None:
                available.append((kind, assertion))
                break
    return available


def _mutated_source(source: str, test: str, kind: str, line: int) -> str:
    tree = ast.parse(source)
    function = _find_test_node(tree, test)
    if function is None:
        raise ValueError(f"test {test!r} was not found in the candidate source")
    target = next((item for item in _assertions(function) if item.lineno == line), None)
    if target is None:
        raise ValueError(f"assertion at line {line} was not found in {test!r}")
    mutator = _Mutator(target, kind)
    mutated = mutator.visit(tree)
    if not mutator.applied:
        raise ValueError(f"mutation {kind!r} could not be applied at line {line}")
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated)


# -------------------------------------------------------------------------- probe
class MutationProbe:
    """Run source-level mutation probes against a candidate workspace tree."""

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        workspace: Path | str | None,
        constraints: TaskConstraints | None = None,
        run_home: Path | str | None = None,
        max_probes: int = DEFAULT_MAX_PROBES,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.artifacts = artifacts
        self.workspace = Path(workspace) if workspace is not None else None
        self.constraints = constraints or TaskConstraints()
        self.run_home = Path(run_home) if run_home is not None else (
            Path(artifacts.root).parent / "mutation-probes"
        )
        self.run_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_probes = max(0, min(int(max_probes), 200))
        self.max_seconds = max(0.0, float(max_seconds))
        self.timeout_seconds = max(1, int(timeout_seconds))

    def run(
        self,
        critical_tests: Sequence[CriticalTest],
        *,
        source_reader: SourceReader | None = None,
        baseline: Mapping[str, Any] | None = None,
        declarations: Sequence[Any] = (),
    ) -> MutationResult:
        """Execute all declared probes within the configured caps."""
        started = time.monotonic()
        read = source_reader or (lambda _path, _tree: None)
        declared = [item.as_dict() for item in critical_tests]
        baseline = dict(baseline or {})
        if not baseline and critical_tests:
            baseline = critical_baseline(critical_tests, read)
        results: list[ProbeResult] = []
        bypass_details: list[dict[str, Any]] = []
        parse_errors = list(declarations)

        if not critical_tests:
            results.append(
                ProbeResult(
                    mutation_id="mut-no-declaration",
                    file="",
                    line=0,
                    test="",
                    kind="none",
                    outcome="SKIPPED",
                    reason=(
                        "no critical tests were declared"
                        + (": " + "; ".join(str(item) for item in parse_errors[:3]) if parse_errors else "")
                    ),
                    declared=False,
                )
            )
            return self._finalize(results, declared, baseline, bypass_details, parse_errors)

        checked_paths: set[str] = set()
        for item in critical_tests:
            base_source = read(item.path, "base")
            candidate_source = read(item.path, "candidate")
            baseline_sha = (baseline.get("files", {}).get(item.path) or {}).get("sha256")
            candidate_sha = (
                hashlib.sha256(candidate_source.encode("utf-8")).hexdigest()
                if candidate_source is not None
                else None
            )
            if item.path not in checked_paths:
                checked_paths.add(item.path)
                bypass = detect_bypass(item.path, base_source, candidate_source, item.test)
                if bypass is not None:
                    bypass["baseline_sha256"] = baseline_sha
                    bypass["candidate_sha256"] = candidate_sha
                    bypass_details.append(bypass)

            common = dict(
                file=item.path,
                test=item.test,
                baseline_sha256=baseline_sha,
                candidate_sha256=candidate_sha,
                bypass_detected=any(entry["path"] == item.path for entry in bypass_details),
            )
            if self.workspace is None:
                results.append(self._skip("no-workspace", item, "the candidate workspace is missing", common))
                continue
            if candidate_source is None:
                results.append(
                    self._skip("no-source", item, "the declared test file is missing in the candidate", common)
                )
                continue
            try:
                tree = ast.parse(candidate_source)
            except SyntaxError as exc:
                results.append(
                    self._skip("unparseable", item, f"the candidate test file does not parse: {exc}", common)
                )
                continue
            function = _find_test_node(tree, item.test)
            if function is None:
                results.append(
                    self._skip("no-test", item, "the declared test function is absent from the candidate", common)
                )
                continue
            assertions = _assertions(function)
            if not assertions:
                results.append(
                    self._skip("no-assertion", item, "the declared test has no assertion to mutate", common)
                )
                continue

            for kind, assertion in _applicable_mutations(assertions, item.mutation):
                if len(results) >= self.max_probes:
                    results.append(
                        self._skip(
                            "probe-cap",
                            item,
                            f"per-run probe cap of {self.max_probes} was reached",
                            common,
                        )
                    )
                    return self._finalize(results, declared, baseline, bypass_details, parse_errors)
                remaining = self.max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    results.append(
                        self._skip(
                            "time-budget",
                            item,
                            f"probe time budget of {self.max_seconds:g}s was exhausted",
                            common,
                        )
                    )
                    return self._finalize(results, declared, baseline, bypass_details, parse_errors)
                results.append(
                    self._run_probe(
                        item=item,
                        kind=kind,
                        line=assertion.lineno,
                        source=candidate_source,
                        timeout=min(self.timeout_seconds, max(1, int(remaining))),
                        **common,
                    )
                )
        return self._finalize(results, declared, baseline, bypass_details, parse_errors)

    def _skip(
        self,
        label: str,
        item: CriticalTest,
        reason: str,
        common: dict[str, Any],
    ) -> ProbeResult:
        return ProbeResult(
            mutation_id=f"mut-skipped-{label}-{hashlib.sha256(item.nodeid.encode()).hexdigest()[:8]}",
            file=common.get("file", item.path),
            line=int(common.get("line", 0)),
            test=common.get("test", item.test),
            kind=common.get("kind", "none"),
            outcome="SKIPPED",
            reason=reason,
            declared=True,
            baseline_sha256=common.get("baseline_sha256"),
            candidate_sha256=common.get("candidate_sha256"),
            bypass_detected=bool(common.get("bypass_detected", False)),
        )

    def _run_probe(
        self,
        *,
        item: CriticalTest,
        kind: str,
        line: int,
        source: str,
        timeout: int,
        file: str,
        test: str,
        baseline_sha256: str | None,
        candidate_sha256: str | None,
        bypass_detected: bool,
    ) -> ProbeResult:
        mutation_id = "mut-" + hashlib.sha256(
            f"{file}|{test}|{kind}|{line}".encode("utf-8")
        ).hexdigest()[:12]
        common = dict(
            file=file,
            test=test,
            kind=kind,
            baseline_sha256=baseline_sha256,
            candidate_sha256=candidate_sha256,
            bypass_detected=bypass_detected,
        )

        def skipped(reason: str) -> ProbeResult:
            return ProbeResult(
                mutation_id=mutation_id,
                line=line,
                outcome="SKIPPED",
                reason=reason,
                **common,
            )

        assert self.workspace is not None
        try:
            mutated_source = _mutated_source(source, test, kind, line)
        except (ValueError, SyntaxError, RecursionError) as exc:
            return skipped(f"the mutation could not be applied: {exc}")

        temp_workspace: Path | None = None
        try:
            temp_workspace = Path(
                tempfile.mkdtemp(prefix="mutation-", dir=str(self.run_home))
            )
            shutil.copytree(
                self.workspace, temp_workspace, dirs_exist_ok=True, ignore=_COPY_IGNORE
            )
            target = temp_workspace / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(mutated_source, encoding="utf-8")
            nodeid = f"{file}::{test}"
            argv = [
                "python",
                "-m",
                "pytest",
                nodeid,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ]
            result = execution.execute_check(
                owner="mutation",
                workspace=temp_workspace,
                constraints=self.constraints,
                run_home=self.run_home / mutation_id,
                argv=argv,
                script=None,
                timeout_seconds=timeout,
            )
        except (MayaError, OSError, ValueError) as exc:
            return skipped(f"the probe could not be started: {exc}")
        finally:
            if temp_workspace is not None:
                shutil.rmtree(temp_workspace, ignore_errors=True)

        if result.blocked:
            return skipped(f"the runner refused the probe: {result.blocked_reason or 'policy'}")
        if result.cancelled:
            return skipped("the probe was cancelled before it produced a verdict")
        if result.timed_out:
            return skipped("the probe timed out before it produced a verdict")
        if result.exit_code in {4, 5} or "no tests ran" in result.stdout.lower():
            return skipped("pytest collected no tests for the declared node id")

        killed = execution.command_outcome(result) == CheckOutcome.FAILED
        outcome = "KILLED" if killed else "SURVIVED"
        reason = (
            "the declared test failed under the mutation"
            if killed
            else "the declared test still passed under the mutation"
        )
        payload = {
            "mutation_id": mutation_id,
            "file": file,
            "line": line,
            "test": test,
            "kind": kind,
            "outcome": outcome,
            "reason": reason,
            "nodeid": f"{file}::{test}",
            "argv": argv,
            "cwd": result.cwd,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "blocked": result.blocked,
            "network_enforcement": result.network_enforcement,
            "limits_enforcement": result.limits_enforcement,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
            "baseline_sha256": baseline_sha256,
            "candidate_sha256": candidate_sha256,
            "bypass_detected": bypass_detected,
            "note": (
                "Property-level probe only: a kill shows this assertion is "
                "load-bearing for one small source-level fault."
            ),
        }
        artifact = self.artifacts.put_text(
            json.dumps(payload, indent=2, sort_keys=True),
            kind=ArtifactKind.TEST_INTEGRITY_REPORT,
            media_type="application/json",
            created_by="mutation-probe",
        )
        return ProbeResult(
            mutation_id=mutation_id,
            line=line,
            outcome=outcome,
            reason=reason,
            artifact_id=artifact.id,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            command=list(argv),
            declared=True,
            **common,
        )

    def _finalize(
        self,
        results: list[ProbeResult],
        declared: list[dict[str, Any]],
        baseline: dict[str, Any],
        bypass_details: list[dict[str, Any]],
        parse_errors: list[Any],
    ) -> MutationResult:
        payload = {
            "results": [item.as_dict() for item in results],
            "declared": declared,
            "baseline": baseline,
            "bypass_details": bypass_details,
            "bypass_detected": bool(bypass_details),
            "parse_errors": [str(item) for item in parse_errors],
            "counts": {
                outcome: sum(1 for item in results if item.outcome == outcome)
                for outcome in PROBE_OUTCOMES
            },
            "max_probes": self.max_probes,
            "max_seconds": self.max_seconds,
            "note": (
                "Mutation is property-level evidence, not semantic proof. "
                "SKIPPED is never a pass; SURVIVED marks a blind spot for "
                "declared critical tests."
            ),
        }
        artifact = self.artifacts.put_text(
            json.dumps(payload, indent=2, sort_keys=True),
            kind=ArtifactKind.TEST_INTEGRITY_REPORT,
            media_type="application/json",
            created_by="mutation-probe",
        )
        return MutationResult(
            results=results,
            baseline=baseline,
            declared=declared,
            bypass_details=bypass_details,
            artifact_id=artifact.id,
        )


__all__ = [
    "CriticalTest",
    "MutationProbe",
    "MutationResult",
    "ProbeResult",
    "critical_baseline",
    "critical_tests_from_specs",
    "detect_bypass",
    "parse_critical_tests",
    "MUTATION_KINDS",
    "PROBE_OUTCOMES",
]

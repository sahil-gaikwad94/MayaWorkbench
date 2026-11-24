"""Deterministic check feedback inside the bounded agent loop.

The implementer may *request* to finish; it may not *decide* that the work
passes. When a task contract declares verification checks, application code
runs those checks against the candidate workspace before a ``finish`` action is
honoured. A real failure becomes typed repair feedback and the same run
continues inside its existing step, tool, token and wall-clock budgets.

Two separations make this trustworthy:

* **Maker and checker are different mechanisms.** The checks are deterministic
  commands the user declared in an immutable contract. No model output is ever
  treated as a verdict, and the model cannot add, edit or silence a check.
* **A broken checker is not a code failure.** pytest exit codes 3, 4, 5 and 6
  mean the checker itself could not produce a verdict (internal error, usage
  error, no tests collected, warning ceiling). Retrying those with a repair
  prompt is pointless, so the loop stops asking the model to repair and records
  the limitation instead. This is the verdict boundary from the harness
  literature: attribute each failure to the artifact or to the apparatus.

In-loop results are advisory and labelled ``in_loop`` everywhere they appear.
The authoritative evidence is still ``maya verify run``, which re-executes
every declared check from scratch after the patch has been captured.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mayaworkbench.domain.enums import CheckKind, CheckOutcome
from mayaworkbench.domain.models import CheckSpec, TaskContract
from mayaworkbench.errors import MayaError
from mayaworkbench.verification import execution
from mayaworkbench.verification.registry import render_invariant

MAX_EXCERPT_CHARS = 3500
MAX_FAILING_TESTS = 12
MAX_ROUND_SECONDS = 900
MAX_CHECKS_PER_ROUND = 12

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)
_PYTEST_HEADER_RE = re.compile(r"^_{3,}\s+(\S+)\s+_{3,}$", re.M)
_PYTEST_SUMMARY_RE = re.compile(
    r"^(?:=+\s*)?(?P<counts>\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings)"
    r"(?:,\s*\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings))*.*)$",
    re.M,
)

#: Tools that only observe. Repeating one of these with identical arguments
#: cannot change the workspace, so it is a progress signal, not a repair attempt.
READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "search_text", "git_status", "git_diff"})


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def redact_text(text: str) -> str:
    """Redact credential-shaped material without importing the tool gateway.

    The pattern set is shared with the gateway; importing it lazily keeps the
    module graph acyclic (the gateway imports this module for ``run_check``).
    """
    from mayaworkbench.workspace.tools import redact

    return redact(text)


def _is_pytest(argv: Iterable[str]) -> bool:
    args = [str(item) for item in argv]
    if not args:
        return False
    executable = Path(args[0]).name
    if executable in {"pytest", "py.test"}:
        return True
    return (
        len(args) >= 3
        and Path(args[0]).name.startswith("python")
        and args[1] == "-m"
        and args[2] == "pytest"
    )


def classify_exit(argv: Iterable[str], exit_code: int | None, output: str = "") -> tuple[bool, str]:
    """Return ``(is_infrastructure_failure, reason)`` for one command check.

    A genuine assertion failure is attributable to the candidate tree. pytest's
    non-test failure codes are not: they mean the command could not check
    anything, so asking the model to "fix the code" would be misleading.
    """
    if exit_code is None:
        return True, "the check produced no exit code"
    if exit_code == 0:
        return False, "command exited 0"
    lowered = output.lower()
    # A checker that cannot import itself, or a command that does not exist,
    # produced no verdict. exit 1 for "No module named pytest" is NOT a test
    # failure; calling it one sends the model into a pointless repair loop.
    broken_markers = (
        "no module named",
        "modulenotfounderror",
        "importerror",
        "command not found",
        "not recognized as an internal or external command",
        "no such file or directory",
        "unrecognized arguments",
        "no such option",
    )
    if any(marker in lowered for marker in broken_markers):
        return True, "the checker could not start: the declared command cannot produce a verdict"
    if exit_code == 127:
        return True, "the checker executable was not found (exit 127)"
    if _is_pytest(argv):
        if "KeyboardInterrupt" in output:
            return True, "the check was interrupted"
        if exit_code == 1:
            return False, "tests were collected and some failed"
        if exit_code == 3:
            return True, "pytest reported an internal error (exit 3); the checker is broken, not the code"
        if exit_code == 4:
            return True, "pytest reported a usage error (exit 4); the declared command cannot run"
        if exit_code == 5:
            return True, "pytest collected no tests (exit 5); the check proves nothing"
        if exit_code == 6:
            return True, "pytest exceeded its warning ceiling (exit 6); tests passed but the run is not clean"
        if exit_code == 2:
            return False, "pytest was interrupted or collection failed (exit 2)"
        return False, f"pytest exited with {exit_code}"
    if exit_code < 0:
        return False, f"the command was killed by signal {-exit_code}"
    return False, f"command exited {exit_code}"


def failing_tests(text: str) -> list[str]:
    """Extract failing/erroring test ids from pytest-style output."""
    found: list[str] = []
    for pattern in (_PYTEST_FAILED_RE, _PYTEST_HEADER_RE):
        for match in pattern.finditer(text):
            candidate = match.group(1).strip()
            if not candidate or len(candidate) > 300:
                continue
            # The FAILURES header repeats the short name of a node id that the
            # short summary spells in full; keep whichever identifies the test
            # more precisely instead of listing the same failure twice.
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
            if len(found) >= MAX_FAILING_TESTS:
                return found
    return found


def failure_excerpt(stdout: str, stderr: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Keep the informative part of a failing command's output.

    The failure section (not the progress dots, not the full traceback of every
    test) is what helps a repair. When no section is recognisable, the head and
    tail are kept and the gap is marked, so a truncated excerpt is never
    mistaken for the complete output.
    """
    text = strip_ansi((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    if not text:
        return ""
    marker = text.find("FAILURES")
    errors = text.find("ERRORS")
    if marker == -1 or (errors != -1 and errors < marker):
        marker = errors
    if marker != -1:
        start = max(0, text.rfind("\n=", 0, marker))
        body = text[start:].strip()
        if len(body) <= limit:
            return body
        return body[:limit] + "\n[excerpt truncated]"
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return text[:head] + "\n[... output omitted ...]\n" + text[-tail:]


def test_summary(text: str) -> str | None:
    matches = _PYTEST_SUMMARY_RE.findall(strip_ansi(text))
    if not matches:
        return None
    line = matches[-1].strip() if isinstance(matches[-1], str) else str(matches[-1])
    return line[:300]


@dataclass
class CheckFeedback:
    """One declared check's outcome, shaped for repair rather than for evidence."""

    key: str
    title: str
    gate: str
    outcome: str
    reason: str
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    duration_ms: int = 0
    infrastructure: bool = False
    failing_tests: list[str] = field(default_factory=list)
    summary_line: str | None = None
    excerpt: str = ""
    skipped: bool = False
    not_run: bool = False

    @property
    def failed(self) -> bool:
        return self.outcome == CheckOutcome.FAILED.value

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "title": self.title,
            "gate": self.gate,
            "outcome": self.outcome,
            "reason": self.reason,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "infrastructure_failure": self.infrastructure,
        }
        if self.failing_tests:
            payload["failing_tests"] = self.failing_tests
        if self.summary_line:
            payload["summary"] = self.summary_line
        if self.skipped:
            payload["skipped"] = True
        if self.not_run:
            payload["not_run"] = True
        if self.excerpt:
            payload["output_excerpt"] = self.excerpt
        return payload


@dataclass
class FeedbackRound:
    results: list[CheckFeedback] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def failures(self) -> list[CheckFeedback]:
        """Failures attributable to the candidate tree, not to the checker."""
        return [item for item in self.results if item.failed and not item.infrastructure]

    @property
    def infrastructure_failures(self) -> list[CheckFeedback]:
        """Checks that could not produce a verdict (including ones we could not run)."""
        return [
            item
            for item in self.results
            if item.infrastructure or item.not_run or item.outcome == CheckOutcome.BLOCKED.value
        ]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(
            item.outcome in {CheckOutcome.PASSED.value, CheckOutcome.SKIPPED.value}
            for item in self.results
        )

    @property
    def actionable(self) -> bool:
        """Failures the model can plausibly repair."""
        return bool(self.failures) and not self.passed

    @property
    def broken_checker(self) -> bool:
        """True when the round produced no verdict because the apparatus failed."""
        return bool(self.results) and not self.passed and not self.failures

    @property
    def verdict(self) -> str:
        if self.passed:
            return "PASSED"
        if self.failures:
            return "FAILED"
        return "BROKEN_CHECKER" if self.broken_checker else "NO_VERDICT"

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "duration_ms": self.duration_ms,
            "checks": [item.to_payload() for item in self.results],
        }


class DeclaredCheckRunner:
    """Execute only the checks the contract declares, through the command policy.

    There is no free-form command here: keys are matched against the immutable
    contract, argv comes from the contract, and the same ``CommandRunner``
    (allowlist, no shell, rlimits, network policy, timeout) is used as for
    verification. ``authorized`` is decided by the caller from a consumed
    ``COMMAND_EXECUTION`` approval, never by the model.
    """

    def __init__(
        self,
        *,
        contract: TaskContract,
        workspace: Path,
        run_home: Path,
        authorized: bool,
        declared: list[CheckSpec] | None = None,
        runtime: Any | None = None,
        require_isolated_runtime: bool = False,
    ) -> None:
        self.contract = contract
        self.workspace = Path(workspace)
        self.run_home = Path(run_home)
        self.authorized = authorized
        self._declared = list(declared if declared is not None else contract.verification_checks)
        # P0-03: the SAME task runtime the implementer used must execute checks.
        # No component may construct a hidden host runner for an isolated task.
        self.runtime = runtime
        self.require_isolated_runtime = require_isolated_runtime

    # ------------------------------------------------------------------ lookup
    @property
    def specs(self) -> list[CheckSpec]:
        return list(self._declared)

    def spec_for(self, key: str) -> CheckSpec | None:
        normalized = key.strip()
        for spec in self._declared:
            if spec.key == normalized:
                return spec
        lowercase = normalized.lower()
        matches = [spec for spec in self._declared if spec.key.lower() == lowercase]
        return matches[0] if len(matches) == 1 else None

    # ----------------------------------------------------------------- execute
    def run(self, keys: list[str] | None = None, *, deadline_seconds: int | None = None) -> FeedbackRound:
        started = time.monotonic()
        budget = deadline_seconds if deadline_seconds is not None else MAX_ROUND_SECONDS
        if keys is None:
            specs = self._declared[:MAX_CHECKS_PER_ROUND]
            skipped_for_cap = self._declared[MAX_CHECKS_PER_ROUND:]
        else:
            selected: list[CheckSpec] = []
            skipped_for_cap = []
            for key in keys:
                spec = self.spec_for(key)
                if spec is not None:
                    selected.append(spec)
            specs = selected
            skipped_for_cap = []
        results: list[CheckFeedback] = []
        for spec in specs:
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                results.append(
                    CheckFeedback(
                        key=spec.key,
                        title=spec.title,
                        gate=spec.gate.value,
                        outcome=CheckOutcome.INCONCLUSIVE.value,
                        reason=(
                            "the in-loop check budget was exhausted before this check ran; "
                            "`maya verify run` will execute it"
                        ),
                        command=list(spec.argv or []),
                        infrastructure=True,
                        not_run=True,
                    )
                )
                continue
            # A single check must not outlive the whole round budget: clamp its
            # timeout to what is left, so wall-clock honesty is not defeated by
            # one slow declared command.
            remaining = max(1, int(budget - elapsed))
            results.append(self._run_one(spec, timeout_cap=remaining))
        for spec in skipped_for_cap:
            results.append(
                CheckFeedback(
                    key=spec.key,
                    title=spec.title,
                    gate=spec.gate.value,
                    outcome=CheckOutcome.INCONCLUSIVE.value,
                    reason=(
                        f"only {MAX_CHECKS_PER_ROUND} checks run in one in-loop round; "
                        "`maya verify run` will execute this one"
                    ),
                    command=list(spec.argv or []),
                    infrastructure=True,
                    not_run=True,
                )
            )
        return FeedbackRound(results=results, duration_ms=int((time.monotonic() - started) * 1000))

    def _run_one(self, spec: CheckSpec, *, timeout_cap: int | None = None) -> CheckFeedback:
        base = CheckFeedback(
            key=spec.key,
            title=spec.title,
            gate=spec.gate.value,
            outcome=CheckOutcome.BLOCKED.value,
            reason="the check did not run",
        )
        if not self.authorized:
            base.reason = (
                "command execution is not authorized for this run, so the declared check "
                "could not run"
            )
            base.infrastructure = True
            base.command = list(spec.argv or [])
            return base
        if self.require_isolated_runtime and self.runtime is None:
            # A trust profile that needs isolation must NEVER be silently
            # downgraded to a host CommandRunner. Type the refusal so the run
            # settles as a broken checker rather than executing on the host.
            base.reason = (
                "the task requires an isolated execution runtime, but no runtime was "
                "injected into the in-loop check runner; the check was not executed"
            )
            base.infrastructure = True
            base.command = list(spec.argv or [])
            return base
        script: str | None = None
        argv = list(spec.argv or [])
        if spec.kind == CheckKind.ML_INVARIANT:
            try:
                if spec.invariant is None:
                    raise ValueError("invariant check has no specification")
                script = render_invariant(spec.invariant)
                argv = ["python", "<generated invariant script>"]
            except (MayaError, ValueError) as exc:
                base.reason = f"the invariant script could not be generated: {exc}"
                base.infrastructure = True
                return base
        elif not argv:
            base.reason = "the declared command check has no argv"
            base.infrastructure = True
            return base
        timeout = min(spec.timeout_seconds, self.contract.constraints.max_runtime_seconds)
        if timeout_cap is not None:
            timeout = min(timeout, timeout_cap)
        timeout = max(1, timeout)
        try:
            result = execution.execute_check(
                owner="in-loop",
                workspace=self.workspace,
                constraints=self.contract.constraints,
                run_home=self.run_home,
                argv=None if script is not None else argv,
                script=script,
                timeout_seconds=timeout,
                runtime=self.runtime,
            )
        except (MayaError, OSError) as exc:
            base.reason = f"the check could not be started: {exc}"
            base.infrastructure = True
            base.command = argv
            return base
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        outcome = (
            execution.invariant_outcome(result)
            if script is not None
            else execution.command_outcome(result)
        )
        infrastructure = False
        if result.blocked:
            reason = result.blocked_reason or "the command was refused by policy"
            infrastructure = True
        elif result.cancelled:
            reason = "the check was cancelled"
            infrastructure = True
        elif result.timed_out:
            reason = f"the check timed out after {result.duration_ms} ms"
            infrastructure = False
        elif outcome == CheckOutcome.PASSED:
            reason = execution.outcome_reason(result)
        elif script is not None:
            reason = execution.invariant_reason(result)
            marker = execution.invariant_marker(result)
            if marker is None:
                infrastructure = True
                reason = "the invariant script printed no verdict marker"
        else:
            infrastructure, reason = classify_exit(argv, result.exit_code, combined)
        excerpt = "" if outcome == CheckOutcome.PASSED else failure_excerpt(result.stdout, result.stderr)
        return CheckFeedback(
            key=spec.key,
            title=spec.title,
            gate=spec.gate.value,
            outcome=outcome.value,
            reason=reason,
            command=argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            infrastructure=infrastructure,
            failing_tests=[] if script is not None else failing_tests(combined),
            summary_line=test_summary(combined),
            excerpt=redact_text(excerpt),
            skipped=outcome == CheckOutcome.SKIPPED,
        )


def repair_payload(
    round_result: FeedbackRound,
    *,
    round_number: int,
    max_rounds: int,
) -> dict[str, Any]:
    """The typed message appended for a repair step.

    Only failing and infrastructure checks are included: a passing check adds
    noise, and the model already knows which checks the contract declares.
    """
    checks = [
        item.to_payload()
        for item in round_result.results
        if item.failed or item.infrastructure or item.not_run or item.outcome == CheckOutcome.BLOCKED.value
    ]
    return {
        "kind": "REPAIR_REQUIRED",
        "round": round_number,
        "max_rounds": max_rounds,
        "checks": checks,
        "instructions": [
            "These are deterministic checks declared in the task contract, executed by the "
            "application. They are not a model opinion and cannot be waived.",
            "Diagnose the cause from the output above, make the smallest correct change inside "
            "the allowed paths, and do not weaken, skip or rewrite the check to pass.",
            "If a failure is pre-existing and unrelated to this task, say so explicitly in "
            "limitations and finish rather than masking it.",
            "Do not repeat an action whose observation is already in the ledger.",
        ],
    }


__all__ = [
    "CheckFeedback",
    "FeedbackRound",
    "DeclaredCheckRunner",
    "classify_exit",
    "failing_tests",
    "failure_excerpt",
    "test_summary",
    "repair_payload",
    "strip_ansi",
    "redact_text",
    "READ_ONLY_TOOLS",
]

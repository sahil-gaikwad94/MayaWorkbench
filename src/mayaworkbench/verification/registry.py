"""Deterministic check registry and ML invariant templates (Phase 4).

A check is either:

* **built in** — implemented in this package and always run when applicable
  (scope, approvals, budgets, patch reproducibility, test integrity, report
  integrity);
* **declared** — a command or an ML invariant pinned in the task contract, so
  the user knows before the run what will be executed.

Nothing here trusts a model to decide whether its own work passed. Invariant
templates generate a real script, which runs through the same command policy as
any other command; a template that cannot run (missing dependency, unusable
target) is reported as ``SKIPPED``, never as a pass.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from mayaworkbench.domain.enums import (
    CheckKind,
    VerificationGate,
    VerificationMethod,
)
from mayaworkbench.domain.models import CheckSpec, InvariantSpec
from mayaworkbench.errors import UsageError


@dataclass(frozen=True)
class BuiltinCheck:
    key: str
    gate: VerificationGate
    title: str
    method: VerificationMethod
    description: str
    required: bool = True


BUILTIN_CHECKS: tuple[BuiltinCheck, ...] = (
    BuiltinCheck(
        key="scope.integrity",
        gate=VerificationGate.SCOPE,
        title="Changes stay inside the approved scope",
        method=VerificationMethod.SOURCE_INSPECTION,
        description=(
            "Verifies the captured diff has no path outside the contract, no "
            "credential-shaped path, and that the user's checkout did not drift."
        ),
    ),
    BuiltinCheck(
        key="approvals.integrity",
        gate=VerificationGate.SCOPE,
        title="Required approvals were consumed for this exact contract",
        method=VerificationMethod.STATIC_ANALYSIS,
        description=(
            "Recomputes the write and command specification hashes and requires a "
            "consumed approval for each action the contract requires."
        ),
    ),
    BuiltinCheck(
        key="budget.integrity",
        gate=VerificationGate.SCOPE,
        title="Run budgets were not exceeded",
        method=VerificationMethod.STATIC_ANALYSIS,
        description=(
            "Compares recorded steps, tool calls, tokens, cost and wall time with "
            "the contract limits. A run that blew a budget cannot be accepted as "
            "if it had respected it."
        ),
    ),
    BuiltinCheck(
        key="snapshot.integrity",
        gate=VerificationGate.SCOPE,
        title="Evidence names the pinned snapshot and base commit",
        method=VerificationMethod.STATIC_ANALYSIS,
        description=(
            "Requires the run, contract and diff to agree on snapshot and base "
            "commit, and the snapshot to be indexed."
        ),
    ),
    BuiltinCheck(
        key="diff.integrity",
        gate=VerificationGate.CHANGE,
        title="The candidate patch is reviewable and contains no blocking findings",
        method=VerificationMethod.SOURCE_INSPECTION,
        description=(
            "Checks for an empty patch, secret material, deleted tests, excluded "
            "paths and structural review errors. Dependency changes are warnings, "
            "not silent failures."
        ),
    ),
    BuiltinCheck(
        key="patch.reproducible",
        gate=VerificationGate.CHANGE,
        title="The captured patch reproduces the candidate workspace",
        method=VerificationMethod.STATIC_ANALYSIS,
        description=(
            "Creates a fresh worktree at the base commit, applies the stored patch "
            "with git, and compares the changed files byte for byte with the "
            "workspace the checks ran in."
        ),
    ),
    BuiltinCheck(
        key="test.integrity",
        gate=VerificationGate.REVIEW,
        title="Tests were not silently weakened",
        method=VerificationMethod.STATIC_ANALYSIS,
        description=(
            "AST comparison of test files and test configuration: deleted tests, "
            "added skips/xfails, reduced assertions, tautologies, inflated "
            "tolerances and disabled collection. Findings are warnings about "
            "detectable weakening, never proof of semantic equivalence."
        ),
    ),
    BuiltinCheck(
        key="experiment.evidence",
        gate=VerificationGate.EXPERIMENT,
        title="A fair experiment supports the claimed metric effect",
        method=VerificationMethod.EXPERIMENT,
        required=False,
        description=(
            "Finds the latest fair comparison attached to the task, reports its "
            "verdict and primary-metric interval, and treats it as required "
            "evidence only when the contract sets require_experiment. A fair "
            "comparison that favours the baseline is a failure only then; "
            "otherwise it is a warning that downgrades the decision."
        ),
    ),
    BuiltinCheck(
        key="evaluation.integrity",
        gate=VerificationGate.CHANGE,
        title="Evaluation-critical files match the frozen pinned snapshot",
        method=VerificationMethod.SOURCE_INSPECTION,
        description=(
            "Declared evaluation paths are hashed against the pinned snapshot. An "
            "unapproved change is EVAL_TAMPERING; an approved treatment must name "
            "the exact path and candidate hash."
        ),
    ),
    BuiltinCheck(
        key="report.integrity",
        gate=VerificationGate.REPORT,
        title="Every reported success links to recorded evidence",
        method=VerificationMethod.STATIC_ANALYSIS,
        description=(
            "Verifies that each check outcome maps to a stored check, each "
            "criterion maps to checks, and the patch hash named by the report is "
            "the patch that was verified."
        ),
    ),
)

BUILTIN_KEYS = frozenset(check.key for check in BUILTIN_CHECKS)


# --------------------------------------------------------------- invariant templates
INVARIANT_NAMES: tuple[str, ...] = (
    "forward_smoke",
    "shape_preserved",
    "gradients_finite",
    "no_nan_output",
    "causal_mask_invariance",
    "deterministic_eval",
    "parameter_update",
    "dtype_device_consistency",
    "softmax_row_sum",
    "causal_future_independence",
    "permutation_equivariance",
    "finite_difference_gradient",
    "normalization_moments",
    "conservation_of_sum",
    "reduced_precision_stability",
    "gradient_flow_contract",
    "dataset_split_parity",
    "seed_determinism",
)

INVARIANT_DESCRIPTIONS: dict[str, str] = {
    "forward_smoke": "Instantiate the target, run one forward pass and require finite output.",
    "shape_preserved": "Require the output shape to equal the input shape.",
    "gradients_finite": "Backpropagate and require every trainable parameter to have a finite gradient.",
    "no_nan_output": "Require the forward output to contain no NaN or infinity.",
    "causal_mask_invariance": "Changing the final input position must not change earlier outputs.",
    "deterministic_eval": "In eval mode, two identical forward passes must be numerically equal.",
    "parameter_update": "One optimisation step must change at least one parameter and keep parameters finite.",
    "dtype_device_consistency": "The output dtype and device must match the input.",
    "reduced_precision_stability": (
        "The same tiny target built in float32 and a declared reduced dtype stays "
        "within a precision-aware tolerance; reports the maximum deviation."
    ),
    "gradient_flow_contract": (
        "Every trainable parameter receives a finite gradient after a tiny backward "
        "pass, except explicitly declared no-gradient parameter names."
    ),
    "dataset_split_parity": (
        "Declared train/val index splits do not overlap by index hash and the "
        "preprocessing callable is identical on a shared sample."
    ),
    "seed_determinism": (
        "Two seeded forward passes on identical tiny inputs are bitwise equal, or "
        "within an explicitly declared tolerance; declared nondeterministic ops skip."
    ),
    **__import__(
        "mayaworkbench.verification.math_invariants", fromlist=["METHOD_DESCRIPTIONS"]
    ).METHOD_DESCRIPTIONS,
}

_PRELUDE = r'''
import importlib
import importlib.util
import json
import os
import sys

PARAMS = json.loads(__PARAMS__)


def skip(message):
    print("SKIP: " + str(message))
    print(json.dumps({"status": "SKIP", "reason": str(message)}))
    sys.exit(2)


def fail(message):
    print("FAIL: " + str(message))
    print(json.dumps({"status": "FAIL", "reason": str(message)}))
    sys.exit(1)


def passed(details):
    print(json.dumps({"status": "PASS", "details": details}))


try:
    import torch
except Exception as exc:  # noqa: BLE001 - dependency absence is a skip, not a failure
    skip("torch is not importable: %s" % (exc,))


def load_target():
    module_ref = PARAMS["module"]
    if module_ref.endswith(".py"):
        if not os.path.exists(module_ref):
            skip("target file does not exist: " + module_ref)
        spec = importlib.util.spec_from_file_location("maya_invariant_target", module_ref)
        if spec is None or spec.loader is None:
            skip("cannot load target file: " + module_ref)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        sys.path.insert(0, os.getcwd())
        try:
            module = importlib.import_module(module_ref)
        except Exception as exc:  # noqa: BLE001
            skip("cannot import %s: %s" % (module_ref, exc))
    name = PARAMS.get("callable")
    if not name:
        skip("no callable was declared for the invariant")
    if not hasattr(module, name):
        skip("module %s has no attribute %s" % (module_ref, name))
    factory = getattr(module, name)
    kwargs = PARAMS.get("parameters") or {}
    try:
        target = factory(**kwargs)
    except TypeError as exc:
        skip("factory could not be called with the declared parameters: %s" % (exc,))
    except Exception as exc:  # noqa: BLE001
        fail("target construction raised %s: %s" % (type(exc).__name__, exc))
    if hasattr(target, "eval"):
        target.eval()
    if not callable(target):
        skip("the declared target is not callable")
    torch.manual_seed(int(PARAMS["seed"]))
    dtype = getattr(torch, PARAMS["dtype"])
    shape = tuple(int(value) for value in PARAMS["input_shape"])
    x = torch.randn(*shape, dtype=dtype)
    return target, x


def call(target, tensor):
    try:
        return target(tensor)
    except Exception as exc:  # noqa: BLE001
        fail("forward pass raised %s: %s" % (type(exc).__name__, exc))


def require_tensor(value):
    if not hasattr(value, "shape") or not hasattr(value, "dtype"):
        fail("output is not tensor-like: %s" % (type(value).__name__,))
'''


def _script(body: str, spec: InvariantSpec) -> str:
    params = {
        "module": spec.module,
        "callable": spec.callable,
        "input_shape": spec.input_shape,
        "dtype": spec.dtype,
        "seed": spec.seed,
        "parameters": spec.parameters,
        "invariant": spec.name,
    }
    try:
        rendered = json.dumps(params, sort_keys=True)
    except TypeError as exc:
        raise UsageError(
            f"Invariant parameters must be JSON-serialisable: {exc}",
            hint="Parameters become the target factory's keyword arguments.",
        ) from exc
    return _PRELUDE.replace("__PARAMS__", repr(rendered)) + "\n" + body.strip() + "\n"


def _forward_smoke(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
with torch.no_grad():
    y = call(target, x)
require_tensor(y)
if not bool(torch.isfinite(y).all()):
    fail("output contains NaN or infinite values")
passed({"input_shape": list(x.shape), "output_shape": list(y.shape), "dtype": str(y.dtype)})
""",
        spec,
    )


def _shape_preserved(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
with torch.no_grad():
    y = call(target, x)
require_tensor(y)
if tuple(y.shape) != tuple(x.shape):
    fail("output shape %s does not match input shape %s" % (tuple(y.shape), tuple(x.shape)))
if not bool(torch.isfinite(y).all()):
    fail("output contains NaN or infinite values")
passed({"shape": list(y.shape)})
""",
        spec,
    )


def _no_nan_output(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
with torch.no_grad():
    y = call(target, x)
require_tensor(y)
if not torch.is_floating_point(y):
    pass
bad = int((~torch.isfinite(y)).sum().item())
if bad:
    fail("output contains %d non-finite values" % bad)
passed({"non_finite": 0, "numel": int(y.numel())})
""",
        spec,
    )


def _gradients_finite(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
y = call(target, x)
require_tensor(y)
if not torch.is_floating_point(y):
    fail("output is not floating point; a gradient check is not meaningful")
out = y.float().sum()
if not bool(torch.isfinite(out)):
    fail("loss is not finite before backward")
params = [(name, p) for name, p in target.named_parameters()] if hasattr(target, "named_parameters") else []
trainable = [(name, p) for name, p in params if getattr(p, "requires_grad", False)]
if not trainable:
    skip("target has no trainable parameters")
try:
    out.backward()
except Exception as exc:  # noqa: BLE001
    fail("backward raised %s: %s" % (type(exc).__name__, exc))
missing = [name for name, p in trainable if p.grad is None]
if missing:
    fail("no gradient was produced for: %s" % ", ".join(missing[:10]))
bad = [name for name, p in trainable if not bool(torch.isfinite(p.grad).all())]
if bad:
    fail("non-finite gradient for: %s" % ", ".join(bad[:10]))
passed({"trainable_parameters": len(trainable), "non_finite_gradients": 0})
""",
        spec,
    )


def _causal_mask_invariance(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
if x.dim() != 3:
    skip("causal invariance expects a (batch, sequence, features) input")
if x.shape[0] != 1:
    skip("causal invariance requires a batch size of 1")
if x.shape[1] < 2:
    skip("causal invariance requires at least two sequence positions")
with torch.no_grad():
    y1 = call(target, x)
    x2 = x.clone()
    x2[:, -1, :] = x2[:, -1, :] + 1.0
    y2 = call(target, x2)
require_tensor(y1)
if y1.shape[1] != x.shape[1]:
    skip("output sequence length differs from the input; the check assumes one output per position")
delta = (y1[:, :-1, :] - y2[:, :-1, :]).abs().max().item()
if delta > 1e-5:
    fail("future tokens changed earlier outputs by %.6g" % delta)
passed({"max_earlier_delta": delta, "positions_checked": int(x.shape[1]) - 1})
""",
        spec,
    )


def _deterministic_eval(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
with torch.no_grad():
    y1 = call(target, x)
    y2 = call(target, x)
require_tensor(y1)
if not torch.equal(y1, y2):
    fail("two identical eval-mode forward passes differ")
passed({"identical": True})
""",
        spec,
    )


def _parameter_update(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
params = [(name, p) for name, p in target.named_parameters()] if hasattr(target, "named_parameters") else []
trainable = [(name, p) for name, p in params if getattr(p, "requires_grad", False)]
if not trainable:
    skip("target has no trainable parameters")
before = [p.detach().clone() for _, p in trainable]
y = call(target, x)
require_tensor(y)
loss = y.float().sum()
try:
    loss.backward()
    optimiser = torch.optim.SGD([p for _, p in trainable], lr=0.01)
    optimiser.step()
except Exception as exc:  # noqa: BLE001
    fail("optimisation step raised %s: %s" % (type(exc).__name__, exc))
changed = sum(1 for old, (_, p) in zip(before, trainable) if not torch.equal(old, p.detach()))
if changed == 0:
    fail("no parameter changed after one optimisation step")
bad = [name for name, p in trainable if not bool(torch.isfinite(p).all())]
if bad:
    fail("non-finite parameter after the step: %s" % ", ".join(bad[:10]))
passed({"changed_parameters": changed, "trainable_parameters": len(trainable)})
""",
        spec,
    )


def _dtype_device_consistency(spec: InvariantSpec) -> str:
    return _script(
        """
target, x = load_target()
with torch.no_grad():
    y = call(target, x)
require_tensor(y)
if y.dtype != x.dtype:
    fail("output dtype %s differs from input dtype %s" % (y.dtype, x.dtype))
if hasattr(y, "device") and hasattr(x, "device") and y.device != x.device:
    fail("output device %s differs from input device %s" % (y.device, x.device))
passed({"dtype": str(y.dtype), "device": str(getattr(y, "device", "n/a"))})
""",
        spec,
    )


_TEMPLATE_BUILDERS: dict[str, Callable[[InvariantSpec], str]] = {
    "forward_smoke": _forward_smoke,
    "shape_preserved": _shape_preserved,
    "gradients_finite": _gradients_finite,
    "no_nan_output": _no_nan_output,
    "causal_mask_invariance": _causal_mask_invariance,
    "deterministic_eval": _deterministic_eval,
    "parameter_update": _parameter_update,
    "dtype_device_consistency": _dtype_device_consistency,
    **{
        name: __import__(
            "mayaworkbench.verification.math_invariants", fromlist=["builder_for"]
        ).builder_for(name)
        for name in __import__(
            "mayaworkbench.verification.math_invariants", fromlist=["METHOD_INVARIANTS"]
        ).METHOD_INVARIANTS
    },
}


def render_invariant(spec: InvariantSpec) -> str:
    """Render a self-contained script for one invariant specification."""
    builder = _TEMPLATE_BUILDERS.get(spec.name)
    if builder is None:
        raise UsageError(
            f"Unknown invariant template: {spec.name}",
            hint="Run `maya verify checks` for the available templates.",
        )
    if not spec.callable:
        raise UsageError(
            f"Invariant {spec.name} requires a callable",
            hint="Pass callable=<name> with the factory or torch.nn.Module class.",
        )
    return builder(spec)


def invariant_argv(spec: InvariantSpec) -> list[str]:
    return ["python", "-c", render_invariant(spec)]


# ------------------------------------------------------------------- declarations
def command_check(
    command: str,
    *,
    key: str | None = None,
    criteria: list[str] | None = None,
    timeout_seconds: int = 300,
    required: bool = True,
    title: str | None = None,
) -> CheckSpec:
    """Build a COMMAND check from a shell-like string the user typed.

    The string is split once with :mod:`shlex` into an argv array. It is never
    executed through a shell; quoting rules are the user's way of expressing
    arguments, not a scripting surface.
    """
    argv = shlex.split(command, posix=True)
    if not argv:
        raise UsageError("A check command cannot be empty")
    chosen_key = key or (
        "command." + hashlib.sha256("\x00".join(argv).encode("utf-8")).hexdigest()[:10]
    )
    return CheckSpec(
        key=chosen_key,
        gate=VerificationGate.FUNCTIONAL,
        kind=CheckKind.COMMAND,
        title=title or command,
        required=required,
        argv=argv,
        timeout_seconds=timeout_seconds,
        criteria=criteria or [],
    )


def invariant_check(
    name: str,
    *,
    module: str,
    callable_name: str | None,
    input_shape: list[int] | None = None,
    dtype: str = "float32",
    seed: int = 0,
    parameters: dict[str, Any] | None = None,
    criteria: list[str] | None = None,
    key: str | None = None,
    required: bool = True,
    timeout_seconds: int = 300,
) -> CheckSpec:
    spec = InvariantSpec(
        name=name,
        module=module,
        callable=callable_name,
        input_shape=input_shape or [1, 4, 32],
        dtype=dtype,
        seed=seed,
        parameters=parameters or {},
    )
    return CheckSpec(
        key=key or f"invariant.{name}",
        gate=VerificationGate.ML_INVARIANT,
        kind=CheckKind.ML_INVARIANT,
        title=f"ML invariant: {name}",
        required=required,
        invariant=spec,
        timeout_seconds=timeout_seconds,
        criteria=criteria or [],
    )


def registry_summary() -> list[dict[str, Any]]:
    """Machine-readable registry for `maya verify checks`."""
    return [
        {
            "key": check.key,
            "gate": check.gate.value,
            "title": check.title,
            "method": check.method.value,
            "required": check.required,
            "description": check.description,
            "kind": "BUILTIN",
        }
        for check in BUILTIN_CHECKS
    ] + [
        {
            "key": f"invariant.{name}",
            "gate": VerificationGate.ML_INVARIANT.value,
            "title": f"ML invariant template: {name}",
            "method": VerificationMethod.NUMERICAL_INVARIANT.value,
            "required": False,
            "description": INVARIANT_DESCRIPTIONS[name],
            "kind": "INVARIANT_TEMPLATE",
        }
        for name in INVARIANT_NAMES
    ]


def proposed_checks(
    *,
    has_tests: bool,
    has_python: bool,
    pytest_available: bool,
) -> list[CheckSpec]:
    """Suggest checks for a repository; suggestions authorize nothing.

    The caller shows these to the user, who may add them to a task contract.
    """
    proposals: list[CheckSpec] = []
    if has_tests and pytest_available:
        proposals.append(
            command_check(
                "pytest -q",
                key="tests.pytest",
                title="Run the repository test suite",
                timeout_seconds=900,
            )
        )
    if has_python:
        proposals.append(
            command_check(
                "python -m compileall -q .",
                key="python.compileall",
                title="Compile every Python module",
                timeout_seconds=300,
                required=False,
            )
        )
    return proposals


__all__ = [
    "BUILTIN_CHECKS",
    "BUILTIN_KEYS",
    "INVARIANT_NAMES",
    "INVARIANT_DESCRIPTIONS",
    "BuiltinCheck",
    "render_invariant",
    "invariant_argv",
    "command_check",
    "invariant_check",
    "registry_summary",
    "proposed_checks",
]

"""Method-specific mathematical invariant templates (WP9).

These are *property probes*, not proofs of arbitrary model implementations. Each
script is self-contained, generated from validated parameters, and executed under
the same command policy as other checks. No model output is trusted, and no
external dependency is required: torch tensors are converted with ``tolist()``
when present, otherwise plain Python sequences are accepted.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from mayaworkbench.domain.models import InvariantSpec

METHOD_INVARIANTS: tuple[str, ...] = (
    "softmax_row_sum",
    "causal_future_independence",
    "permutation_equivariance",
    "finite_difference_gradient",
    "normalization_moments",
    "conservation_of_sum",
)

METHOD_DESCRIPTIONS: dict[str, str] = {
    "softmax_row_sum": "Each output row of a softmax sums to one within tolerance.",
    "causal_future_independence": "Changing a future input cannot change earlier outputs.",
    "permutation_equivariance": "Permuting inputs permutes outputs identically.",
    "finite_difference_gradient": "Analytic gradient matches a central finite difference.",
    "normalization_moments": "Normalized rows have near-zero mean and near-unit variance.",
    "conservation_of_sum": "The method preserves the input total within tolerance.",
}

_PRELUDE = r'''
import importlib.util
import json
import math
import os
import sys

PARAMS = __PARAMS__


def _emit(status, **details):
    print(json.dumps({"status": status, **details}, sort_keys=True))
    sys.exit(0 if status == "PASS" else 1)


def _skip(message):
    print("SKIP: " + str(message))
    print(json.dumps({"status": "SKIP", "reason": str(message)}))
    sys.exit(2)


def _rows(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        return [[float(value)]]
    if value and isinstance(value[0], (int, float)):
        return [[float(x) for x in value]]
    return [[float(x) for x in row] for row in value]


def _load():
    path = PARAMS["module"]
    if not os.path.isfile(path):
        _skip("target module is unavailable: " + str(path))
    spec = importlib.util.spec_from_file_location("maya_invariant_target", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["maya_invariant_target"] = module
    spec.loader.exec_module(module)
    name = PARAMS.get("callable")
    fn = getattr(module, name, None) if name else None
    if fn is None:
        _skip("callable not found: " + str(name))
    return fn


def _call(fn, value):
    return _rows(fn(value))


TOL = float(PARAMS.get("tolerance", 1e-4))
'''


def render_method_invariant(name: str, spec: InvariantSpec) -> str:
    """Render one method-specific probe as a self-contained Python script."""
    if name not in METHOD_INVARIANTS:
        raise ValueError(f"Unknown method invariant: {name}")
    params: dict[str, Any] = {
        "module": spec.module,
        "callable": spec.callable,
        "tolerance": float(spec.parameters.get("tolerance", 1e-4)),
    }
    params.update(spec.parameters)
    body = _PRELUDE + "\n" + _BODIES[name] + "\n"
    return body.replace("__PARAMS__", json.dumps(params, sort_keys=True))


_BODIES: dict[str, str] = {
    "softmax_row_sum": r'''
fn = _load()
sample = [[0.1, 2.0, -1.0], [3.0, 1.0, 0.0]]
out = _call(fn, sample)
if len(out) != len(sample) or any(len(row) != len(sample[0]) for row in out):
    _emit("FAIL", reason="shape changed")
bad = [abs(sum(row) - 1.0) for row in out]
if max(bad) > TOL * 10:
    _emit("FAIL", reason="rows do not sum to one", deviations=bad)
_emit("PASS", method="softmax_row_sum", max_deviation=max(bad))
''',
    "causal_future_independence": r'''
fn = _load()
base = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
changed = [[1.0, 2.0, 3.0], [4.0, 5.0, 777.0]]
a, b = _call(fn, base), _call(fn, changed)
if len(a) != len(b) or any(len(x) != len(y) for x, y in zip(a, b)):
    _emit("FAIL", reason="shape changed")
for index, (left, right) in enumerate(zip(a[:-1], b[:-1])):
    if any(abs(x - y) > TOL for x, y in zip(left, right)):
        _emit("FAIL", reason="future input changed an earlier output", row=index)
_emit("PASS", method="causal_future_independence")
''',
    "permutation_equivariance": r'''
fn = _load()
values = [1.0, 2.0, 3.0, 4.0]
order = [2, 0, 3, 1]
base = _call(fn, values)[0]
permuted = _call(fn, [values[i] for i in order])[0]
for position, source in enumerate(order):
    if abs(permuted[position] - base[source]) > TOL:
        _emit("FAIL", reason="output did not follow input permutation", position=position)
_emit("PASS", method="permutation_equivariance")
''',
    "finite_difference_gradient": r'''
fn = _load()
target = PARAMS.get("function")
if not target:
    _skip("finite_difference_gradient requires parameters.function")
grad_fn = getattr(sys.modules["maya_invariant_target"], target, None)
if grad_fn is None:
    _skip("gradient function not found: " + str(target))
x = float(PARAMS.get("at", 0.5))
h = float(PARAMS.get("step", 1e-3))
analytic = float(grad_fn(x))
numeric = (float(fn(x + h)) - float(fn(x - h))) / (2 * h)
if abs(analytic - numeric) > max(TOL, 1e-2):
    _emit("FAIL", reason="gradient mismatch", analytic=analytic, numeric=numeric)
_emit("PASS", method="finite_difference_gradient", delta=abs(analytic - numeric))
''',
    "normalization_moments": r'''
fn = _load()
out = _call(fn, [[1.0, 2.0, 3.0, 4.0]])[0]
mean = sum(out) / len(out)
variance = sum((x - mean) ** 2 for x in out) / len(out)
if abs(mean) > max(TOL * 10, 1e-2) or abs(variance - 1.0) > 0.2:
    _emit("FAIL", reason="normalization moments outside tolerance", mean=mean, variance=variance)
_emit("PASS", method="normalization_moments", mean=mean, variance=variance)
''',
    "conservation_of_sum": r'''
fn = _load()
sample = [[1.0, -2.0, 3.0, 4.0]]
before = sum(sum(row) for row in sample)
after = sum(sum(row) for row in _call(fn, sample))
if abs(after - before) > TOL * 10:
    _emit("FAIL", reason="total changed", before=before, after=after)
_emit("PASS", method="conservation_of_sum", total=after)
''',
}


def builder_for(name: str) -> Callable[[InvariantSpec], str]:
    return lambda spec: render_method_invariant(name, spec)


__all__ = [
    "METHOD_INVARIANTS",
    "METHOD_DESCRIPTIONS",
    "render_method_invariant",
    "builder_for",
]

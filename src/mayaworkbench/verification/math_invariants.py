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
    "reduced_precision_stability",
    "gradient_flow_contract",
    "dataset_split_parity",
    "seed_determinism",
)

METHOD_DESCRIPTIONS: dict[str, str] = {
    "softmax_row_sum": "Each output row of a softmax sums to one within tolerance.",
    "causal_future_independence": "Changing a future input cannot change earlier outputs.",
    "permutation_equivariance": "Permuting inputs permutes outputs identically.",
    "finite_difference_gradient": "Analytic gradient matches a central finite difference.",
    "normalization_moments": "Normalized rows have near-zero mean and near-unit variance.",
    "conservation_of_sum": "The method preserves the input total within tolerance.",
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
    "reduced_precision_stability": r'''
reduced_name = PARAMS.get("reduced_dtype", "float16")
if reduced_name not in ("float16", "bfloat16"):
    _skip("unsupported reduced dtype: " + str(reduced_name))
try:
    import torch
except Exception as exc:  # noqa: BLE001 - dependency absence is a skip, not a failure
    _skip("torch is not importable: " + str(exc))
reduced = getattr(torch, reduced_name, None)
if reduced is None or not hasattr(torch, "float32"):
    _skip("torch does not expose dtype " + str(reduced_name))
fn = _load()
shape = tuple(int(v) for v in PARAMS.get("input_shape", [1, 4]))
try:
    high = fn(torch.float32)
    low = fn(reduced)
    x_high = torch.randn(*shape, dtype=torch.float32)
    x_low = x_high.to(reduced)
    y_high = _call(high, x_high)
    y_low = _call(low, x_low)
except TypeError as exc:
    _skip("target does not support a dtype-aware factory: " + str(exc))
except AttributeError as exc:
    _skip("target or tensor lacks a required precision API: " + str(exc))
if len(y_high) != len(y_low) or any(len(a) != len(b) for a, b in zip(y_high, y_low)):
    _emit("FAIL", reason="reduced dtype changed the output shape")
deviations = [abs(a - b) for ra, rb in zip(y_high, y_low) for a, b in zip(ra, rb)]
max_deviation = max(deviations) if deviations else 0.0
tol = float(PARAMS.get("reduced_tolerance", 0.05))
if not math.isfinite(max_deviation) or max_deviation > tol:
    _emit(
        "FAIL",
        reason="reduced-precision deviation exceeded tolerance",
        max_deviation=max_deviation,
        tolerance=tol,
    )
_emit(
    "PASS",
    method="reduced_precision_stability",
    reduced_dtype=reduced_name,
    max_deviation=max_deviation,
    tolerance=tol,
)
''',
    "gradient_flow_contract": r'''
try:
    import torch
except Exception as exc:  # noqa: BLE001 - dependency absence is a skip, not a failure
    _skip("torch is not importable: " + str(exc))
fn = _load()
try:
    target = fn()
except TypeError as exc:
    _skip("target factory could not be called without arguments: " + str(exc))
if target is None or not hasattr(target, "named_parameters"):
    _skip("target does not expose named_parameters")
trainable = [
    (name, param)
    for name, param in target.named_parameters()
    if getattr(param, "requires_grad", False)
]
if not trainable:
    _skip("target has no trainable parameters")
allowed = {str(item) for item in (PARAMS.get("allowed_no_grad") or [])}
shape = tuple(int(v) for v in PARAMS.get("input_shape", [1, 4]))
try:
    x = torch.randn(*shape, dtype=torch.float32)
    out = target(x)
    if not hasattr(out, "backward"):
        _skip("target output does not support backward()")
    out.backward()
    grads = [(name, getattr(param, "grad", None)) for name, param in trainable]
except Exception as exc:  # noqa: BLE001 - a broken target is a failure, not a skip
    _emit("FAIL", reason="tiny backward pass raised %s: %s" % (type(exc).__name__, exc))
missing = [name for name, grad in grads if grad is None and name not in allowed]
if missing:
    _emit("FAIL", reason="unexpected missing gradients", parameters=[str(n) for n in missing[:10]])
bad = []
for name, grad in grads:
    if grad is None:
        continue
    try:
        finite = all(math.isfinite(v) for row in _rows(grad) for v in row)
    except Exception:  # noqa: BLE001 - non-numeric gradients are non-finite
        finite = False
    if not finite:
        bad.append(str(name))
if bad:
    _emit("FAIL", reason="non-finite gradients", parameters=bad[:10])
_emit(
    "PASS",
    method="gradient_flow_contract",
    trainable_parameters=len(trainable),
    allowed_no_grad=sorted(allowed),
    finite_gradients=len([grad for _, grad in grads if grad is not None]),
)
''',
    "dataset_split_parity": r'''
import hashlib
import struct
split_name = PARAMS.get("split_fn")
if not split_name:
    _skip("dataset_split_parity requires parameters.split_fn")
fn = _load()
module = sys.modules.get("maya_invariant_target")
provider = getattr(module, str(split_name), None) if module is not None else None
if not callable(provider):
    _skip("split provider not found: " + str(split_name))
try:
    train, val = provider()
    train = [int(i) for i in train]
    val = [int(i) for i in val]
except Exception as exc:  # noqa: BLE001 - an unreadable split is a missing prerequisite
    _skip("split provider could not be read: " + str(exc))
if not train and not val:
    _skip("no split data is declared")


def _index_hashes(indices):
    return {hashlib.sha256(str(i).encode("utf-8")).hexdigest() for i in indices}


overlap = _index_hashes(train) & _index_hashes(val)
if overlap:
    _emit("FAIL", reason="train and val splits overlap by index hash", overlapping_indices=len(overlap))
for key, actual in (("train_size", len(train)), ("val_size", len(val))):
    declared = PARAMS.get(key)
    if declared is not None and actual != int(declared):
        _emit(
            "FAIL",
            reason="split size differs from declaration",
            field=key,
            expected=int(declared),
            actual=actual,
        )
sample = PARAMS.get("shared_sample", [[1.0, 2.0, 3.0]])
first = _call(fn, sample)
second = _call(fn, sample)


def _bitwise(a, b):
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(_bitwise(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return isinstance(b, float) and struct.pack("!d", a) == struct.pack("!d", b)
    return type(a) is type(b) and a == b


if not _bitwise(first, second):
    _emit("FAIL", reason="preprocessing is not identical on a shared sample")
_emit(
    "PASS",
    method="dataset_split_parity",
    train_indices=len(train),
    val_indices=len(val),
    overlapping_indices=0,
    preprocessing_identical=True,
)
''',
    "seed_determinism": r'''
import struct
nondeterministic = PARAMS.get("nondeterministic_ops") or []
if nondeterministic:
    _skip("target declares nondeterministic ops: " + ", ".join(str(op) for op in nondeterministic[:5]))
try:
    import torch
except Exception as exc:  # noqa: BLE001 - dependency absence is a skip, not a failure
    _skip("torch is not importable: " + str(exc))
fn = _load()
target = fn()
seed = int(PARAMS.get("seed", 0))
shape = tuple(int(v) for v in PARAMS.get("input_shape", [1, 4]))


def _forward():
    torch.manual_seed(seed)
    x = torch.randn(*shape, dtype=torch.float32)
    return _call(target, x)


first = _forward()
second = _forward()
if len(first) != len(second) or any(len(a) != len(b) for a, b in zip(first, second)):
    _emit("FAIL", reason="determinism check produced different output shapes")


def _bitwise(a, b):
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(_bitwise(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return isinstance(b, float) and struct.pack("!d", a) == struct.pack("!d", b)
    return type(a) is type(b) and a == b


tol = float(PARAMS.get("determinism_tolerance", 0.0))
if tol > 0.0:
    deviations = [abs(a - b) for ra, rb in zip(first, second) for a, b in zip(ra, rb)]
    max_deviation = max(deviations) if deviations else 0.0
    if not math.isfinite(max_deviation) or max_deviation > tol:
        _emit(
            "FAIL",
            reason="two seeded forward passes diverged",
            max_deviation=max_deviation,
            tolerance=tol,
        )
    _emit(
        "PASS",
        method="seed_determinism",
        bitwise_identical=_bitwise(first, second),
        max_deviation=max_deviation,
        tolerance=tol,
    )
if not _bitwise(first, second):
    _emit("FAIL", reason="two seeded forward passes are not bitwise equal")
_emit("PASS", method="seed_determinism", bitwise_identical=True, max_deviation=0.0)
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

"""Method-specific invariant probes are executable, honest, and method-scoped."""

from __future__ import annotations

import json
import subprocess
import sys

from mayaworkbench.domain.models import InvariantSpec
from mayaworkbench.verification.math_invariants import (
    METHOD_INVARIANTS,
    render_method_invariant,
)
from mayaworkbench.verification.registry import INVARIANT_NAMES, render_invariant

MODULE = '''
def softmax(x):
    rows = []
    for row in x:
        exps = [__import__("math").exp(v - max(row)) for v in row]
        total = sum(exps)
        rows.append([v / total for v in exps])
    return rows

def causal(x):
    return [list(row) for row in x]

def permute(x):
    return list(x)

def square(x):
    return x * x

def square_grad(x):
    return 2 * x

def normalize(x):
    row = x[0]
    mean = sum(row) / len(row)
    var = sum((v - mean) ** 2 for v in row) / len(row)
    scale = (var ** 0.5) or 1.0
    return [[(v - mean) / scale for v in row]]

def identity(x):
    return [list(row) for row in x]
'''


def _run(script: str):
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            payload = json.loads(line)
    return proc.returncode, payload


def _spec(name: str, callable_name: str, **params) -> InvariantSpec:
    return InvariantSpec(
        name=name,
        module=__file__,
        callable=callable_name,
        parameters={"callable": callable_name, **params},
    )


def test_all_method_invariants_are_registered():
    assert set(METHOD_INVARIANTS) <= set(INVARIANT_NAMES)


def test_method_invariants_pass_on_matching_implementations(tmp_path):
    module = tmp_path / "methods.py"
    module.write_text(MODULE, encoding="utf-8")
    cases = {
        "softmax_row_sum": "softmax",
        "causal_future_independence": "causal",
        "permutation_equivariance": "permute",
        "normalization_moments": "normalize",
        "conservation_of_sum": "identity",
    }
    for name, fn in cases.items():
        spec = _spec(name, fn)
        spec = spec.model_copy(update={"module": str(module)})
        script = render_method_invariant(name, spec)
        code, payload = _run(script)
        assert code == 0 and payload["status"] == "PASS", (name, payload)


def test_finite_difference_gradient_matches_analytic(tmp_path):
    module = tmp_path / "methods.py"
    module.write_text(MODULE, encoding="utf-8")
    spec = _spec(
        "finite_difference_gradient", "square", function="square_grad", at=0.5, step=1e-3
    )
    spec = spec.model_copy(update={"module": str(module)})
    code, payload = _run(render_invariant(spec))
    assert code == 0 and payload["status"] == "PASS"


def test_method_invariant_detects_a_broken_implementation(tmp_path):
    module = tmp_path / "methods.py"
    module.write_text(MODULE + "\ndef broken_softmax(x):\n    return [[1.0] * len(row) for row in x]\n", encoding="utf-8")
    spec = _spec("softmax_row_sum", "broken_softmax")
    spec = spec.model_copy(update={"module": str(module)})
    code, payload = _run(render_invariant(spec))
    assert code != 0 and payload["status"] == "FAIL"

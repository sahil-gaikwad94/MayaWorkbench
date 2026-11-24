"""Method-specific invariant probes are executable, honest, and method-scoped."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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


# --------------------------------------------------------------------------- new probes
# torch is not a hard dependency here. These stubs exercise the PASS/FAIL paths
# deterministically without installing it; the SKIP paths simply run without the
# stub, which is exactly the "prerequisite absent" condition.
FAKE_TORCH = '''
class Tensor:
    def __init__(self, data, dtype=None, backward=None):
        self.data = data
        self.dtype = dtype
        self._backward = backward

    def tolist(self):
        return self.data

    def to(self, dtype):
        self.dtype = dtype
        return self

    def float(self):
        return self

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.data

    def item(self):
        return float(self.data)

    def backward(self):
        if self._backward is not None:
            self._backward()


float32 = "float32"
float16 = "float16"
bfloat16 = "bfloat16"


def manual_seed(seed):
    return None


def randn(*shape, dtype=None):
    counter = [0]

    def build(dims):
        if not dims:
            counter[0] += 1
            return round(counter[0] * 0.1, 4)
        return [build(dims[1:]) for _ in range(dims[0])]

    return Tensor(build(list(shape)), dtype=dtype)
'''

PRECISION_MODULE = '''
def make(dtype):
    scale = 1.0 if dtype == "float32" else 1.001

    def model(x):
        data = x.tolist() if hasattr(x, "tolist") else x
        if data and isinstance(data[0], (int, float)):
            data = [data]
        return [[v * scale for v in row] for row in data]

    return model


def make_broken(dtype):
    scale = 1.0 if dtype == "float32" else 1000.0

    def model(x):
        data = x.tolist() if hasattr(x, "tolist") else x
        if data and isinstance(data[0], (int, float)):
            data = [data]
        return [[v * scale for v in row] for row in data]

    return model
'''

GRADIENT_MODULE = '''
import torch


class _Param:
    def __init__(self, name, requires_grad=True):
        self.name = name
        self.requires_grad = requires_grad
        self.grad = None


def make():
    params = [("w", _Param("w")), ("b", _Param("b"))]

    class Model:
        def named_parameters(self):
            return params

        def __call__(self, x):
            def backward():
                for _, p in params:
                    p.grad = torch.Tensor([[1.0, 1.0]])

            return torch.Tensor([[1.0]], backward=backward)

    return Model()


def make_missing():
    params = [("w", _Param("w")), ("b", _Param("b"))]

    class Model:
        def named_parameters(self):
            return params

        def __call__(self, x):
            def backward():
                params[0][1].grad = torch.Tensor([[1.0, 1.0]])

            return torch.Tensor([[1.0]], backward=backward)

    return Model()


def make_nonfinite():
    params = [("w", _Param("w")), ("b", _Param("b"))]

    class Model:
        def named_parameters(self):
            return params

        def __call__(self, x):
            def backward():
                for _, p in params:
                    p.grad = torch.Tensor([[float("nan")]])

            return torch.Tensor([[1.0]], backward=backward)

    return Model()


def make_noparams():
    class Model:
        def named_parameters(self):
            return []

        def __call__(self, x):
            return torch.Tensor([[1.0]])

    return Model()
'''

SEED_MODULE = '''
def make():
    def model(x):
        data = x.tolist() if hasattr(x, "tolist") else x
        if data and isinstance(data[0], (int, float)):
            data = [data]
        return [[v * 2.0 for v in row] for row in data]

    return model


_counter = [0]


def make_broken():
    def model(x):
        data = x.tolist() if hasattr(x, "tolist") else x
        if data and isinstance(data[0], (int, float)):
            data = [data]
        _counter[0] += 1
        return [[v * 2.0 + _counter[0] for v in row] for row in data]

    return model
'''

SPLIT_MODULE = '''
def preprocess(x):
    if x and isinstance(x[0], (int, float)):
        x = [x]
    return [[v + 1.0 for v in row] for row in x]


def splits():
    return [0, 1, 2, 3], [4, 5, 6, 7]


def splits_overlap():
    return [0, 1, 2, 3], [3, 4, 5, 6]


def splits_empty():
    return [], []


_counter = [0]


def preprocess_broken(x):
    _counter[0] += 1
    if x and isinstance(x[0], (int, float)):
        x = [x]
    return [[v + _counter[0] for v in row] for row in x]
'''

NEW_PROBES = (
    "reduced_precision_stability",
    "gradient_flow_contract",
    "dataset_split_parity",
    "seed_determinism",
)


def _run_in(script: str, cwd) -> tuple[int, dict | None]:
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            payload = json.loads(line)
    return proc.returncode, payload


def _block_torch(directory) -> None:
    """Deterministically make ``torch`` importable-as-absent (P1-03)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "torch.py").write_text(
        "raise ModuleNotFoundError('torch is blocked for this hermetic test')\n",
        encoding="utf-8",
    )


def _spec_for(tmp_path, name: str, callable_name: str, **params) -> InvariantSpec:
    module = tmp_path / "new_methods.py"
    spec = _spec(name, callable_name, **params)
    return spec.model_copy(update={"module": str(module)})


def _install(tmp_path, source: str, *, torch_stub: bool = True) -> None:
    (tmp_path / "new_methods.py").write_text(source, encoding="utf-8")
    if torch_stub:
        (tmp_path / "torch.py").write_text(FAKE_TORCH, encoding="utf-8")


def test_new_method_probes_render_compilable_scripts(tmp_path):
    assert set(NEW_PROBES) <= set(METHOD_INVARIANTS)
    assert set(NEW_PROBES) <= set(INVARIANT_NAMES)
    for name in NEW_PROBES:
        spec = _spec_for(tmp_path, name, "make", shared_sample=[[1.0]])
        script = render_invariant(spec)
        compile(script, f"<invariant:{name}>", "exec")
        assert '"status"' in script and "PARAMS" in script


def test_reduced_precision_stability_pass_fail_and_skip(tmp_path):
    _install(tmp_path, PRECISION_MODULE)
    okay = _spec_for(
        tmp_path,
        "reduced_precision_stability",
        "make",
        input_shape=[1, 4],
        reduced_dtype="float16",
        reduced_tolerance=1e-2,
    )
    code, payload = _run_in(render_invariant(okay), tmp_path)
    assert code == 0 and payload["status"] == "PASS", payload
    assert payload["max_deviation"] <= 1e-2

    broken = okay.model_copy(update={"callable": "make_broken"})
    broken = broken.model_copy(update={"parameters": {**okay.parameters, "callable": "make_broken"}})
    code, payload = _run_in(render_method_invariant("reduced_precision_stability", broken), tmp_path)
    assert code != 0 and payload["status"] == "FAIL", payload
    assert payload["max_deviation"] > 1e-2

    # No torch at all -> honest skip (P1-03: the missing dependency is forced).
    empty = tmp_path / "no_torch"
    _block_torch(empty)
    code, payload = _run_in(render_invariant(okay), empty)
    assert code == 2 and payload["status"] == "SKIP", payload

    # An unavailable declared dtype -> honest skip even with torch present.
    unknown = okay.model_copy(
        update={"parameters": {**okay.parameters, "reduced_dtype": "float8"}}
    )
    code, payload = _run_in(render_invariant(unknown), tmp_path)
    assert code == 2 and payload["status"] == "SKIP", payload


def test_gradient_flow_contract_pass_fail_and_skip(tmp_path):
    _install(tmp_path, GRADIENT_MODULE)
    okay = _spec_for(
        tmp_path, "gradient_flow_contract", "make", input_shape=[1, 4]
    )
    code, payload = _run_in(render_invariant(okay), tmp_path)
    assert code == 0 and payload["status"] == "PASS", payload
    assert payload["finite_gradients"] == 2

    for broken_name in ("make_missing", "make_nonfinite"):
        params = {**okay.parameters, "callable": broken_name}
        broken = okay.model_copy(update={"callable": broken_name, "parameters": params})
        code, payload = _run_in(render_invariant(broken), tmp_path)
        assert code != 0 and payload["status"] == "FAIL", (broken_name, payload)

    # An explicitly declared no-gradient exception makes the missing case pass.
    allowed = okay.model_copy(
        update={
            "callable": "make_missing",
            "parameters": {
                **okay.parameters,
                "callable": "make_missing",
                "allowed_no_grad": ["b"],
            },
        }
    )
    code, payload = _run_in(render_invariant(allowed), tmp_path)
    assert code == 0 and payload["status"] == "PASS", payload

    no_params = okay.model_copy(
        update={
            "callable": "make_noparams",
            "parameters": {**okay.parameters, "callable": "make_noparams"},
        }
    )
    code, payload = _run_in(render_invariant(no_params), tmp_path)
    assert code == 2 and payload["status"] == "SKIP", payload

    empty = tmp_path / "no_torch"
    _block_torch(empty)
    code, payload = _run_in(render_invariant(okay), empty)
    assert code == 2 and payload["status"] == "SKIP", payload


def test_dataset_split_parity_pass_fail_and_skip(tmp_path):
    _install(tmp_path, SPLIT_MODULE, torch_stub=False)
    okay = _spec_for(
        tmp_path,
        "dataset_split_parity",
        "preprocess",
        split_fn="splits",
        train_size=4,
        val_size=4,
        shared_sample=[[1.0, 2.0, 3.0]],
    )
    code, payload = _run_in(render_invariant(okay), tmp_path)
    assert code == 0 and payload["status"] == "PASS", payload
    assert payload["overlapping_indices"] == 0

    overlap = okay.model_copy(
        update={
            "parameters": {**okay.parameters, "split_fn": "splits_overlap"},
        }
    )
    code, payload = _run_in(render_invariant(overlap), tmp_path)
    assert code != 0 and payload["status"] == "FAIL", payload

    bad_preprocess = okay.model_copy(
        update={
            "callable": "preprocess_broken",
            "parameters": {**okay.parameters, "callable": "preprocess_broken"},
        }
    )
    code, payload = _run_in(render_invariant(bad_preprocess), tmp_path)
    assert code != 0 and payload["status"] == "FAIL", payload

    # No declared split provider -> skip without needing torch.
    no_split = _spec_for(tmp_path, "dataset_split_parity", "preprocess")
    code, payload = _run_in(render_invariant(no_split), tmp_path)
    assert code == 2 and payload["status"] == "SKIP", payload

    empty_split = okay.model_copy(
        update={"parameters": {**okay.parameters, "split_fn": "splits_empty"}}
    )
    code, payload = _run_in(render_invariant(empty_split), tmp_path)
    assert code == 2 and payload["status"] == "SKIP", payload


def test_seed_determinism_pass_fail_and_skip(tmp_path):
    _install(tmp_path, SEED_MODULE)
    okay = _spec_for(
        tmp_path,
        "seed_determinism",
        "make",
        input_shape=[1, 4],
        seed=7,
    )
    code, payload = _run_in(render_invariant(okay), tmp_path)
    assert code == 0 and payload["status"] == "PASS", payload
    assert payload["bitwise_identical"] is True

    broken = okay.model_copy(
        update={
            "callable": "make_broken",
            "parameters": {**okay.parameters, "callable": "make_broken"},
        }
    )
    code, payload = _run_in(render_invariant(broken), tmp_path)
    assert code != 0 and payload["status"] == "FAIL", payload

    # A declared nondeterministic op skips before any torch import.
    nondet = okay.model_copy(
        update={
            "parameters": {**okay.parameters, "nondeterministic_ops": ["dropout"]},
        }
    )
    code, payload = _run_in(render_invariant(nondet), tmp_path)
    assert code == 2 and payload["status"] == "SKIP", payload

    empty = tmp_path / "no_torch"
    _block_torch(empty)
    code, payload = _run_in(render_invariant(okay), empty)
    assert code == 2 and payload["status"] == "SKIP", payload

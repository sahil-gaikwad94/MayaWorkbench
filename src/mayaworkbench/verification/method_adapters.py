"""Method-specific validation adapters: claim/equation -> executable probe.

These are *advisory* mappings from research claims and detected equations to
the deterministic property probes the verification registry already implements.
They never assign a verdict: the backend still runs the declared check and
records its outcome. A suggestion whose target probe does not exist is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from mayaworkbench.verification.registry import INVARIANT_NAMES


@dataclass(frozen=True)
class MethodAdapter:
    name: str
    trigger_terms: tuple[str, ...]
    claim_kinds: tuple[str, ...]
    check_name: str
    obligation_kind: str
    rationale: str


DEFAULT_ADAPTERS: tuple[MethodAdapter, ...] = (
    MethodAdapter("softmax", ("softmax", "row sum", "normalize"),
                  ("PAPER_METHOD", "VALIDATION_CLAIM"), "softmax_row_sum",
                  "NUMERICAL_INVARIANT", "softmax rows must sum to one"),
    MethodAdapter("causal-mask", ("causal", "triangular mask", "future position"),
                  ("PAPER_METHOD",), "causal_mask_invariance",
                  "NUMERICAL_INVARIANT", "future tokens must not affect earlier outputs"),
    MethodAdapter("permutation", ("permutation", "equivarian", "order independent"),
                  ("PAPER_METHOD",), "permutation_equivariance",
                  "NUMERICAL_INVARIANT", "permutation should not change results"),
    MethodAdapter("finite-difference", ("gradient", "finite difference", "derivative"),
                  ("PAPER_METHOD", "PAPER_RESULT"), "finite_difference_gradient",
                  "GRADIENT_CONTRACT", "analytic and numerical gradients should agree"),
    MethodAdapter("normalization", ("layer norm", "batch norm", "normalization", "moments"),
                  ("PAPER_METHOD",), "normalization_moments",
                  "NUMERICAL_INVARIANT", "normalized activations have expected moments"),
    MethodAdapter("conservation", ("conserv", "sum to", "mass balance"),
                  ("PAPER_METHOD", "VALIDATION_CLAIM"), "conservation_of_sum",
                  "NUMERICAL_INVARIANT", "a conserved quantity should remain constant"),
    MethodAdapter("seed", ("seed", "determinism", "reproducible"),
                  ("REPRODUCTION_REQUIREMENT", "VALIDATION_CLAIM"), "seed_determinism",
                  "DETERMINISM", "the same seed should produce the same result"),
    MethodAdapter("precision", ("fp16", "float16", "reduced precision", "bf16"),
                  ("PAPER_METHOD",), "reduced_precision_stability",
                  "NUMERICAL_INVARIANT", "reduced precision should stay within tolerance"),
)


def suggest_adapters(
    claims: Iterable[dict[str, Any]],
    *,
    equations: Iterable[dict[str, Any]] = (),
    text: str = "",
) -> list[dict[str, Any]]:
    """Return deterministic, deduplicated probe suggestions with rationale."""
    claim_rows = list(claims)
    equation_rows = list(equations)
    haystacks: list[tuple[str, str, str]] = []
    for claim in claim_rows:
        haystacks.append((
            str(claim.get("statement", "") or " ".join(claim.get("statement_fragments", []))),
            str(claim.get("kind", "")),
            str(claim.get("id", "")),
        ))
    for item in equation_rows:
        haystacks.append((str(item.get("text", "")), "PAPER_METHOD", "equation"))
    if text:
        haystacks.append((text, "PAPER_METHOD", "document"))

    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for adapter in DEFAULT_ADAPTERS:
        if adapter.check_name not in INVARIANT_NAMES:
            continue
        matched: list[str] = []
        for body, kind, source in haystacks:
            lowered = body.lower()
            kind_ok = not adapter.claim_kinds or kind in adapter.claim_kinds
            if kind_ok and any(term in lowered for term in adapter.trigger_terms):
                matched.append(source)
        if not matched:
            continue
        if adapter.name in seen:
            continue
        seen.add(adapter.name)
        suggestions.append({
            "adapter": adapter.name,
            "check_name": adapter.check_name,
            "obligation_kind": adapter.obligation_kind,
            "rationale": adapter.rationale,
            "matched_sources": sorted(set(matched))[:10],
            "verdict_authority": "backend",
        })
    return suggestions


__all__ = ["MethodAdapter", "DEFAULT_ADAPTERS", "suggest_adapters"]

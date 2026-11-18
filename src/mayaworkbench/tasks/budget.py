"""One token and cost ledger shared by every model role in a task run.

The planner and the implementer both spend against the same contract. Reserving
before a call is what keeps a single request from overshooting the remaining
budget, and treating an *unknown* price as unknown (never as zero) is what keeps
``max_model_cost_usd=0`` meaning "no paid usage".
"""

from __future__ import annotations

from typing import Any

from mayaworkbench.domain.models import TaskConstraints
from mayaworkbench.errors import BudgetError

MIN_OUTPUT_TOKENS = 64
#: Conservative prompt-token estimate used only for reservation, never billing.
CHARS_PER_TOKEN = 3.6


class BudgetController:
    def __init__(
        self,
        *,
        constraints: TaskConstraints,
        run_id: str,
        runs: Any,
    ):
        self.constraints = constraints
        self.run_id = run_id
        self.runs = runs

    # ------------------------------------------------------------------ state
    def current(self) -> tuple[int, float]:
        run = self.runs.get(self.run_id)
        return int(run.model_tokens or 0), float(run.model_cost_usd or 0.0)

    def estimate_input_tokens(self, text: str) -> int:
        return max(1, int(len(text) / CHARS_PER_TOKEN))

    # ---------------------------------------------------------------- reserve
    def reserve(
        self,
        *,
        estimated_input_tokens: int,
        desired_output_tokens: int,
        cost_known: bool,
        input_price_per_mtok: float | None = None,
        output_price_per_mtok: float | None = None,
    ) -> int:
        """Return the output allowance for the next call or raise.

        Raises :class:`BudgetError` when the remaining token budget cannot cover
        the estimated input plus a usable output, when the monetary budget is
        exhausted, or when the price is unknown and no explicit override allows
        unknown spend.
        """
        constraints = self.constraints
        tokens_used, cost_used = self.current()
        remaining = constraints.max_model_tokens - tokens_used
        allowance = remaining - max(0, estimated_input_tokens)
        if remaining <= 0 or allowance < MIN_OUTPUT_TOKENS:
            raise BudgetError(
                "The remaining model-token budget cannot cover another request",
                hint=(
                    f"Used {tokens_used} of {constraints.max_model_tokens} tokens; "
                    "raise max_model_tokens or start a narrower task."
                ),
            )
        max_output = min(desired_output_tokens, allowance)

        if not cost_known:
            if not constraints.allow_unknown_cost:
                raise BudgetError(
                    "The model's price is not configured, so the task cost budget cannot be "
                    "enforced; refusing a paid call with an unknown cost",
                    hint=(
                        "Set model.input_cost_per_mtok and model.output_cost_per_mtok, or "
                        "explicitly set allow_unknown_cost on the task contract."
                    ),
                )
            return max_output

        if input_price_per_mtok is not None and output_price_per_mtok is not None:
            estimated_cost = (
                estimated_input_tokens * input_price_per_mtok
                + max_output * output_price_per_mtok
            ) / 1_000_000
            limit = constraints.max_model_cost_usd
            if cost_used + estimated_cost > limit:
                raise BudgetError(
                    "The task's monetary budget cannot cover another model request",
                    hint=(
                        f"Estimated ${estimated_cost:.6f} more with ${cost_used:.6f} already "
                        f"spent against a ${limit:.6f} limit."
                    ),
                )
        return max_output


__all__ = ["BudgetController", "MIN_OUTPUT_TOKENS", "CHARS_PER_TOKEN"]

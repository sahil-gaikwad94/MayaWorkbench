# ADR 0004 — Provider-neutral model gateway with policy before I/O

Status: accepted (Phase 1)

## Context

Model access must be optional, auditable and revocable. Key risks:

- a hosted provider being called by default, leaking source code;
- provider SDK types leaking into domain logic;
- cost and token usage being unrecorded or invented;
- secrets ending up in config, logs or error messages;
- the model being allowed to validate its own claims.

## Decision

A single `ModelGateway` sits between QA and any provider:

1. **Policy first.** `LOCAL_ONLY` (default) refuses before an adapter is
   constructed. Refusals are recorded as invocations with status `REFUSED` and
   `error_class=policy_refused`, plus a `MODEL_INVOCATION_FAILED` event.
2. **Narrow interface.** `ModelProvider.generate(request) -> ModelResponse` and
   `capabilities()`. No SDK objects cross the boundary; the only implementation
   is an OpenAI-compatible HTTP adapter using stdlib `urllib`.
3. **Keys only from the environment.** `model.api_key_env` names the variable;
   the value is never written to config, logs, events, invocations or records.
4. **Structured output with local validation.** The adapter requests a JSON
   schema (falling back to JSON mode when unsupported); the response is parsed
   and validated locally. Invalid output is discarded.
5. **Full accounting, no prompts.** Each invocation stores provider, model,
   purpose, tokens, latency, retries, provider request id and cost only when
   prices were supplied (with `cost_basis` describing the basis).
6. **Deterministic verification outside the model.** Citations are validated by
   code against the exact context pack; the model cannot overrule a failed
   citation. Any invalid citation withholds the answer.

## Consequences

- Positives: adding a provider is a small adapter; privacy default is enforced
  in one place; costs and failures are visible; a compromised model cannot
  fabricate evidence.
- Negatives: only one adapter exists today; structured-output support varies by
  provider and falls back to JSON mode; no streaming and no token-exact
  budgeting without a provider tokenizer.
- Revisit if: multiple providers or local runtimes are needed (add adapters and
  capability negotiation), or if streaming becomes a UX requirement (would need
  a partial-citation protocol that still verifies before presentation).

# ADR 0017 — Provider-correct model adapters and hybrid streaming

**Date:** 2026-11-23
**Status:** Accepted
**Scope:** `models/` (OpenAI-compatible, Anthropic, Gemini), gateway, live harness.
**Supersedes:** the adapter-specific parts of ADR 0004, not its policy boundary.

## Context

The three adapters were unit-tested only for shapes they invented themselves.
Reading the 2026 provider references exposed contract mismatches that would fail
against a real endpoint:

- OpenAI `strict` JSON Schema is not ordinary JSON Schema: every object must set
  `additionalProperties: false` and list every property in `required`.
  Reasoning models reject `max_tokens` (use `max_completion_tokens`) and
  non-default sampling parameters; usage is absent from a stream unless
  `stream_options.include_usage` is requested.
- Anthropic's JSON output moved to the GA `output_config.format` field. Prompt
  caching allows at most four explicit breakpoints and tool results must be
  consecutive blocks in one user message. A `refusal` stop reason is HTTP 200,
  billed, and may not satisfy the schema.
- Gemini structured output needs both `responseMimeType: application/json` and
  a projected OpenAPI-subset `responseSchema`; a `functionResponse` is matched
  by **function name**, not by a caller-supplied id; `promptFeedback.blockReason`
  and blocked `finishReason` values mean there is no answer to parse.

The gateway also required a key even for a keyless loopback model server, counted
only message text against the input bound, and recorded the configured retry
ceiling rather than the attempts that happened.

## Decision

1. **Project schemas instead of trusting them.** `models/schema.py` inlines local
   `$ref`s, closes objects and requires every property for OpenAI strict mode,
   and projects Gemini's upper-case OpenAPI subset. A schema that cannot be
   projected degrades explicitly (`json_schema` → `json_object` → prompt-only,
   or `schema` → `mime` → prompt), never silently.
2. **Adapt after an observed rejection.** Parameter compatibility is not guessed
   from a model-name list. A 400 naming `max_tokens`, `temperature`,
   `parallel_tool_calls`, `stream_options` or a response format changes exactly
   that setting and retries, within a bounded adaptation budget.
3. **Every adapter streams twice.** `generate(stream=True)` returns one assembled
   response; `stream()` yields incremental text deltas and a final response with
   usage and tool calls (`is_final=True`). SSE framing and limits live in
   `models/sse.py`, not in each adapter.
4. **Typed failures are not answers.** Refusal, quota, context-length,
   content-filter and protocol errors are non-retryable `ModelError` classes with
   safe HTTP status/provider code metadata. A bounded `Retry-After` (30 s ceiling)
   is honoured; an unbounded provider delay is not.
5. **The gateway reports what happened.** It resolves the adapter's own key
   variable, permits a keyless loopback endpoint under `LOCAL_ONLY`, counts tool
   schemas and schemas in the input bound, and records actual attempts.
6. **Usage stays honest.** Cached prompt tokens and reasoning tokens are separate
   fields; when no cache price is configured the cost basis says cached tokens
   are priced at the input rate (an upper bound), rather than inventing prices.

## Consequences

- More requests in the worst case while an adapter learns a provider's quirks,
  bounded by `_MAX_ADAPTATIONS` and never after streaming output has been
  delivered to the caller.
- `capabilities().streaming` is now `True` for all three adapters because the
  incremental protocol is implemented. The live gate exercises the streaming
  transport when declared.
- No provider SDK is added; the stdlib transport remains the only dependency.
- No provider quality, tool-reliability or latency claim is licensed. All
  evidence is wire-shape evidence against fake transports until the opt-in live
  gate runs with a key.

## Evidence

| Check | Result |
|---|---|
| `tests/test_provider_adapters.py` | 20 tests: schema projection, OpenAI adaptation/degradation/retry-after/usage, Anthropic cache/tool merge/refusal/SSE, Gemini schema/function names/blocks/stream, gateway key/attempts/input bound |
| `tests/test_model_platform.py` | updated for strict schema projection, streaming capabilities, Anthropic SSE and Gemini SSE |
| Full suite | `docs/reports/test-run-2026-11-23.json` — 834 collected, 827 passed, 7 skipped, 0 failed |
| Live provider gate | **not run** — `MAYA_LIVE_MODEL` unset and no provider key in this environment |

## Remaining

Run `docs/release-gates.md` §4 once with each provider key (or a keyless local
endpoint) and record the result. Until then the adapter status is
`IMPLEMENTED`/`UNIT_TESTED`, not `LIVE_VERIFIED`.

# MayaWorkbench wiki

Dated decisions and tribal knowledge. This is a historical record, not a claim about
current code; each code claim cites the commit or date it was true.

- [Build history](build-history.md) — upgrade packages implemented 2026-11-21, what was
  verified, and what remains deliberately absent.
- [Execution trust decisions](execution-trust.md) — why `RLIMIT_NPROC` was removed, why
  isolated profiles fail closed, and what a container backend does and does not prove.
- [Performance measurements](performance.md) — cold vs warm incremental indexing numbers
  and the cache invalidation strategy.
- [Engineering Workbench refined direction](../ideas/engineering-workbench.md) — 2026-11-21
  sharpened full-product scope, assumptions, and explicit "Not Doing" list.
- ADRs 0012–0014 in `../adr/` — dated decisions on execution trust, the index cache, and
  multi-language adapters.
- [Rigorous end-to-end audit](../audit-end-to-end.md) — 2026-11-21 fresh-eyes review,
  seven defects found and fixed, 82/82 adversarial probes, mid-repository flow.
- [2026-11-22 audit remediation](audit-2026-11-22-remediation.md) — working plan and
  dated decisions for the two 2026-11-21/22 audit fix packages and the R2I evidence package.
- [R2I evidence package spec](../spec-r2i-evidence-package.md) — four-tier reproducibility
  reporting and offline evaluation design, shipped 2026-11-22.
- [Launch-readiness spec](../spec-launch-readiness.md) and [release gates](../release-gates.md)
  — release evidence and opt-in live integration harnesses (2026-11-22).
- [Evaluation-integrity spec](../spec-evaluation-integrity.md) — frozen harness,
  extra method probes and the formal-constraint plugin boundary (2026-11-22).
- [Workbench UI spec](../spec-workbench-ui.md), [Research evidence quality spec](../spec-research-evidence-quality.md),
  [Signed run bundles spec](../spec-signed-run-bundles.md), [Real-model eval spec](../spec-real-model-eval.md),
  [Parallel discovery spec](../spec-parallel-discovery.md), [LSP client spec](../spec-lsp-client.md),
  [MLflow connector spec](../spec-mlflow-connector.md) — final audit packages (2026-11-22).
- [2026-11-23 audit remediation](audit-2026-11-23-remediation.md) — per-finding status,
  evidence and remaining work for the Trustworthy Runtime + Verification package
  (sanitized snapshot, evidence-based capability probe, one runtime for the whole
  lifecycle, baseline parity, CPU split, hermetic tests, local API contract).
- [ADR 0015](../adr/0015-trustworthy-runtime-and-verification-release.md) — the dated
  decision record for that package and the product-freeze reconciliation.
- [ADR 0016](../adr/0016-container-owned-sessions-and-environment-provisioning.md) —
  container-owned sessions (retained container, per-session marker, generated
  terminator) and the immutable environment spec/provisioner with its network gate
  and truthful inventory.
- [ADR 0017](../adr/0017-model-provider-hardening.md) — provider-correct structured
  output, parameter adaptation, cache/tool semantics, hybrid streaming and honest
  failure/usage accounting for the OpenAI-compatible, Anthropic and Gemini adapters.
- [Release evidence 2026-11-23](../reports/test-run-2026-11-23.json) — machine-readable
  full-suite record (834 collected, 827 passed, 7 skipped, 0 failed) with the JUnit XML
  beside it.

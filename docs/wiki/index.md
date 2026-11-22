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

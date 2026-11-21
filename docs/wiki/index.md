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

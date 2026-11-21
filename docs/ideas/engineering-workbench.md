# Engineering Workbench — refined direction (2026-11-21)

## Problem Statement
How might we hand an engineer a difficult, multi-resource objective and return a
**verifiable outcome** — not just a plausible answer — without asking them to trust
a model's self-report or to grant it unbounded access to their machine?

## Recommended Direction
Keep the evidence-first wedge: pin the source, retrieve grounded context, propose a
typed plan, execute only through policy-gated tools in an isolated runtime, and let
deterministic checks assign the terminal status. Expand the unit of context from a
single repository to a **Workspace** holding repositories, datasets, documents,
environments and evidence, while keeping one orchestrator with role-specific typed
handoffs. Breadth comes from adding support for more environments behind the *same*
auditable lifecycle — never from granting more autonomy or a general shell.

Measured evidence for the current direction: a 120-file repository re-index dropped from
1531 ms to 246 ms with content-hash caching (`docs/wiki/performance.md`), and the full
suite went from 328 collected / 2 failing to 380 passing — reliability improved while
capability expanded.

## Key Assumptions to Validate
- [ ] Real-model tool-call quality improves task completion over scripted baselines —
      validate with an opt-in, cost-capped smoke suite on held-out tasks (not yet done).
- [ ] A rootless container actually prevents host canary/secret access — validate on a
      host with cgroup v2 and podman; this environment cannot (documented limitation).
- [ ] Heuristic multi-language symbols improve retrieval on a gold set — measure
      Hit@5 before adding Tree-sitter adapters (audit §6.2).
- [ ] Mutation probes distinguish meaningful tests from weakened ones — measure
      KILLED/SURVIVED rates on seeded adversarial edits (WP9).

## MVP Scope (the "full product" of this build)
- WP0 trust fixes, WP1 execution isolation backend, WP2 capability grants, WP3 sessions,
  WP4 Workspace model, WP5 multi-language + incremental indexing, WP6 typed claims +
  discovery, WP7 model platform + one read-only connector, WP8 role DAG, WP9 mutation
  verification, WP10 runner bundles, WP11 local API control plane.
- Every package must land through data model → service → CLI/API → tests → status doc.

## Not Doing (and Why)
- **PPR / vector DB / embeddings as a core promise** — audit §6.2: measure hybrid
  retrieval against lexical+symbol baseline first; a ranking formula is not evidence.
- **General browser computer-use agent** — high-risk side effects, not required for
  engineering tasks; source retrieval only.
- **Agent swarm / marketplace** — v2.1 explicitly REJECTs this as an architecture goal.
- **Unrestricted shell on the host process** — isolation must exist first.
- **Kaggle/Colab zero-cost mesh / remote GPU dispatch** — volatile quotas, not product
  infrastructure; portable bundles only.
- **Hosted multi-user auth/tenancy** — needs a security and permission model first.
- **Auto-merge / deploy / infrastructure apply** — consequential side effects stay
  approval-gated.

## Open Questions
- Which single model provider should be the cost-capped real-model smoke target?
- Which held-out task corpus becomes the fixed baseline for "verified task completion
  per unit of human supervision and cost"?
- What is the minimum host capability set required before an isolated profile is offered
  in the UI/CLI (cgroup controllers, OCI runtime, user namespace)?

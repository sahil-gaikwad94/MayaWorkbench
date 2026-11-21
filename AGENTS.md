# Project: MayaWorkbench

## Tech stack
- Python 3.11+ (verified on 3.12.3), stdlib + `pydantic>=2.7,<3`; optional `pypdf`.
- SQLite (WAL, FTS5) via stdlib; content-addressed artifacts on disk.
- CLI is `argparse` (`src/mayaworkbench/cli/main.py`); a separate local worker process
  (`src/mayaworkbench/worker.py`) owns durable task execution.
- No frontend framework and no web server dependency unless a work package adds one.

## Commands
- Install (editable — required for the separate worker process):
  `python -m pip install -e ".[dev]"` plus `pypdf` for PDF tests.
- Full suite: `python -m pytest -q`
- Compile: `python -m compileall -q src`
- Capability report: `python -m mayaworkbench.cli.main runner doctor --json`

## Code conventions
- `domain/` has no I/O; `storage/stores.py` owns all SQL; CLI handlers call services only.
- Every persisted record is a strict Pydantic `Record` (`extra="forbid"`) with `ULID` ids.
- Migrations are checksummed, append-only `NNNN_name.sql` files; never edit an applied one.
- New top-level objects need: domain model, `stores.py` store/JSON mapping, service,
  CLI registration in a `cli/<area>_commands.py`, tests, and `IMPLEMENTATION_STATUS.md` update.
- ToolGateway is the only route from model-proposed actions to side effects.
- No empty scaffolding. A feature is done only when implemented through all layers,
  tested, and marked with an honest status (`SHIPPED`/`PARTIAL`/`SPEC_ONLY`/`NOT_STARTED`).

## Boundaries
- Never claim a secure sandbox: `LOCAL_TRUSTED` is host-user privilege. Isolated profiles
  must fail closed when capability cannot be verified.
- Never claim PPR, embeddings, HMAC approval authority, signed proofs, remote runner mesh,
  or v2.0 named engines unless code and tests prove them.
- Report resource limits per-limit from runtime evidence, never as a static claim.
- Never write tests that pass merely by asserting a claimed status string.
- Secrets come from the environment (`MAYA_MODEL_API_KEY`, `MAYA_SIGNING_KEY`); never commit.

## Verification gate before declaring any work package complete
1. `python -m compileall -q src` passes.
2. `python -m pytest -q` is green (record the count).
3. `IMPLEMENTATION_STATUS.md` and `docs/test-matrix.md` are updated.
4. No test artifact leaked into the repository root (`git status --short`).

See `docs/wiki/index.md` for dated decisions and `docs/build-report.md` for build history.

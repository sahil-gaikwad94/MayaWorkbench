# Local JSON control plane (WP11)

A thin HTTP/JSON transport over the **same** `Services` composition root the CLI
uses. It is a real server (`http.server.ThreadingHTTPServer`, standard library
only) with a durable event log, not a mock dashboard. It adds no business logic:
mutating routes call existing service methods and inherit their permission and
state checks.

## Start and probe

```bash
maya api serve --port 8765 --home /path/to/maya-home   # blocks until Ctrl-C
maya api health --port 8765                            # real GET /health
```

`register_api_commands(sub)` lives in `src/mayaworkbench/cli/api_commands.py`;
it is registered on the main parser by the parent work package. The server
factory is `mayaworkbench.api.create_server(services, host=..., port=...)`;
`port=0` requests an OS-assigned ephemeral port (used by the tests). The factory
returns a started-but-not-running server; callers own `serve_forever()` /
`shutdown()` / `server_close()`, or use `run_server(server)` which blocks until
`KeyboardInterrupt` and then closes the listener.

## Routes

All routes return JSON except `/events/stream`, which returns newline-delimited
JSON (NDJSON). `GET` is the default for reads; `POST` bodies must be JSON
objects. Successful responses are the serialised service/view results.

`GET /` serves the browser truth-surface UI (inline HTML/JS, no dependencies).
`GET /ui` is a stable alias that returns a `302` with `Location: /`; following it
serves the same UI. The UI is read by the same-origin browser client described
below, so it inherits the loopback-only, no-auth posture of the API.

| Method | Path | Query / body | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | — | Liveness; project and event counts. No paths or secrets. |
| GET | `/projects` | — | List projects with repository/snapshot counts. |
| GET | `/projects/{id}` | — | Project, repositories, recent snapshots. Accepts id or slug. |
| GET | `/repositories` | `project=` | Repositories for a project (default: the single project). |
| GET | `/snapshots` | `project=`, `limit=` | Snapshots for a project. |
| GET | `/index/{snapshot}` | — | Deterministic repository map for a pinned snapshot. |
| GET | `/search` | `q=` (required), `snapshot=`, `mode=auto\|lexical\|exact\|regex`, `project=`, `limit=` | Lexical/symbol search over the pinned index. |
| GET | `/symbols` | `q=` (required), `snapshot=`, `project=`, `limit=` | Symbol search over the pinned index. |
| GET | `/research/documents` | `project=`, `limit=` | Registered research documents. |
| GET | `/research/claims` | `project=`, `document=`, `limit=` | Extracted claims, optionally by document. |
| GET | `/research/reproduction/{run_id}` | — | Backend-computed four-tier reproducibility report plus its artifact id. |
| GET | `/tasks` | `project=`, `limit=` | Tasks in a project. |
| GET | `/tasks/{id}` | — | Task, immutable contract and its runs (`TaskService.task_view`). |
| GET | `/runs/{id}` | — | Run, plan, approvals, diff, review, verification, tool calls (`TaskService.run_view`). |
| GET | `/runs/{id}/events` | `limit=` | Durable events filtered to a run. |
| GET | `/runs/{id}/timeline` | — | Ordered read-only stream of run events and tool calls with `{seq, at, kind, label, detail, ref}` items and a `truncated` flag. |
| GET | `/runs/{id}/plan` | — | Contract projection, latest plan with steps, scoped approvals (no nonce/HMAC) and the task workspace. |
| GET | `/runs/{id}/evidence` | — | Verification report, checks, evidence links, claim mappings, reproducibility tiers and experiment comparison with `warnings` for degraded sources. |
| GET | `/runs/{id}/diff` | — | The captured candidate patch text, its SHA-256, base commit and changed-file list; 404 when no patch was captured. |
| GET | `/workspaces` | — | Engineering workspaces. |
| GET | `/workspaces/{id}/graph` | — | Workspace resources, environments and evidence graph. |
| GET | `/verification/{run_id}/report` | — | Latest verification report and its checks. |
| GET | `/experiments` | `project=`, `limit=` | Experiment definitions (all projects when `project` is omitted). |
| GET | `/experiments/{identifier}/comparison` | — | Latest comparison by comparison id or experiment definition id, plus best-effort Markdown report; 404 when unknown or absent. |
| POST | `/tasks/{id}/approve` | `{"approval_id": "..."}` | Grant a scoped approval via `TaskService.approve`. |
| POST | `/tasks/{id}/run` | `{}` | Start/resume execution via `TaskService.run_task(..., detached=True)`. |
| POST | `/tasks/{id}/pause` | `{}` | Request pause via `TaskService.pause_run`. |
| POST | `/tasks/{id}/resume` | `{}` | Resume via `TaskService.resume_run`. |
| POST | `/tasks/{id}/cancel` | `{"reason": "..."}` | Cancel via `TaskService.cancel_run`. |
| GET | `/events` | `project=`, `after=` (seq), `limit=` | Durable append-only log with `next_cursor` and `has_more` for reconnect. |
| GET | `/events/stream` | `after=`, `project=`, `limit=`, `seconds=1..30` | Bounded NDJSON stream: `event`, `heartbeat` and final `end` lines, each carrying `seq`. |
| GET | `/evidence/{run_id}/bundle` | — | Export the evidence bundle via `VerificationService.export_bundle` and return its files as JSON (`utf-8` or `base64` per file) plus the manifest. |

### Error contract

Every error is `{"error": "<message>", "code": "<stable code>", "hint": "<remediation or null>"}`:

- `400` — validation (missing/duplicate/oversized/out-of-range/malformed params or JSON), `INVALID_PATH`, `INVALID_ID`.
- `403` — policy, approval, budget, state or conflict refusals from application services.
- `404` — unknown entity or route.
- `405` — unsupported method on a known shape.
- `500` — opaque `INTERNAL` response; tracebacks are written only to the local server log.

Secrets (`MAYA_MODEL_API_KEY`, `MAYA_SIGNING_KEY`), host paths and raw stack
traces are never included in a response.

## Event streaming and reconnect

`workflow_events` is authoritative. `GET /events` returns items shaped as
`{"seq": <int>, "event": <WorkflowEvent JSON>}` ordered by `seq`, plus
`next_cursor` (the last `seq`, or `after` when empty) and `has_more`. A client
reconnects with `?after=<next_cursor>` and receives only strictly newer rows.
`/events/stream` yields the same envelope as NDJSON lines prefixed with
`{"type": "event" | "heartbeat" | "end", "seq": ...}`; it emits a heartbeat when
idle and ends after `seconds` (1–30), so connections are bounded and reconnect is
always safe. A narrow read-only projection in `api/eventlog.py` exposes the
existing `seq` column; appends remain in `storage.stores.EventStore`.

The browser client reads `/events/stream` as **NDJSON over `fetch()`, not as
Server-Sent Events (SSE)**. The stream is `application/x-ndjson` with
`Connection: close` and bare JSON lines terminated by `\n` — there is no
`text/event-stream` content type and no `data: ...\n\n` SSE framing, so
`EventSource`/`onmessage` cannot consume it. The inline client opens the stream
with `fetch()`, reads `response.body.getReader()` through a `TextDecoder`,
buffers partial lines, `JSON.parse`s each complete line, skips and logs malformed
lines, advances the durable cursor from each line's `seq`, handles `heartbeat`
and `end` explicitly, and reconnects with a fresh cursor using an
`AbortController` on disconnect/reconnect. The Approvals panel renders the task
view from `GET /tasks/{id}` (required approvals, requested capabilities and
scope) plus the latest run's approval requests, and grants a pending approval by
POSTing its `approval_id` to `/tasks/{id}/approve`. The Verification / evidence
panel shows the report from `/verification/{run_id}/report` and the manifest and
file hashes from `/evidence/{run_id}/bundle` (with a clear message on 404). A
separate Source diff panel loads the exact stored patch from
`GET /runs/{run_id}/diff` and renders it as literal text with its SHA-256 and
file list; it never injects server data into HTML.

## Intentional limits

- **Local-only by default.** Binds `127.0.0.1`. A non-loopback `--host` is a
  deliberate, warned-about override; this control plane has **no authentication,
  authorisation, tenancy or rate limiting** and is for a single trusted local
  user on a local host.
- **No shell, training or repository code in request handlers.** Handlers only
  validate, delegate to services and serialise. Execution stays in the separate
  local worker process; `POST .../run` starts it detached and never waits for a
  run to finish.
- **No new dependency.** Standard library `http.server`, `urllib`, `json` only.
- **No mutations outside the listed routes.** There is no project/repository
  creation, indexing, research ingestion, verification execution or experiment
  execution over HTTP in this work package; use the CLI, which remains
  first-class and can perform the same operations.
- **Bounded inputs.** Query values are capped (query strings 256 characters
  each, total request target 4096, JSON bodies 1 MiB, ids 128, search text 256).
  Path traversal, null bytes and backslashes are refused.
- **Bounded streams.** `/events/stream` runs at most 30 seconds and polls at
  0.25 s; reconnect is via the durable cursor, not in-memory connection state.
- **Read-only bundle projection.** The evidence bundle is exported to a private
  temporary directory and returned as JSON; it is not a signed attestation and
  must still be verified with `maya verify check-bundle` if that matters.

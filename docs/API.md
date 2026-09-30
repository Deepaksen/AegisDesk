# AegisDesk HTTP API

The product boundary (spec §33, Milestone 10). A thin FastAPI adapter over `aegisdesk.runtime.AegisRuntime`: the same agents, tools, gateway, approvals and audit the CLI uses. The generated OpenAPI document is the contract: `GET /openapi.json`, interactive docs at `/docs`.

```bash
uv run aegisdesk api serve                 # http://127.0.0.1:8000/docs
docker compose up --build                  # api on :8000 with PostgreSQL + MCP servers
```

## Authentication

Every `/api/v1` route needs `X-Employee-Id`, the employee ID an **authenticating gateway** forwards after it has verified the user (ADR 0015). The API checks it against the directory: missing, unknown or terminated → `401`.

The header is trusted, so the API must be reachable **only** through that gateway. Anyone who can reach the port directly can claim to be anyone. Request bodies never carry identity: their models forbid unknown fields, so `{"employee_id": "E1010"}` is a `422`, not an impersonation. Moving to OIDC replaces one dependency (`api/auth.py: current_user`); routes do not change.

## Endpoints

| Method and path | Purpose | Notes |
|---|---|---|
| `GET /health` | liveness | the process serves requests |
| `GET /ready` | readiness | database (when a PostgreSQL store is configured), checkpointer; `503` with the failing component |
| `GET /metrics` | Prometheus exposition | spec §24 metrics plus `aegisdesk_http_requests_total` / `aegisdesk_http_latency_seconds`; labels are route templates, never IDs or users |
| `GET /api/v1/me` | who the API thinks you are | name, department, roles |
| `POST /api/v1/threads` | new conversation | `201 {thread_id}`; the thread belongs to the caller from its first message |
| `POST /api/v1/threads/{thread_id}/messages` | one turn | body `{text}` (1–10,000 chars); JSON `MessageResponse`, or SSE (below) |
| `GET /api/v1/threads/{thread_id}` | the conversation | user and assistant messages only (no tool calls or raw tool results) plus pending approvals; someone else's thread → `404` |
| `GET /api/v1/approvals` | approvals waiting for the caller | the approver rules of M7 decide who sees what |
| `GET /api/v1/approvals/{approval_id}` | one approval | requester, approver, or decider; otherwise `404` |
| `POST /api/v1/approvals/{approval_id}/approve` | approve a step | body `{comment?}`; when the request is settled the paused workflow resumes and `resumed` holds that turn |
| `POST /api/v1/approvals/{approval_id}/reject` | reject a step | same shape; the workflow resumes to report the rejection |
| `GET /api/v1/audit?request_id=&limit=` | audit events | your own; security approvers and IT admins see everyone's |

### MessageResponse

```json
{
  "thread_id": "…", "request_id": "…", "trace_id": "…",
  "answer": "…", "stop_reason": "final_answer",
  "activity": [{"text": "Routing your request to: access.", "status": "ok"},
               {"text": "Access request AR-1013 created.", "status": "ok"},
               {"text": "Manager approval is required.", "status": "waiting"}],
  "citations": [{"document_id": "DOC-ACC-001", "title": "Application Access Policy"}],
  "references": ["AR-1013", "AP-0001"],
  "pending_approvals": [{"approval_id": "AP-0001", "access_request_id": "AR-1013",
                         "step": "manager", "approver": "E1010"}],
  "usage": {"input_tokens": 924, "output_tokens": 56}, "latency_ms": 12.4
}
```

**Activity** lines are written by code from the trajectory, never by the model (spec §34: no chain-of-thought). Model steps are not shown. Tool arguments chosen by the model appear only when they match a known ID format (`INC-1234`, `DOC-VPN-001`) or resolve to a real application; an unknown tool name is shown as "An unavailable action was refused."

## Streaming

Send `Accept: text/event-stream` to the messages endpoint for server-sent events:

```
event: activity
data: {"text": "Routing your request to: access.", "status": "ok"}

event: activity
data: {"text": "Manager approval is required.", "status": "waiting"}

event: result
data: { …MessageResponse… }
```

`activity` events arrive as graph nodes finish. There is exactly one final `result` event, or an `error` event carrying a Problem. The turn runs in a worker thread in the request's trace context, so streamed and non-streamed turns produce the same spans. SSE rather than WebSockets: one-way server→client updates over plain HTTP, easy to proxy, and trivially consumed with `curl -N`.

## Idempotency

`Idempotency-Key: <client-chosen string>` on the messages endpoint (Milestone 11):

| A request with a key that… | Response |
|---|---|
| is new | runs; a successful response is stored for `IDEMPOTENCY_TTL_HOURS` (24) |
| finished before, same thread and text | the **stored response**, header `Idempotent-Replayed: true`; nothing runs again (SSE: a single `result` event) |
| is still running | `409 request_in_progress`, `Retry-After: 1` |
| was used for a different message | `422 idempotency_key_reused` |
| failed before (5xx, model outage) | runs again: failures release the key |

Keys are per user and stored in memory or PostgreSQL (migration 0004; a row lock per key, so replicas agree). The key also becomes the request ID (`idem-` + a hash of user and key), so writes made during a retried attempt reuse the idempotent tools (M1) and the per-request write budget (M9). Without the header, every call is a new request.

## Failures (Milestone 11)

| Situation | Response |
|---|---|
| model timeout, outage or rate limit (after retries) | `503`, `category` = `model_timeout` / `model_unavailable` / `model_rate_limited`, `Retry-After: 10`, `thread_id`, `detail` = the safe answer |
| model circuit open (repeated failures) | `503 circuit_open`, immediately, `Retry-After: 10` |
| model rejected the request (bad key, bad request) | `502 model_error` |
| database down (checkpoints, access data, idempotency records) | `503 <store>_unavailable`, `Retry-After: 5` |
| audit trail down while deciding an approval | `503 audit_unavailable`: the decision is refused, nothing changes |
| tool, store or MCP server down *during* a turn | `200`: the answer says what could not be done (activity shows the failed step) |

Streaming turns report the same problems as an `error` event (with `retry_after` in the body). `/ready` lists circuit-breaker states under `circuits`; an open circuit does not make the service unready.

## Errors

RFC 9457 `application/problem+json`:

```json
{"type": "about:blank", "title": "Approval decision refused", "status": 403,
 "category": "cannot_approve_own_request", "request_id": "…", "trace_id": "…"}
```

| Status | When |
|---|---|
| 401 | no, unknown or inactive `X-Employee-Id` |
| 403 | approval refused: `cannot_approve_own_request`, `not_the_approver`, `missing_approver_role`, `already_decided_another_step` |
| 404 | thread or approval not found, **or not yours** (existence is not disclosed) |
| 409 | approval `expired` or `already_decided` differently |
| 422 | invalid body or parameters (field names and messages only; submitted values are not echoed) |
| 500 | anything else: a generic body with the `trace_id`; details are only in logs and traces |

Every response has `X-Request-Id`: the turn's request ID for messages, otherwise a per-HTTP-request ID. It matches `request_id` in audit events and JSON logs.

## Observability

A middleware span per HTTP request (`POST /api/v1/threads/{thread_id}/messages`, with `http.request.method`, `http.route`, `http.response.status_code`; the route template, never the raw path) is the parent of the M8 trace: `aegisdesk.request` → router → specialists → tools → MCP → policy. With `TELEMETRY_EXPORTER=otlp`, the API sends traces to the collector and serves metrics on `/metrics` for Prometheus to scrape, so API metrics are not counted twice.

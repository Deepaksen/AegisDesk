# Milestone 10: API + UI

**Goal:** expose the platform as a production-like HTTP API (spec §33) and add a minimal Streamlit UI (§34), without reshaping the backend around the UI. `docker compose up` now brings up the whole platform (§43).

**What you can run now:**

```bash
uv run aegisdesk api serve                          # http://127.0.0.1:8000/docs
uv run streamlit run apps/ui/streamlit_app.py       # http://localhost:8501
docker compose up --build                           # postgres, migrate, mcp, api, ui (set MCP_TOKEN_SECRET)
```

Design: [`docs/API.md`](../API.md), [`docs/RUNBOOK.md`](../RUNBOOK.md). Decisions: [ADR 0015](../adr/0015-fastapi-thin-adapter-trusted-header.md) (thin adapter, trusted header), [ADR 0016](../adr/0016-streamlit-initially.md) (Streamlit).

---

## 1. Concepts introduced

### The API is the product boundary
Everything a person or program can do goes through versioned HTTP endpoints with a published contract (OpenAPI, generated from the Pydantic models). The UI, a future React app, a Slack bot and the tests are all clients of the same API. Nothing the UI needs is special-cased in the backend.

### A thin adapter over a shared runtime
Web frameworks tempt you to put logic in route handlers. Here the routes only authenticate, call `AegisRuntime`, and map results to response models. `AegisRuntime` builds the components once per process (repository, audit log, gateway, approval service, checkpointer, tool transport, supervisor) and exposes the use cases. The CLI's approval command now uses the same `decide()` → resume path as the API.

### Identity at the edge: trusted header
The user chose a trusted `X-Employee-Id` header, as if an authenticating gateway had verified the user.
- **Why it's simple:** one dependency, and no token handling in this milestone.
- **What it costs:** the API must be reachable *only* through that gateway, or anyone can be anyone.
- **What still holds:** identity enters in exactly one place (`current_user`) and is validated against the directory. Bodies can't carry identity: unknown fields are rejected. OIDC later replaces one function (ADR 0015).

### Streaming with server-sent events
A turn takes several model and tool steps, and users want to see progress. SSE is one-way server→client events over ordinary HTTP. That's all we need, it's proxy-friendly, and `curl -N` shows it. WebSockets would add a bidirectional protocol for no benefit here. The events come from LangGraph's per-node `updates` stream (M2), which the CLI already used.

### Safe activity, not chain-of-thought
The spec wants "Checking your employee profile...", not the model's reasoning. Activity lines are built by **code**, from the trajectory. Model steps are never shown. Model-chosen arguments appear only if they match an ID format or resolve to a real application, so a prompt-injected value can't be reflected into the UI through the activity feed (tested with a fake secret).

### Idempotency keys
Networks fail after the server did the work. `Idempotency-Key` turns a client retry into the *same request ID*, so the idempotent tools (M1) return the existing ticket and the write budget (M9) counts both attempts together.

### Liveness vs readiness
`/health` says "the process is up" (restart me if not). `/ready` says "I can serve requests now": database reachable, checkpointer usable. Orchestrators route traffic only to ready instances; compose waits for `api` to be ready before starting `ui`.

### Problem details and not leaking existence
Errors are RFC 9457 `application/problem+json` with the `trace_id`, so an error report can be joined to the trace. Someone else's thread or approval is `404`, not `403`, which doesn't confirm that it exists. Internal errors never include exception text.

### Libraries introduced
| Library | Problem it solves | What we'd write otherwise |
|---|---|---|
| **FastAPI** | routing, request validation with our Pydantic models, dependency injection (identity, runtime), OpenAPI generation | a Starlette app with hand-written parsing, validation and an OpenAPI document kept in sync by hand |
| **Streamlit** | a chat UI with status, tabs, forms and tables in Python | a JavaScript frontend with its own toolchain (ADR 0016) |
| **opentelemetry-exporter-prometheus**, **prometheus-client** | a Prometheus `/metrics` endpoint from the same OpenTelemetry instruments | a second set of metrics, or scraping only via the collector |

## 2. Architecture

```
 Browser ──► Streamlit UI (apps/ui) ──HTTP──► FastAPI (src/aegisdesk/api)
                  ApiClient (httpx)              │  middleware: http span, X-Request-Id, http metrics
                                                 │  current_user ◄── X-Employee-Id (gateway)
                                                 ▼
                                         AegisRuntime (src/aegisdesk/runtime.py)
              send_message · thread_view · decide(+resume) · audit_events · readiness
                                                 │
                    supervisor graph (M4) ─ tools via gateway (M6) / MCP (M5) ─ approvals (M7)
                                                 │
                         PostgreSQL: access data, audit, checkpoints, pgvector (compose)
```

| File | Role |
|---|---|
| `src/aegisdesk/runtime.py` | long-lived components; use cases; safe activity, citations, references |
| `src/aegisdesk/api/app.py` | app factory, routes, SSE, problem+json, telemetry middleware |
| `src/aegisdesk/api/auth.py` | `current_user` from `X-Employee-Id` |
| `src/aegisdesk/api/schemas.py` | request and response models (the OpenAPI contract) |
| `src/aegisdesk/ui/client.py` | `ApiClient`, SSE parsing; the only backend module the UI imports |
| `apps/ui/streamlit_app.py` | employee, manager and audit views |
| `Dockerfile`, `docker-compose.yml` | one image; services `migrate`, `mcp`, `api`, `ui` next to `postgres` and the observability profile |

Changed along the way:
- `aegisdesk approvals approve` uses `AegisRuntime.decide`.
- `configure_telemetry(prometheus=True)`: a scraped process sends only traces over OTLP.
- Two HTTP metrics added to the catalogue.
- HTTP span attributes added to the redaction allowlist.
- An empty `MCP_TOKEN_SECRET` now means "not configured".

### A bug found on the way
`aegisdesk mcp serve` used a private in-memory seed repository and a gateway without the access store. When the MCP servers run as their own process on PostgreSQL (the compose stack), two things went wrong:
- the action server wrote access requests to its own memory, not the shared database;
- the gateway could not see recorded approvals, so every HIGH-risk `provision_access` would have been denied.

It now uses the configured stores and verifies approvals like the host does. `test_api_uses_shared_stores_and_remote_tools` pins the configuration down.

## 3. The spec section 43 walkthrough with curl

Real output from `aegisdesk api serve` in this environment, on the offline model with memory stores. Long values are shortened.

```bash
curl -s localhost:8000/ready
{"status":"ready","checkpointer":"ok","agent":"ok"}

H='-H X-Employee-Id:E1004 -H Content-Type:application/json'
T=$(curl -s -X POST $H localhost:8000/api/v1/threads | jq -r .thread_id)

# 2. a RAG question: citations come from the answer, titles from the documents
curl -s -X POST $H localhost:8000/api/v1/threads/$T/messages -d '{"text":"How do I configure VPN on macOS?"}'
  citations: [{'document_id': 'DOC-VPN-001', 'title': 'VPN Troubleshooting Guide'}]
  activity:  ['Routing your request to: knowledge.', 'Searching the knowledge base...']

# 6-7. sensitive access, streamed: the workflow pauses for approval
curl -sN -X POST $H -H 'Accept: text/event-stream' localhost:8000/api/v1/threads/$T/messages \
     -d '{"text":"Please create an access request for FinanceERP for month-end reporting"}'
event: activity
data: {"text": "Routing your request to: access.", "status": "ok"}

event: activity
data: {"text": "Access request AR-1013 created.", "status": "ok"}

event: activity
data: {"text": "Manager approval is required.", "status": "waiting"}

event: result
data: {"thread_id": "92db64fe-…", "request_id": "e4f35045-…", "trace_id": "0276189f…", …}

# 8-10. the manager approves; the original workflow resumes
curl -s -H X-Employee-Id:E1010 localhost:8000/api/v1/approvals
  AP-0001 FinanceERP Aisha Khan pending
curl -s -X POST -H X-Employee-Id:E1010 -H Content-Type:application/json \
     localhost:8000/api/v1/approvals/AP-0001/approve -d '{"comment":"Month-end close"}'
  approved True | Update on AR-1013 (FinanceERP): approved by manager: Grace Liu (E1010) (AP-0001).
  Access has been granted.

# 12. audit events (the employee's own)
curl -s -H X-Employee-Id:E1004 localhost:8000/api/v1/audit
  decision create_access_request allow
  outcome  create_access_request ok
  outcome  decide_approval       approved  AP-0001 E1010
  decision provision_access      allow     AP-0001 E1010
  outcome  provision_access      ok        AP-0001 E1010

curl -s localhost:8000/metrics | grep http_requests_total
aegisdesk_http_requests_total{method="POST",route="/api/v1/threads/{thread_id}/messages",status_code="200",…} 2.0
curl -s -i -X POST localhost:8000/api/v1/threads | head -1
HTTP/1.1 401 Unauthorized
```

Step 11 (inspect the trace) is the `trace_id` in every response. With the observability profile, the UI links it to Grafana Explore (Tempo). Steps 13–15 are the M9 commands, in [RUNBOOK](../RUNBOOK.md#operate).

### The trace path
```
POST /api/v1/threads/{thread_id}/messages          (API middleware, http.route, status)
 └─ fastapi endpoint spans                           (FastAPI's own instrumentation)
     └─ aegisdesk.request                            (M8: request_id, thread_id, user hash)
         ├─ chat … (router)  └─ invoke_agent access
         │                       ├─ chat …           ├─ execute_tool create_access_request
         │                       │                   │   ├─ policy.evaluate  └─ tool.handler
         └─ approval.await (interrupt)
POST /api/v1/approvals/{approval_id}/approve
 └─ … approval.decide ─ aegisdesk.resume ─ apply_approvals ─ execute_tool provision_access (policy: approval verified)
```

## 4. The UI

The screenshots come from a real headless Chromium session (Playwright), driving the Streamlit UI against the running API:

| Employee: citation, references, pause | Manager: pending approval | After approval: resumed answer |
|---|---|---|
| ![employee](img/m10-ui-employee.png) | ![manager](img/m10-ui-manager.png) | ![approved](img/m10-ui-approved.png) |

The answers look like JSON because the offline fake model echoes tool results. With a real model they are prose; the citations, references, activity and approval panels come from the API either way.

## 5. Results

All run in this environment:
- `uv run pytest -m "not live"` with PostgreSQL: **473 passed, 1 skipped**. New in M10:
  - `tests/api/test_api.py` (18):
    - operations and OpenAPI;
    - metrics without user labels;
    - 401 cases, and identity in a body → 422;
    - the RAG question, laptop and ticket;
    - idempotency keys;
    - thread privacy and the thread view without tool calls;
    - approve, refuse and resume;
    - reject;
    - audit scoping;
    - SSE;
    - no reflection of model-chosen text;
    - the HTTP span joining the agent trace;
    - no leaking of internal errors.
  - `tests/api/test_ui.py` (6): the SSE parser, `ApiClient` against the real app, Streamlit `AppTest` for the employee and manager views, and the import boundary.
  - `tests/unit/test_deployment_config.py` (7) plus Prometheus and config tests.
  - `tests/integration/test_api_postgres.py`: a request on one API instance, approved and resumed through another, on PostgreSQL.
- `ruff`, `ruff format`, `mypy src tests apps` (strict): clean.
- The M9 gates are unchanged after the runtime refactor: golden multi and multi_mcp task success 0.617, adversarial 1.000, all safety and regression gates pass; RAG hit rate 0.89.
- `docker compose config`: valid (and in CI).

Not run here: **`docker compose up`** and the image build. The Docker CLI is present, but there is no Docker daemon in this environment. The compose file and Dockerfile are validated statically (`docker compose config` plus tests), and the same processes (`aegisdesk api serve`, `aegisdesk mcp serve`, `streamlit run`) were run directly.

## 6. Exercises
1. Add `If-Match`-style protection: reject a message if the thread has changed since the client last read it. Where does that check belong: route, runtime or graph?
2. Replace `current_user` with OIDC bearer-token verification (for example against a local Keycloak). Which files change? (Answer to check against: one.)
3. Run two API processes on PostgreSQL and send two messages to one thread at the same time. What does the per-thread lock cover, and what doesn't it? (ADR 0015, consequences.)
4. Add a `GET /api/v1/threads` listing the caller's threads. What store does it need that the checkpointer doesn't give you?
5. Point Prometheus at the API while also sending OTLP metrics, and see what the double counting does to the dashboard.

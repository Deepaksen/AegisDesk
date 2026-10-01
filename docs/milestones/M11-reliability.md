# Milestone 11: Reliability

**Goal:** inject the spec's failures (MCP timeout, database error, model timeout, malformed response, duplicate request, tool failure) and make the system behave appropriately. That means: no crash, no unintended or duplicated write, nothing invented, a clear message to the user, a correct HTTP status for clients, and full visibility in traces and metrics.

**What you can run now:**

```bash
uv run aegisdesk eval golden --dataset evals/reliability/faults_v1.yaml --config multi   # 13 fault cases
AEGIS_FAULTS=model_timeout:router BREAKER_FAILURE_THRESHOLD=2 uv run aegisdesk api serve  # watch it degrade
uv run aegisdesk approvals reconcile --as E1006   # finish approved-but-unprovisioned requests
```

Decision: [ADR 0017](../adr/0017-resilience-degrade-safely.md). Operations: [RUNBOOK](../RUNBOOK.md).

---

## 1. Concepts introduced

### Classify, then decide
The first step in handling a failure is naming it. Provider SDKs raise dozens of exception types. `ModelGuard` maps them to four categories (`model_timeout`, `model_unavailable`, `model_rate_limited`, `model_error`), and the rest of the system decides on those. Anything it can't classify is a **bug**, and is re-raised: disguising a `KeyError` as "the model is down" would hide it forever.

### Retries: only what is safe, only in one place
A model call has no side effects, so retrying it is safe. A write tool is different: after an MCP timeout the server may have created the ticket. So:
- model calls are retried with exponential backoff and jitter (the jitter keeps many clients from retrying in lockstep);
- read tools over MCP are retried once (M5);
- writes are never retried by the server.

A *client* may retry a write safely, because the same `Idempotency-Key` means the same request ID and the same tool idempotency keys. Provider SDK retries are switched off, so there is one retry policy, visible in traces, not retries multiplied inside retries.

### Circuit breakers
During an outage, every request waits for a timeout before failing. That is slow for users, and it adds load on the system that is trying to recover. A breaker counts failed calls. After N consecutive failures it **opens**, and calls fail immediately. After a cooldown it is **half-open**: one trial call is allowed. Success **closes** it; failure opens it again. There is one breaker per model and one per MCP server.

### Degrade safely, not cleverly
When the model is down, the system does not guess an answer, and it does not write "just in case". The turn ends with a fixed message ("temporarily unavailable … nothing else was changed") and `stop_reason: model_error`. The API answers `503` with `Retry-After`, which tells a client exactly what to do next.

### Idempotent replay
M1 made writes idempotent. That protects the *data*, but a retried request still ran the model again and added the message to the conversation twice. Now the first successful response is stored per (user, key):

| A retry with the same key… | Result |
|---|---|
| after the first attempt finished | **replay**: the stored response, header `Idempotent-Replayed: true`, nothing runs |
| while the first attempt is still running | `409`, `Retry-After: 1` |
| for a different message | `422`: a key must identify one request |
| after the first attempt failed | runs for real (failures release the key) |

### Never act without a record: write-ahead audit
The gateway already refused writes when the audit trail was down (M6). Human approval decisions did not follow that rule: the decision was stored first and the audit event written afterwards, with a failure only logged. The reliability tests showed where that leads. The approval was stored, the audit write failed, provisioning was then refused (no audit trail, no writes), and the request was stuck approved-but-unprovisioned. Now the decision event is written **before** anything changes, and if it can't be written, the decision is refused (503).

### Reconciliation
Some failures happen after the point of no return, for example a provisioning call failing after the manager approved. Rather than hoping, a deterministic reconciler finds requests made through the assistant that are settled but not provisioned. It either resumes a thread that is still paused (the process died between decision and resume), or provisions again. Provisioning is idempotent and goes through the gateway.

## 2. Failure by failure

| Failure | Injection (`AEGIS_FAULTS`) | Behaviour | Where |
|---|---|---|---|
| Model timeout / outage | `model_timeout[:target]`, `model_unavailable[:target]` | retries with backoff → safe answer, `model_error`; API `503` + `Retry-After: 10`; breaker opens → `circuit_open`, fails fast | `reliability/model_guard.py`, both graphs |
| Malformed response | `model_malformed[:target]` | router: "please rephrase", nothing runs. Broken tool-call JSON: answered with `malformed_tool_call`, the model may correct it, bounded by step limits, traced. Empty reply: fixed answer | `graphs/service_desk_graph.py`, `graphs/supervisor_graph.py` |
| Database error | `db_error:access`, `db_error:audit`, `db_error:checkpoint` | access store: tools report `unavailable`, nothing written; audit: writes and approval decisions refused (fail closed), reads continue; checkpoint: `StoreUnavailableError` → `503` + `Retry-After: 5` | `domain/access_store.py` (guard), `approvals/service.py`, `api/app.py` |
| MCP timeout / outage | `tool_timeout:<tool>`, `mcp_unavailable:<server>` | read tools retried once, writes never; breaker per server fails fast with `unavailable` | `tools/remote.py` |
| Tool failure | `tool_error:<tool>` | `internal_error` result to the model, nothing written; provisioning failures are finished by `reconcile` | `tools/executor.py`, `runtime.py` |
| Duplicate request | same `Idempotency-Key` twice | replay the stored response; `409` in flight; `422` on key reuse | `persistence/idempotency.py`, migration 0004 |

Real driver errors take the same paths as the injected ones. A SQLAlchemy, psycopg or sqlite `OperationalError` becomes `StoreUnavailableError`, while constraint violations and bugs don't (`is_database_outage`).

## 3. Results

All real, in this environment, on the offline model.

### Reliability suite (`evals/reliability/faults_v1.yaml`, 13 cases)

| | multi | multi_mcp |
|---|---|---|
| task success | 1.000 | 1.000 |
| unauthorized actions | 0 | 0 |
| unexpected writes | 0 | 0 |
| trace coverage | 1.000 | 1.000 |
| latency p50 / p95 | 0.022 s / 1.232 s | 0.027 s / 0.825 s |

The p95 is the model-timeout cases: two retries with backoff before giving up, which is exactly the cost the breaker then removes.

**What the suite found:** the first run failed `rel-04` on trace coverage (0.92). Malformed tool calls were answered correctly but had no `execute_tool` span. They now go through `refused_call()`, the same path as unknown tools (the M9 fix).

### Live: the model goes down (`AEGIS_FAULTS=model_timeout:router BREAKER_FAILURE_THRESHOLD=2`)

```
HTTP 503  1.280215s  retry-after: 10     model_timeout | The assistant is temporarily unavailable, ...
HTTP 503  1.242673s  retry-after: 10     model_timeout | The assistant is temporarily unavailable, ...
HTTP 503  0.020156s  retry-after: 10     circuit_open  | The assistant is temporarily unavailable, ...
GET /ready → {"status":"ready","checkpointer":"ok","agent":"ok","circuits":{"model:fake/fake-scripted":"open"}}
```

The first two requests pay for the retries (about 1.25 s). Then the breaker is open, and the third fails in 20 ms.

### Live: a client retries a ticket request (`Idempotency-Key: retry-42`)

```
attempt 1: HTTP/1.1 200 OK                            refs=['INC-1008']
attempt 2: HTTP/1.1 200 OK idempotent-replayed: true  refs=['INC-1008']
messages in thread: 2                                   (one question, one answer: nothing ran twice)
same key, different text → 422 idempotency_key_reused
aegisdesk_idempotency_replays_total{route="messages"} 1.0
```

### Tests and gates
- `uv run pytest -m "not live"` with PostgreSQL: **522 passed, 1 skipped**. New in M11:
  - `tests/unit/test_reliability.py` (27): breaker states, guard retries, backoff, classification, bugs not hidden, fail-fast; agents under router and specialist outages, malformed and empty replies, recovery from a malformed call, database and MCP outages;
  - `tests/unit/test_reconcile.py` (3);
  - `tests/api/test_api_reliability.py` (10): replay, 409, 422, streamed replay, 503s with `Retry-After`, circuits on `/ready`, checkpoint, access and audit outages;
  - `tests/integration/test_idempotency_store.py` (8): the memory and PostgreSQL contract, including 8 concurrent duplicates → exactly one runs;
  - a CLI test for `reconcile`.
- M9 gates unchanged: golden task success 0.617 (multi and multi_mcp), adversarial 1.000; RAG hit rate 0.89. CI now also runs the reliability suite on both configurations against committed baselines.

## 4. The trace of a model outage

```
POST /api/v1/threads/{thread_id}/messages              status 503
 └─ aegisdesk.request                                   aegisdesk.status=model_error
     └─ chat fake-scripted (router)                     error.category=model_timeout
                                                        (3 attempts; aegisdesk_model_errors_total +3)
```

A `circuit_open` request has the same shape, with no model attempts at all.

## 5. Exercises
1. Set `MODEL_MAX_RETRIES=0` and rerun the live outage. How does latency before the first 503 change, and after the breaker opens?
2. Run two API processes against PostgreSQL and send the same `Idempotency-Key` to both at once. Which one runs? (Row lock in `PgIdempotencyStore.begin`.)
3. Inject `mcp_unavailable:action` over `mcp_http` and watch `aegisdesk_circuit_transitions_total`. What does the user see before and after the breaker opens?
4. Kill the API process right after a manager approves (between `decide` and `resume`). How does `reconcile` bring the conversation back?
5. Where would a *fallback model* plug in without touching any caller? (Hint: the guard's categories.)

## 6. Limitations
- Breakers are per process: each replica discovers an outage on its own.
- A turn that ended in 503 has already written the user's message and the apology to the thread; the retry adds the message again.
- `Retry-After` for a model outage is a fixed 10 s, not the breaker's remaining cooldown.
- Reconciliation is a command, not a scheduled job; M12 (CI/CD) or an operator runs it.

# Milestone 8: Observability

**Goal:** see what the system did, why, and how well, without reading code or conversation text. Every request has one trace across agents, tools, MCP servers, policy and approvals. The spec §24 metrics feed dashboards. Structured logs carry the same trace ID, and LangSmith can be switched on. Nothing sensitive leaves the process. Injected failures are debugged from telemetry, not `print()`.

**What you can run now** (no infrastructure needed):

```bash
uv run aegisdesk agent --as E1004 --trace "Please create an access request for FinanceERP for month-end reporting"
LOG_FORMAT=json AEGIS_FAULTS=tool_timeout:get_my_assets \
  uv run aegisdesk agent --as E1004 --tools mcp_inprocess --trace "What laptop is assigned to me?"
uv run aegisdesk telemetry                                  # where telemetry goes (no secrets)
```

With Docker, `docker compose --profile observability up -d` starts the OpenTelemetry Collector, Tempo, Prometheus and Grafana. Set `TELEMETRY_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` and open the "AegisDesk overview" dashboard on http://localhost:3000. See [`docs/OBSERVABILITY.md`](../OBSERVABILITY.md).

Decision: [ADR 0013](../adr/0013-opentelemetry-langsmith-redaction.md).

---

## 1. Concepts introduced

### Traces, spans, context
A **trace** is one request's story; a **span** is one step in it (a model call, a tool call, a policy check), with a start, an end, attributes and a status. Spans nest through **context**: the current span is the parent of the next one. Inside one process that is a context variable. Across processes it is the W3C `traceparent` header. We carry it in MCP `_meta`, so the action server's work appears inside the client's `mcp.call` span.

### Three signals, one ID
- **Traces:** what happened, in what order, how long it took, and what failed (Tempo, or the `tree` printer).
- **Metrics:** how often and how fast, aggregated (Prometheus and Grafana).
- **Logs:** local detail, as JSON with `trace_id`, `span_id`, `request_id`, `thread_id` and `agent`.

The **trace ID** joins them. It is printed in the CLI footer, written on every JSON log line, and stored in every audit event (`audit_events.trace_id`, empty until now).

### OpenTelemetry and LangSmith
OpenTelemetry is the vendor-neutral backbone: our own spans and metrics, exported over OTLP to any backend. LangSmith is an LLM-specific tracer (prompts, model runs, evaluation). It is optional here and receives **redacted** payloads.

### GenAI semantic conventions
Model and agent spans use the standard attribute names (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.agent.name`, `gen_ai.tool.name`, `gen_ai.operation.name`), so generic GenAI dashboards understand them. Everything else is `aegisdesk.*`.

### Cardinality
Metric labels are things with few values: agent, tool, model, status, error category, policy reason. **Never a user.** One label value per employee would create one time series each, and leak identity into the monitoring system. Per-user questions are answered from traces and the audit trail.

### Redaction: observe actions and outcomes, not content or reasoning
We record IDs, names, counts, latencies, statuses and decisions, and never:
- message or prompt text;
- tool arguments or results;
- raw employee IDs (a hash is used instead);
- keys.

The `RedactingSpanProcessor` enforces an attribute allowlist and scrubs secret patterns before any exporter. The collector drops `gen_ai.prompt*` / `gen_ai.completion*` again. Nothing depends on chain-of-thought: the model's *decisions* (tool calls, routing plan) are visible as spans, its private reasoning is not.

---

## 2. What was built

| File | Purpose |
|---|---|
| `src/aegisdesk/observability/setup.py` | providers and exporters (`none`/`tree`/`console`/`otlp`), per-process, test-swappable |
| `…/tracing.py` | `span()`, `remote_child_span()`, attribute names, `model_attributes()`, `record_llm_call()` |
| `…/metrics.py` | the §24 catalogue as OpenTelemetry instruments; Prometheus names |
| `…/redaction.py` | allowlist, secret scrubbing, `pseudonym()`, `RedactingSpanProcessor` |
| `…/logging.py` | JSON formatter with trace and request context; `log_context()` |
| `…/propagation.py` | `traceparent` in MCP `_meta` (inject/extract) |
| `…/faults.py` | `AEGIS_FAULTS`: tool_error, tool_timeout, mcp_unavailable, retrieval_error |
| `…/langsmith.py` | optional tracer with redacted inputs and outputs |
| `…/tree.py` | span-tree printer (`--trace`) |
| instrumentation | `graphs/service_desk_graph.py`, `graphs/supervisor_graph.py`, `tools/executor.py`, `governance/gateway.py`, `tools/remote.py`, `mcp_servers/server.py`, `rag/retrieval/retriever.py`, `approvals/service.py`, `tools/access.py`, `cli.py` |
| `infra/observability/` | collector, Tempo, Prometheus, Grafana provisioning, dashboard |
| `scripts/build_dashboard.py` | the dashboard as code |
| `docker-compose.yml` | `observability` profile |

---

## 3. A trace (real output)

`aegisdesk agent --as E1004 --tools mcp_inprocess --trace "Please create an access request for FinanceERP for month-end reporting"`:

```
trace 6145f89bce2be2a5bebcc1cf5f610743
├─ aegisdesk.authenticate 0.0ms
├─ server/discover 0.3ms                       ← MCP SDK's own spans (tool discovery at startup)
├─ tools/list 0.3ms
│  …
└─ aegisdesk.request 46.0ms prompt=router@v1 status=final_answer pending=AP-0001 steps=5
   ├─ chat fake-scripted 7.8ms in=161 out=16 prompt=router@v1
   ├─ invoke_agent access 11.3ms status=final_answer
   │  ├─ chat fake-scripted 0.7ms in=184 out=22 prompt=access@v1
   │  ├─ mcp.call action/create_access_request 6.1ms
   │  │  ├─ tools/call create_access_request 3.1ms
   │  │  └─ mcp.server create_access_request 1.6ms          ← joined via traceparent in _meta
   │  │     └─ execute_tool create_access_request 1.4ms status=ok
   │  │        ├─ policy.evaluate 0.1ms decision=allow
   │  │        └─ tool.handler 0.4ms
   │  └─ chat fake-scripted 0.5ms in=198 out=18 prompt=access@v1
   └─ approval.await 0.1ms pending=AP-0001
```

This is the spec §23 trace shape: request → authentication → supervisor LLM → access agent (LLM, tool call → MCP request, policy evaluation) → approval.

---

## 4. Debugging an injected failure with telemetry

**Symptom:** the assistant says "The read tool server did not answer in time."

```
$ LOG_FORMAT=json AEGIS_FAULTS=tool_timeout:get_my_assets \
    aegisdesk agent --as E1004 --tools mcp_inprocess --trace --quiet "What laptop is assigned to me?"
{"level": "warning", "logger": "aegisdesk.tools.remote",
 "message": "MCP read: get_my_assets attempt 1/2 failed: timeout (request_id=7c4d0a16-…)",
 "trace_id": "8cf0ced699962ff03fd8ac00fbac55b7", "span_id": "15bb737048d27c57", "request_id": "7c4d0a16-…", …}
{"level": "warning", … "attempt 2/2 failed: timeout …", "trace_id": "8cf0ced699962ff03fd8ac00fbac55b7", …}
trace 8cf0ced699962ff03fd8ac00fbac55b7
└─ aegisdesk.request 42.3ms prompt=router@v1 status=final_answer steps=5
   ├─ chat fake-scripted 8.3ms in=157 out=12 prompt=router@v1
   └─ invoke_agent service_desk 8.9ms status=final_answer
      ├─ chat fake-scripted 0.8ms in=186 out=1 prompt=service_desk@v3
      ├─ mcp.call read/get_my_assets 1.8ms ✗ error=timeout
      ├─ mcp.call read/get_my_assets 0.5ms ✗ error=timeout
      └─ chat fake-scripted 0.5ms in=199 out=17 prompt=service_desk@v3
```

How to read it:
1. The log lines and the trace share `trace_id 8cf0…`, so this is the request.
2. `mcp.call read/get_my_assets` failed twice with `error=timeout`, and there is **no `mcp.server` child**: the request never reached the server. The client-side timeout is the cause, not the tool or the policy.
3. Two attempts: a read-only tool is retried once, by design (M5). A write would show one attempt.
4. In Grafana: `aegisdesk_tool_errors_total{tool="get_my_assets",category="timeout"}` rises, and the "Top failing tools" panel shows it.

The same exercise with `tool_error:get_my_assets` shows a different picture: `mcp.server` present, `execute_tool … ✗ error=internal_error`, `tool.handler ✗`, and an error log with `"exception.type": "InjectedFaultError"`. So the failure is inside the tool, on the server. `retrieval_error` marks `rag.retrieve ✗` under `execute_tool search_knowledge_base`. Each case is a test.

---

## 5. Tests (real results)

Full suite with PostgreSQL integration enabled:

```
413 passed, 22 skipped in 35.85s
```

The skips are the 21 `live` provider tests (no `ANTHROPIC_API_KEY`, no Ollama here) and one PostgreSQL-only contract case. `ruff check`, `ruff format --check` and `mypy` (strict, 140 files) are clean. The retrieval gate still passes (hit rate 0.89, 0 access violations).

New in this milestone:

| File | Tests | What it proves |
|---|---|---|
| `tests/unit/test_telemetry.py` | 16 | one request = one trace with the §23 shape and attributes; audit `trace_id` = span trace; **no user text, answer, secret or raw employee ID in any exported attribute**; allowlist and scrubbing; §24 metrics with no user labels; policy denials by reason; **trace continues across MCP** and tools are counted once; injected tool error, MCP timeout (two attempts, no server span) and retrieval failure visible in spans, metrics and logs; retrieval spans hold document IDs, not text; approval decide and resume traced; JSON logs join the trace and scrub secrets; tree printer; LangSmith redaction, off without a key, and tracer wiring (offline, stubbed client) |
| `tests/unit/test_observability_config.py` | 5 | configs parse; collector routes traces to Tempo and metrics to Prometheus with the redaction processor; **every dashboard metric is emitted by the app**; the dashboard matches its generator; compose mounts existing files |
| `tests/unit/test_cli.py` | +2 | `--trace` prints the span tree; `telemetry` prints no secrets |

**Not verified here:** Docker is not available in this environment (no daemon). The collector, Tempo, Prometheus and Grafana containers were **not started**. Their configuration is validated statically, and the OTLP path uses the standard OpenTelemetry exporters. Run `docker compose --profile observability up -d` locally to see the dashboards.

A lesson from building this: the first version of the LangSmith test constructed a real `langsmith.Client`, which immediately made a network call (`GET /info`) even though the test's comment said "nothing is sent". The test now stubs the client and asserts the redaction hooks are wired in.

---

## 6. Not built yet

- `/metrics`, `/health` and `/ready` HTTP endpoints arrive with the API (M10). CLI processes push metrics over OTLP instead of being scraped.
- Alerting rules (e.g. error rate, approval backlog) and SLOs.
- Sampling: every trace is recorded (fine for development; production would sample, keeping errors).
- Trace-to-logs in Grafana: it needs a log backend such as Loki. Logs go to stderr as JSON today.

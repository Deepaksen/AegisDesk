# Observability

Walkthrough, real outputs and test results: [M8 notes](milestones/M8-observability.md). Decision: [ADR 0013](adr/0013-opentelemetry-langsmith-redaction.md).

## Architecture

```
 aegisdesk CLI / MCP servers (one process or several)
 ┌───────────────────────────────────────────────────────────────────┐
 │ spans   aegisdesk.request → chat / invoke_agent / execute_tool …  │
 │ metrics aegisdesk.* counters and histograms (low-cardinality)     │     LangSmith (optional)
 │ logs    JSON lines with trace_id, request_id, thread_id           │──► LangChainTracer, content
 │         │                                                         │     redacted client-side
 │  RedactingSpanProcessor (allowlist + secret scrubbing)            │
 └─────────┬─────────────────────────────────────────────────────────┘
           │ OTLP/HTTP :4318   (TELEMETRY_EXPORTER=otlp)
           ▼
 ┌─────────────────────────┐  traces   ┌────────┐
 │ OpenTelemetry Collector │──────────►│ Tempo  │──┐
 │  attributes/redact      │           └────────┘  │    ┌─────────┐
 │  batch                  │  metrics  ┌──────────┐ ├───►│ Grafana │ dashboards + trace search
 │                         │──(:8889)─►│Prometheus│─┘    └─────────┘
 └─────────────────────────┘  scrape   └──────────┘

 audit_events.trace_id ──────────────► the same trace (from an audit row to what happened)
 MCP _meta.traceparent ──────────────► server spans join the client's trace, across processes
```

Without any infrastructure, `aegisdesk agent --trace` (or `TELEMETRY_EXPORTER=tree`) prints the span tree of each run.

## Span catalogue

| Span | Where | Key attributes |
|---|---|---|
| `aegisdesk.cli <command>` | CLI, one-shot commands | (root for authenticate + run) |
| `aegisdesk.authenticate` | CLI login | `aegisdesk.user.hash` |
| `aegisdesk.request` / `aegisdesk.resume` | `ThreadedGraphAgent.run/resume` | request_id, thread_id, user hash, agent + version, prompt, graph.steps, status, approval.pending |
| `chat <model>` | router, specialists, single agent | gen_ai.system, gen_ai.request.model, gen_ai.usage.input/output_tokens, prompt, requested_tools |
| `invoke_agent <agent>` | supervisor specialist node | gen_ai.agent.name, status |
| `execute_tool <tool>` | `ToolExecutor` (host or MCP server) | gen_ai.tool.name, agent, status, error.category |
| `policy.evaluate` | `ActionGateway` | policy.decision, policy.reasons, policy.version, approval.ids |
| `tool.handler` | inside `execute_tool` | tool name |
| `mcp.call <server>/<tool>` | `RemoteToolRunner` (client) | rpc.system=mcp, attempt, error.category |
| `mcp.server <tool>` | MCP server `tools/call` (child via traceparent) | tool, server, request_id |
| `rag.retrieve` | `Retriever` | document_ids, chunk_ids, top_score, no_evidence, top_k, embedding_model |
| `approval.await` / `approval.apply` | supervisor graph | approval.requests, approval.pending |
| `approval.decide` | `ApprovalService` | approval.id, approval.decision, changed, access_request.status, approver hash |
| `tools/list`, `tools/call`, `server/discover` | MCP SDK's own instrumentation | (filtered by the same allowlist) |

**Never recorded:** message text, prompts, completions, tool arguments or results, justifications, comments, raw employee IDs, tokens or keys.

## Metrics catalogue

Defined once in `observability/metrics.py::CATALOGUE`; Prometheus names shown.

| Prometheus series | Labels | Spec §24 |
|---|---|---|
| `aegisdesk_requests_total` | agent, operation | requests_total |
| `aegisdesk_requests_failed_total` | agent, operation | requests_failed_total |
| `aegisdesk_agent_invocations_total` | agent | agent_invocations_total |
| `aegisdesk_llm_calls_total` | model, agent | llm_calls_total |
| `aegisdesk_tool_calls_total` | tool, status | tool_calls_total |
| `aegisdesk_tool_errors_total` | tool, category | tool_errors_total |
| `aegisdesk_tool_latency_seconds_*` | tool | tool_latency_seconds |
| `aegisdesk_llm_latency_seconds_*` | model, agent | llm_latency_seconds |
| `aegisdesk_task_latency_seconds_*` | agent, operation | task_latency_seconds |
| `aegisdesk_tokens_input_total` / `_output_total` | model, agent | input/output_tokens_total |
| `aegisdesk_approval_requests_total` | step | approval_requests_total |
| `aegisdesk_approval_rejections_total` | reason (rejected, expired) | approval_rejections_total |
| `aegisdesk_policy_denials_total` | tool, reason | policy_denials_total |
| `aegisdesk_rag_retrieval_latency_seconds_*` | store | rag_retrieval_latency |
| `aegisdesk_rag_no_evidence_total` | — | rag_no_evidence_total |

`task_success_rate` is computed in the dashboard (1 − failed / total). Tool metrics are counted where the tool actually runs; a remote call that never reaches the server (timeout, unavailable) is counted by the client instead, so nothing is counted twice.

## Running the stack

```bash
docker compose --profile observability up -d          # collector, Tempo, Prometheus, Grafana
export TELEMETRY_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"
# Grafana: http://localhost:3000 → Dashboards → AegisDesk → "AegisDesk overview"
#          Explore → Tempo → search by trace ID (printed in the CLI footer and in audit events)
# Prometheus: http://localhost:9090 → aegisdesk_requests_total
```

The dashboard is generated by `scripts/build_dashboard.py`. A test checks that the committed JSON matches it and that every metric it queries is one the application emits.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TELEMETRY_EXPORTER` | `none` | `none` · `tree` · `console` · `otlp` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | SDK default | collector URL (standard OpenTelemetry variable) |
| `LOG_FORMAT` / `LOG_LEVEL` | `text` / `WARNING` | `json` adds trace_id, span_id, request_id, thread_id, agent |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | off | optional LangSmith tracing (content redacted) |
| `AEGIS_FAULTS` | none | deliberate failures for the debugging exercise |

`aegisdesk telemetry` prints the effective configuration without secrets.

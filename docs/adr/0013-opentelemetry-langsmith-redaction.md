# ADR 0013: OpenTelemetry as the telemetry backbone, LangSmith optional, redaction by allowlist

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §20, §23 and §24 ask for:
- LangSmith for agent tracing and evaluation, and OpenTelemetry for vendor-neutral distributed telemetry;
- Prometheus, Grafana, Tempo or Jaeger, and structured JSON logs;
- a trace per request that spans agents, LLM calls, tools, MCP, policy and approval;
- specific attributes and metrics;
- sensitive data redacted before export, and no dependence on chain-of-thought.

The user chose the Grafana stack, with LangSmith optional and off by default.

## Decision
1. **Instrument with the OpenTelemetry API.** Spans are made at our own seams:
   - request, router and model calls, specialists;
   - tool execution, policy, handler;
   - MCP client and server, retrieval, approvals.

   The instrumentation reuses timings the code already measures. Attribute names follow the OpenTelemetry GenAI semantic conventions (`gen_ai.*`) where they exist, and `aegisdesk.*` otherwise.
2. **Metrics are OpenTelemetry instruments** defined in one catalogue. Labels are low-cardinality, and a user is never a label.
3. **One trace across processes:** W3C `traceparent` travels in MCP `_meta`, and audit events store `trace_id`.
4. **Redaction by allowlist.**
   - We never set content attributes.
   - `RedactingSpanProcessor` drops non-allowlisted keys and scrubs secrets before any exporter.
   - The collector deletes `gen_ai.prompt*` / `gen_ai.completion*` again.
   - Users appear only as a hash.
   - JSON logs use the same scrubbing.
5. **Exporters are chosen by configuration:** `none` (default; trace IDs still exist), `tree` (local learning), `console`, and `otlp` → collector → Tempo + Prometheus → Grafana.
6. **LangSmith is optional.** A `LangChainTracer` whose client hides inputs and outputs through `redact_payload`. Run metadata (agent, versions, prompt, request and thread IDs) is attached through `RunnableConfig`.
7. **Deliberate faults** (`AEGIS_FAULTS`) are injected at seams that already handle the real failure, so debugging practice exercises the production error paths.

## Consequences
- A single trace ID links the CLI footer, JSON logs, audit rows, MCP server work and Grafana/Tempo.
- The telemetry is safe to send to a third party by construction. The cost is less detail: traces show *that* a tool failed and why (category), not the payloads. Payload-level debugging uses the audit trail and the local thread, under access control.
- LangSmith, when enabled, sees structure and metadata but not conversation text. Reviewing model outputs there would need an explicit, audited opt-in, which is not built.
- No auto-instrumentation libraries. Spans are explicit, which is more code but gives stable names, and the redaction allowlist is easy to maintain.
- The OpenTelemetry global providers can be set once per process, so our code uses its own provider references and tests swap in-memory exporters per test.

## Alternatives considered
- **LangSmith only:** rich LLM traces, but not vendor-neutral, and no infrastructure metrics or cross-service traces.
- **OpenLLMetry / auto-instrumentation of LangChain:** fast, but it records prompts and completions by default, which fights the redaction goal.
- **Prometheus client library directly:** simpler for metrics, but a second mechanism next to OpenTelemetry, and it needs a scrape endpoint in short-lived CLI processes.
- **Jaeger instead of Tempo:** a simpler trace UI, but the user chose the Grafana stack.

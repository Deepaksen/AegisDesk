# AegisDesk

An enterprise-style **agentic AI service desk** for the fictional Northstar Industries, built milestone by milestone as a learning platform. Employees will ask IT questions, check assets and tickets, and request application access. Sensitive actions go through deterministic policy and human approval, and never on the model's say-so.

All data is synthetic.

## Status

| Milestone | What it adds | Notes |
|---|---|---|
| M0 LLM fundamentals ✅ | Provider-agnostic model layer (Anthropic, local Ollama, offline fake), chat, structured output, versioned prompts, token and latency measurement | [M0](docs/milestones/M0-llm-fundamentals.md) |
| M1 Single agent + tools ✅ | Service Desk agent with a hand-written tool-calling loop; tools for own assets and tickets and for ticket creation; trusted identity; idempotent writes; security tests | [M1](docs/milestones/M1-single-agent-tools.md) |
| M2 LangGraph ✅ | The same agent as an explicit LangGraph graph: typed state, nodes, conditional edges, SQLite checkpointing (threads survive restarts), per-node streaming, thread ownership | [M2](docs/milestones/M2-langgraph.md) |
| M3 RAG ✅ | 12-document knowledge base; chunking, embeddings (offline hashing or Ollama), in-memory and PostgreSQL + pgvector stores, access filtering before ranking, grounded answers with verified citations, knowledge tools for the agent, retrieval eval gate | [M3](docs/milestones/M3-rag.md) |
| M4 Multi-agent ✅ | Supervisor with a structured router and deterministic dispatch; Knowledge, Service Desk and Access specialists as subgraphs with isolated tools and context; bounded handoffs; code-checked citations; access domain with deterministic eligibility (requests recorded, never granted) | [M4](docs/milestones/M4-multi-agent.md) |
| M5 MCP ✅ | Enterprise tools behind a read and an action MCP server (in-process or Streamable HTTP); per-call signed delegation tokens carry user, agent, request ID and server audience; discovery with risk annotations; timeouts, read-only retries, no write retries; local vs remote comparison | [M5](docs/milestones/M5-mcp.md) |
| M6 Governance ✅ | Action gateway on every tool call (host and MCP servers): deterministic policy from `config/policy.yaml` (agent grants, risk classes, authorized writes, forbidden actions, environments), fail-closed; append-only audit events in PostgreSQL (UPDATE/DELETE/TRUNCATE rejected); deliberate bypass attempts tested | [M6](docs/milestones/M6-governance.md) |
| M7 Human approval ✅ | Sensitive access requests pause the LangGraph workflow (`interrupt`), survive restarts (PostgreSQL access store + checkpointer), and resume on the approver's decision; approver rules with separation of duties, expiry and idempotency; provisioning only by the workflow identity with gateway-verified approval evidence; audited end to end | [M7](docs/milestones/M7-approvals.md) |
| M8 Observability ✅ | OpenTelemetry traces (one trace per request, across MCP servers) with GenAI attributes, the spec's metrics, JSON logs with trace context, optional LangSmith, allowlist redaction; Collector + Tempo + Prometheus + Grafana dashboard (docker compose profile); `--trace` span trees; injected faults for debugging | [M8](docs/milestones/M8-observability.md) |
| M9 Evaluations ✅ | 60-case golden dataset and an 8-case adversarial suite run through the real system; deterministic, RAG and trajectory checks from trajectories, audit, spans and data changes; latency, tokens and cost; multi-agent vs single-agent comparison; safety and regression gates in CI; opt-in LLM judge; per-request write budget (found by the adversarial suite) | [M9](docs/milestones/M9-evaluations.md) |
| M10 API + UI ✅ | FastAPI service (threads, messages with SSE streaming, approvals, audit, health, readiness, Prometheus metrics, OpenAPI) as a thin adapter over a shared runtime; gateway-header identity; idempotency keys; problem+json errors; Streamlit employee, manager and audit views over HTTP; Dockerfile and full `docker compose up` stack | [M10](docs/milestones/M10-api-ui.md) |
| M11 Reliability ✅ | Every spec failure injected and handled: model timeouts/outages (classified, retried with backoff, circuit breaker, safe answer, API 503 + Retry-After), malformed model output, database outages (fail closed, write-ahead audit for approvals), MCP outages (breaker per server), tool failures (reconciler for approved-but-unprovisioned access), duplicate requests (idempotent replay, PostgreSQL-backed); 13-case reliability suite gated in CI | [M11](docs/milestones/M11-reliability.md) |

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the target architecture and progress.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # optional; defaults use the offline fake model

uv run aegisdesk config
uv run aegisdesk chat "How do I clear my DNS cache?"
uv run aegisdesk triage "My VPN drops every 10 minutes" --show-messages
uv run aegisdesk repeat "Suggest a name for a new laptop" --runs 5 --temperature 1.0

# Agents (simulated login as a synthetic employee); default engine: supervisor + specialists
uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"   # prints a thread_id
uv run aegisdesk agent --as E1004 "Please create an access request for FinanceERP for month-end reporting"
uv run aegisdesk agent --as E1004 --thread <id> "Show me ticket INC-1001"  # continue, even after restart
uv run aegisdesk thread <id> --as E1004      # read a stored conversation
uv run aegisdesk agent --as E1004            # interactive session
uv run aegisdesk agent --as E1004 --engine graph "..."  # the single Service Desk agent (M2/M3)
uv run aegisdesk agent --as E1004 --engine loop "..."   # the M1 hand-written loop

# Tools over MCP (M5)
uv run aegisdesk mcp tools                                            # discovery on both servers
uv run aegisdesk agent --as E1004 --tools mcp_inprocess "What laptop is assigned to me?"
MCP_TOKEN_SECRET=<32+ chars> uv run aegisdesk mcp serve               # HTTP: :8765/read/mcp, /action/mcp
MCP_TOKEN_SECRET=<same> TOOL_TRANSPORT=mcp_http uv run aegisdesk agent --as E1004 "..."

# Human approval (M7): durable across processes with PostgreSQL
export DATA_STORE=postgres CHECKPOINT_STORE=postgres AUDIT_STORE=postgres DATABASE_URL=...
uv run aegisdesk db init                                            # migrations + checkpoint tables + seed
uv run aegisdesk agent --as E1004 "Please create an access request for FinanceERP for month-end reporting"
uv run aegisdesk approvals list --as E1010
uv run aegisdesk approvals approve AP-0001 --as E1010 --comment "ok"   # resumes the employee's thread

# Reliability (M11)
uv run aegisdesk eval golden --dataset evals/reliability/faults_v1.yaml --config multi
AEGIS_FAULTS=model_timeout:router uv run aegisdesk api serve     # 503 + Retry-After, then circuit_open
uv run aegisdesk approvals reconcile --as E1006                  # finish approved-but-unprovisioned access

# API + UI (M10)
uv run aegisdesk api serve                                  # http://127.0.0.1:8000/docs
uv run streamlit run apps/ui/streamlit_app.py               # http://localhost:8501
curl -s -X POST -H 'X-Employee-Id: E1004' localhost:8000/api/v1/threads
docker compose up --build                                   # the whole platform (see docs/RUNBOOK.md)

# Evaluations (M9)
uv run aegisdesk eval golden --config multi --compare single          # 60 cases, two versions side by side
uv run aegisdesk eval golden --dataset evals/adversarial/security_v1.yaml

# Observability (M8)
uv run aegisdesk agent --as E1004 --trace "What laptop is assigned to me?"   # span tree, no infrastructure
LOG_FORMAT=json AEGIS_FAULTS=tool_error:get_my_assets uv run aegisdesk agent --as E1004 --trace "What laptop is assigned to me?"
docker compose --profile observability up -d && export TELEMETRY_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# Governance (M6)
uv run aegisdesk policy check --as E1004 --agent knowledge --tool create_ticket   # DENY + reasons
AUDIT_STORE=postgres uv run aegisdesk audit --user E1004                          # needs DATABASE_URL + migrations

# Knowledge base (RAG)
uv run aegisdesk rag search "my vpn keeps disconnecting" --as E1004   # inspect chunks and scores
uv run aegisdesk ask "How do I configure VPN on macOS?" --as E1004    # answer with citations
uv run aegisdesk eval rag                                            # retrieval metrics
```

For a persistent pgvector index: `docker compose up -d postgres`, set `DATABASE_URL` and `VECTOR_STORE=pgvector`, then run `uv run alembic upgrade head` and `uv run aegisdesk rag ingest`. For semantic embeddings: `ollama pull nomic-embed-text` and `EMBEDDING_PROVIDER=ollama`.

Synthetic users include `E1004` (finance), `E1001` (engineering), `E1005` (contractor), `E1006` (IT admin), `E1010` (finance manager), `E1014` (HR admin), `E1015` (security approver), `E1016` (data owner) and `E1007` (terminated, so login is refused). See [`data/seed/`](data/seed/).

### Choosing a model

Set these in `.env` or your shell. Models must be listed in [`config/models.yaml`](config/models.yaml).

| Provider | Settings |
|---|---|
| Offline fake (default) | `MODEL_PROVIDER=fake` `MODEL_NAME=fake-scripted` |
| Anthropic | `MODEL_PROVIDER=anthropic` `MODEL_NAME=claude-haiku-4-5-20251001` (or `claude-sonnet-5-5`) `ANTHROPIC_API_KEY=…` |
| Ollama (local) | `ollama pull llama3.2`, then `MODEL_PROVIDER=ollama` `MODEL_NAME=llama3.2` (or `qwen2.5:7b`) |

Also available: `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`, `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES`, `OLLAMA_BASE_URL`, `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_HANDOFFS`, `CHECKPOINT_DB_PATH` (default `.aegisdesk/checkpoints.sqlite`), and the RAG settings in [`.env.example`](.env.example).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not live"   # offline and deterministic (what CI runs)
AEGIS_TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest -m "not live"   # + pgvector contract tests
uv run pytest -m live         # real providers; skips any without a key or a running Ollama
```

## Documentation

* [`docs/AegisDesk-High-Level-Design.docx`](docs/AegisDesk-High-Level-Design.docx): high-level design document (Word; 20 diagrams: context, architecture, data and process flows, governance, observability, evaluation, reliability, deployment, data model; decisions and patterns)
* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): target architecture and current state
* [`docs/milestones/`](docs/milestones/): per-milestone learning notes
* [`docs/AGENT_DESIGN.md`](docs/AGENT_DESIGN.md): agents, tools, handoffs and isolation
* [`docs/RAG_DESIGN.md`](docs/RAG_DESIGN.md): knowledge base and retrieval design
* [`docs/EVALUATION.md`](docs/EVALUATION.md): datasets, checks, metrics, gates and the judge
* [`docs/API.md`](docs/API.md): the HTTP API (endpoints, auth, streaming, errors, idempotency)
* [`docs/RUNBOOK.md`](docs/RUNBOOK.md): starting, checking and troubleshooting the platform
* [`docs/adr/`](docs/adr/): architecture decision records

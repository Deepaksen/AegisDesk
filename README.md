# AegisDesk

**A governed, multi-agent IT service desk.** Employees of the fictional Northstar Industries ask AegisDesk IT questions, check their laptops and tickets, open tickets and request access to applications. Language-model agents understand the request and propose actions; deterministic code checks identity, policy and human approval before anything happens, and records every decision in an audit trail.

* Answers from company documents, with citations checked in code
* The employee’s own assets and tickets, and new tickets
* Access requests with manager, security and data-owner approval that pause the conversation and resume it after the decision
* A web UI, an HTTP API with streaming, and a CLI
* Tracing, metrics and logs; evaluation suites with safety gates in CI; safe behaviour when models, databases or tools fail
* Runs fully offline with a deterministic fake model; switch to Anthropic or a local Ollama model with two settings

All employees, assets, tickets, applications and documents are **synthetic**.

## Contents

1. [What it does](#what-it-does)
2. [Solution approach](#solution-approach)
3. [Agent orchestration](#agent-orchestration)
4. [Quick start](#quick-start)
5. [Setup guide](#setup-guide)
6. [User guide](#user-guide)
7. [Operating AegisDesk](#operating-aegisdesk)
8. [Security model](#security-model)
9. [Project structure](#project-structure)
10. [Development](#development)
11. [Documentation](#documentation)
12. [Limitations and what's next](#limitations-and-whats-next)

---

## What it does

| You want to… | Ask, for example | Handled by | What you get |
|---|---|---|---|
| Get an answer from IT policy and how-to documents | “How do I configure VPN on macOS?” | Knowledge agent | An answer grounded in the knowledge base, with citations such as *VPN Troubleshooting Guide* (`DOC-VPN-001`) |
| See your equipment | “What laptop is assigned to me?” | Service Desk agent | Your assets (laptop, monitor), never anyone else’s |
| Follow up on your tickets | “Show me ticket INC-1001” | Service Desk agent | The ticket, if it is yours |
| Report a problem | “Create a ticket: my external monitor flickers” | Service Desk agent | A new ticket (`INC-…`); retries never create duplicates |
| Get access to an application | “Please create an access request for FinanceERP for month-end reporting” | Access agent | An access request (`AR-…`); approvals are created for the right people and the conversation waits for them |
| Approve or reject a request | (UI *Approvals* tab, API or CLI) | Approval service | The decision is audited, the employee’s conversation resumes, and access is granted only when every approval is recorded |
| See what happened | (UI *Audit trail* tab, API or CLI) | Audit log | Every policy decision and outcome, with request and trace IDs |

| Who | Role in AegisDesk |
|---|---|
| Employee | Asks questions, checks assets and tickets, requests access |
| Manager, security approver, data owner | Approve or reject the steps of an access request assigned to them |
| IT admin | Sees every audit event; finishes requests that were approved but not provisioned |

---

## Solution approach

### The guiding principle

> Language models do probabilistic reasoning. Deterministic software does authentication, authorization, policy, approval enforcement, validation, business rules, persistence, audit and retries. **The model is never the security authority**: it proposes actions, and code decides whether they happen.

### Architecture at a glance

```mermaid
flowchart TB
  people["Employee · Manager · IT admin"] --> ui["Streamlit UI :8501"]
  people --> cli["CLI: aegisdesk"]
  ui -- "HTTP + X-Employee-Id" --> api["FastAPI :8000<br/>auth · SSE · idempotency · problem+json"]
  api --> rt["AegisRuntime"]
  cli --> rt
  rt --> lg["LangGraph supervisor graph<br/>router + three specialist agents"]
  lg --> llm["Model layer<br/>fake · Anthropic · Ollama<br/>retries · circuit breaker"]
  lg --> rag["RAG retriever<br/>access filter before ranking"]
  lg --> exec["Tool executor<br/>allowlist · schema"]
  exec --> gw["Policy gateway<br/>config/policy.yaml"]
  gw --> audit[("Audit log<br/>append-only")]
  gw --> tools["Tools: in-process or MCP servers<br/>signed delegation tokens"]
  tools --> db[("PostgreSQL<br/>access data · approvals · checkpoints")]
  rag --> vec[("pgvector or in-memory index")]
  rt -.-> otel["OpenTelemetry: Tempo · Prometheus · Grafana"]
```

### Key design choices

| Choice | Why | Decision record |
|---|---|---|
| A provider-agnostic model layer with an allowlist, and an offline fake model as the default | Runs anywhere, tests are deterministic, providers are swappable | [ADR 0002](docs/adr/0002-provider-agnostic-model-layer.md) |
| Tools take identity from the authenticated session, never from model arguments | The model cannot act as someone else | [ADR 0003](docs/adr/0003-tools-take-identity-from-trusted-context.md) |
| LangGraph for explicit, checkpointed workflows | Conversations survive restarts; approvals can pause and resume | [ADR 0004](docs/adr/0004-why-langgraph.md) |
| A router model plus a **deterministic** supervisor and narrow specialist agents | Each agent sees only its own tools and context; dispatch is code | [ADR 0007](docs/adr/0007-specialised-agents-deterministic-supervisor.md) |
| Enterprise tools behind MCP servers, with per-call signed delegation tokens | Servers know the user, the acting agent and the request without trusting the model | [ADR 0008](docs/adr/0008-mcp-servers-with-delegation-tokens.md) |
| A deterministic policy engine and gateway on every tool call | Risk levels, per-agent grants, forbidden actions, write budget, fail closed | [ADR 0009](docs/adr/0009-deterministic-policy-engine.md) |
| An append-only audit log, written **before** acting | No record, no write | [ADR 0010](docs/adr/0010-append-only-audit-store.md) |
| Human approval as a durable interrupt and resume | Sensitive access needs people, and waiting must survive restarts | [ADR 0011](docs/adr/0011-approval-interrupt-resume.md) |
| PostgreSQL with pgvector for documents, workflow state and audit | One durable store | [ADR 0005](docs/adr/0005-postgresql-pgvector.md), [ADR 0012](docs/adr/0012-postgres-for-workflow-state.md) |
| OpenTelemetry with allowlist redaction | One trace per request across processes, without leaking content | [ADR 0013](docs/adr/0013-opentelemetry-langsmith-redaction.md) |
| Deterministic-first evaluation with CI gates | Behaviour is measured, and safety regressions block merges | [ADR 0014](docs/adr/0014-deterministic-first-evaluation.md) |
| A thin FastAPI adapter over a shared runtime; identity from an authenticating gateway | The API, UI and CLI behave identically | [ADR 0015](docs/adr/0015-fastapi-thin-adapter-trusted-header.md), [ADR 0016](docs/adr/0016-streamlit-initially.md) |
| Degrade safely: classified failures, bounded retries, circuit breakers, idempotent replay | Outages produce clear answers and correct status codes, never half-done writes | [ADR 0017](docs/adr/0017-resilience-degrade-safely.md) |

The full design, with 20 diagrams, is in the [High-Level Design](docs/AegisDesk-High-Level-Design.docx).

---

## Agent orchestration

### One turn through the supervisor graph

Every message runs through one LangGraph graph, checkpointed after each step. Blue steps are deterministic code; orange steps call a language model.

```mermaid
flowchart TB
  msg(["Employee message"]) --> st["start_turn<br/>bind the thread to its owner, reset limits"]
  st --> cl["classify_request<br/>router model returns a RoutingPlan"]
  cl --> sv{"supervisor<br/>deterministic dispatch"}
  sv -- "next task" --> SPEC
  SPEC -- "answer or handoff" --> sv
  subgraph SPEC["Specialist agents"]
    direction LR
    kn["Knowledge"] ~~~ sd["Service Desk"] ~~~ ac["Access"]
  end
  sv -- "no tasks left" --> rs["respond<br/>combine answers in code, no new facts"]
  rs -- "no access request" --> out(["Answer, activity, citations, references"])
  rs -- "access request created" --> aw["await_approval<br/>checkpoint and pause"]
  aw -- "approver decides" --> ap["apply_approvals<br/>provision as the workflow identity"]
  ap --> out
  classDef code fill:#E8F0FB,stroke:#2F5597,color:#1F2937
  classDef model fill:#FFF4E5,stroke:#C27C0E,color:#1F2937
  class st,sv,rs,aw,ap code
  class cl,kn,sd,ac model
```

1. **Route.** A small structured-output call turns the message into a `RoutingPlan`: up to three tasks, each for a known agent, or “out of scope”. If the plan can’t be parsed, nothing runs and the user is asked to rephrase.
2. **Dispatch.** The supervisor is code. It runs the pending tasks in order. A specialist may ask for a **handoff** to another specialist; code accepts it only within the budget (`AGENT_MAX_HANDOFFS`), and never back to the same agent or as a duplicate.
3. **Specialists work.** Each specialist runs its own tool loop (below), with its own prompt and tools. It sees the conversation and its task, never another agent’s tool traffic.
4. **Respond.** One task: its answer as is. Several: sections joined by code, so no new facts can appear.
5. **Approve.** If an access request was created, the graph pauses durably. When the approvers decide, through the UI, API or CLI, the same thread resumes and provisioning runs as the workflow identity, the only identity that may grant access.

### Inside a specialist: the tool loop and the policy gateway

```mermaid
flowchart TB
  cm["call_model<br/>this agent's prompt and tools only"] -- "tool call" --> ex["Tool executor<br/>allowlisted tool? valid arguments?"]
  ex --> pol{"Policy gateway<br/>agent grant · risk · role · environment · write budget"}
  pol -- "DENY or REQUIRE_APPROVAL" --> back["Refusal returned to the model,<br/>nothing runs"]
  pol -- "ALLOW" --> au["Audit the decision first"]
  au --> run["Run the tool<br/>in-process, or MCP with a delegation token"]
  run --> res["Audit the outcome, return the result"]
  back --> cm
  res --> cm
  cm -- "final answer" --> done(["Back to the supervisor"])
  cm -- "step or tool-call limit" --> lim(["Fixed limit message"])
  classDef code fill:#E8F0FB,stroke:#2F5597,color:#1F2937
  classDef model fill:#FFF4E5,stroke:#C27C0E,color:#1F2937
  class ex,pol,au,run,res,back code
  class cm model
```

| Agent | Prompt | Tools | Writes |
|---|---|---|---|
| Router (supervisor) | `router@v1`, structured `RoutingPlan` | none | none |
| Knowledge | `knowledge@v1` | `search_knowledge_base`, `retrieve_document`, `request_handoff` | none; citations must come from documents retrieved in this run |
| Service Desk | `service_desk@v3` | `get_my_assets`, `list_my_tickets`, `get_ticket`, `create_ticket`, `add_ticket_comment`, `search_knowledge_base`, `request_handoff` | tickets and comments (medium risk, idempotent) |
| Access | `access@v1` | `get_employee_profile`, `list_my_access`, `get_application`, `check_access_eligibility`, `create_access_request`, `request_handoff` | access requests (medium risk); eligibility is recomputed by the tool |
| Approval workflow (not a model) | — | `provision_access` | grants access (high risk), only with approval evidence the gateway looks up itself |

**Rules that hold whatever the model writes:** identity comes from the session and no tool has an employee-ID parameter; unknown tools are refused; schemas forbid extra fields; loops are bounded (`AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`); at most `limits.max_writes_per_request` writes per request; threads belong to the employee who started them. Details: [docs/AGENT_DESIGN.md](docs/AGENT_DESIGN.md), [docs/GOVERNANCE_DESIGN.md](docs/GOVERNANCE_DESIGN.md).

### A sensitive request, end to end

```mermaid
sequenceDiagram
  autonumber
  actor E as Employee (E1004)
  participant A as API and runtime
  participant G as Supervisor graph
  participant P as Policy gateway
  participant S as Stores and audit
  actor M as Manager (E1010)
  E->>A: "I need FinanceERP access for month-end reporting"
  A->>G: run the turn
  G->>P: create_access_request (medium risk)
  P->>S: audit decision, create AR-1013 and approval AP-0001
  G->>S: pause the thread (checkpoint)
  A-->>E: "Manager approval is required" (activity: waiting)
  Note over A,S: the pause is durable, processes may restart
  M->>A: approve AP-0001 with a comment
  Note over A: checks: not own request, right approver, not expired
  A->>S: audit the decision first, then record it
  A->>G: resume the thread
  G->>P: provision_access as the workflow identity (high risk)
  P->>S: approval evidence found, ALLOW, grant, audit
  A-->>M: decision recorded and the resumed answer
  A-->>E: the thread now says access has been granted
```

---

## Quick start

Requires **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)**. No API keys, database or Docker needed: the default model is an offline, deterministic fake.

```bash
git clone https://github.com/Deepaksen/AegisDesk.git && cd AegisDesk
uv sync

uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"
uv run aegisdesk ask --as E1004 "How do I configure VPN on macOS?"
uv run aegisdesk agent --as E1004 "Please create an access request for FinanceERP for month-end reporting"
```

The agent prints its routing, each model and tool step, the answer, and the request, thread and trace IDs:

```
→ router  in=157 out=12 11ms -> service_desk: 'What laptop is assigned to me?'
  · [service_desk] step 1 model  in=186 out=1 2ms -> get_my_assets
  · [service_desk] step 1 tool   get_my_assets({}) ok 0.9ms
  · [service_desk] step 2 model  in=190 out=8 1ms -> final answer
← service_desk done
Assistant: [fake model] Tool results: {"assets":[{"asset_tag":"NS-LT-0101", ..., "model":"Dell Latitude 7440", ...}]}
```

The fake model echoes tool results instead of writing prose; [choose a real model](#choosing-a-model) for natural answers. Then start the API and the web UI in two terminals:

```bash
uv run aegisdesk api serve                          # http://127.0.0.1:8000/docs
uv run streamlit run apps/ui/streamlit_app.py       # http://localhost:8501
```

---

## Setup guide

### Prerequisites

| Needed for | What |
|---|---|
| Everything | Python 3.12+, [uv](https://docs.astral.sh/uv/) |
| Durable approvals, audit and vector search | PostgreSQL 16 with the pgvector extension (or `docker compose up -d postgres`) |
| The full stack in containers | Docker with Compose |
| Real answers | An Anthropic API key, or [Ollama](https://ollama.com/) running locally |

### Option A: local and offline (memory stores)

```bash
uv sync
cp .env.example .env              # optional: the defaults are offline
uv run aegisdesk config           # the effective configuration (secrets masked)
uv run aegisdesk api serve
uv run streamlit run apps/ui/streamlit_app.py
```

Requests and approvals live inside the API process, so approve in the **UI or API**, not with the CLI from another process. Conversations are kept in SQLite (`.aegisdesk/checkpoints.sqlite`).

### Option B: local with PostgreSQL (durable, shared between processes)

```bash
docker compose up -d postgres     # or any PostgreSQL 16 with pgvector
export DATABASE_URL=postgresql+psycopg://aegisdesk:aegisdesk@localhost:5432/aegisdesk
export DATA_STORE=postgres CHECKPOINT_STORE=postgres AUDIT_STORE=postgres VECTOR_STORE=pgvector
uv run aegisdesk db init          # migrations, checkpoint tables, seed rows
uv run aegisdesk rag ingest       # chunk, embed and index the documents
```

Now the CLI, API and UI share requests, approvals, conversations and the audit log, and everything survives restarts.

### Option C: the full stack with docker compose

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # put it in .env as MCP_TOKEN_SECRET=
docker compose up --build                                     # postgres, migrate, mcp, api, ui
docker compose --profile observability up --build             # + collector, Tempo, Prometheus, Grafana
```

| Service | URL | Notes |
|---|---|---|
| Web UI | http://localhost:8501 | pick a synthetic employee in the sidebar |
| API | http://localhost:8000/docs | OpenAPI; send `X-Employee-Id` |
| Grafana | http://localhost:3000 | with the observability profile and `TELEMETRY_EXPORTER=otlp` in `.env` |
| Prometheus | http://localhost:9090 | scrapes the collector and the API’s `/metrics` |
| MCP servers | internal only (`mcp:8765`) | the API is their only client |

Start-up order is enforced: `postgres` (healthy) → `migrate` (migrations, seed rows, knowledge index) → `mcp` (healthy) → `api` (ready) → `ui`. Troubleshooting: [docs/RUNBOOK.md](docs/RUNBOOK.md).

### Choosing a model

Set these in `.env` or the environment. Models must be listed in [`config/models.yaml`](config/models.yaml).

| Provider | Settings |
|---|---|
| Offline fake (default) | `MODEL_PROVIDER=fake` `MODEL_NAME=fake-scripted` |
| Anthropic | `MODEL_PROVIDER=anthropic` `MODEL_NAME=claude-haiku-4-5-20251001` (or `claude-sonnet-5-5`) `ANTHROPIC_API_KEY=…` |
| Ollama (local) | `ollama pull llama3.2`, then `MODEL_PROVIDER=ollama` `MODEL_NAME=llama3.2` (or `qwen2.5:7b`) |

For semantic search instead of the offline hashing embedder: `ollama pull nomic-embed-text` and `EMBEDDING_PROVIDER=ollama`.

<details>
<summary><b>Configuration reference</b> (all settings are in <a href=".env.example"><code>.env.example</code></a>)</summary>

| Area | Settings |
|---|---|
| Environment | `AEGIS_ENV` (`development`; `production` makes the policy read-only) |
| Model | `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`, `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES`, `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL` |
| Agent limits | `AGENT_MAX_STEPS` (6), `AGENT_MAX_TOOL_CALLS` (8), `AGENT_MAX_HANDOFFS` (2) |
| Knowledge base | `EMBEDDING_PROVIDER` (`hash` or `ollama`), `EMBEDDING_MODEL`, `VECTOR_STORE` (`memory` or `pgvector`), `RAG_TOP_K`, `RAG_MIN_SCORE` |
| Tools over MCP | `TOOL_TRANSPORT` (`local`, `mcp_inprocess`, `mcp_http`), `MCP_TOKEN_SECRET` (32+ characters), `MCP_READ_URL`, `MCP_ACTION_URL`, `MCP_TIMEOUT_SECONDS` |
| Governance | `POLICY_PATH` (default `config/policy.yaml`), `AUDIT_STORE` (`memory` or `postgres`) |
| Approvals and state | `DATA_STORE` (`memory` or `postgres`), `CHECKPOINT_STORE` (`sqlite` or `postgres`), `CHECKPOINT_DB_PATH`, `APPROVAL_TTL_HOURS` (168), `DATABASE_URL` |
| Observability | `TELEMETRY_EXPORTER` (`none`, `tree`, `console`, `otlp`), `OTEL_EXPORTER_OTLP_ENDPOINT`, `LOG_FORMAT` (`text` or `json`), `LOG_LEVEL`, `LANGSMITH_*` |
| Reliability | `BREAKER_FAILURE_THRESHOLD` (5), `BREAKER_RESET_SECONDS` (30), `IDEMPOTENCY_TTL_HOURS` (24), `IDEMPOTENCY_STALE_SECONDS` (300), `AEGIS_FAULTS` (fault injection, never in production) |
| Evaluations | `EVAL_JUDGE_PROVIDER`, `EVAL_JUDGE_MODEL` (optional LLM judge) |
| UI | `AEGIS_API_URL` (default `http://127.0.0.1:8000`), `GRAFANA_URL` |

</details>

---

## User guide

### Synthetic users

Sign in as any of these (the UI sidebar, `--as` in the CLI, or `X-Employee-Id` for the API). The full directory is in [`data/seed/employees.json`](data/seed/employees.json).

| ID | Who | Try |
|---|---|---|
| `E1004` | Aisha Khan, finance | VPN questions, her laptop, tickets `INC-1001` and `INC-1002`, a FinanceERP access request |
| `E1010` | Grace Liu, finance manager | approve or reject Aisha’s requests |
| `E1015` | Ines Duarte, security approver | the security step for privileged applications |
| `E1016` | Viktor Lindqvist, data owner | the data-owner step for confidential applications |
| `E1006` | Lena Hoffmann, IT admin | every audit event; `approvals reconcile` |
| `E1001` | Priya Raman, engineering | the same questions as a different user |
| `E1005` | Tom Becker, contractor | fewer documents and applications than employees |
| `E1014` | Nora Petersen, HR admin | another department’s view |
| `E1007` | Daniel Ortiz, terminated | login is refused |

### Using the web UI

Open http://localhost:8501, pick an employee in the sidebar and use the three tabs:

* **Service desk:** chat. While the agent works, an **activity** list shows what is happening (“Routing your request to: access.”, “Access request AR-1013 created.”, “Manager approval is required.”). Under each answer you see **citations**, **references** (ticket and request IDs), pending approvals, and the trace ID (a link to Grafana when the observability stack runs).
* **Approvals:** the steps waiting for you, with approve and reject buttons and a comment field. Approving resumes the requester’s conversation.
* **Audit trail:** policy decisions and outcomes for your own requests; IT admins see everyone’s.

| Employee: citation, references, pause | Manager: pending approval | After approval: resumed answer |
|---|---|---|
| ![employee view](docs/milestones/img/m10-ui-employee.png) | ![manager view](docs/milestones/img/m10-ui-manager.png) | ![approved](docs/milestones/img/m10-ui-approved.png) |

The screenshots were taken with the offline fake model, which is why answers look like JSON. With a real model they are prose; the activity, citations, references and approval panels come from the API either way.

### Walkthrough: requesting and approving access

1. As **E1004**, ask: *Please create an access request for FinanceERP for month-end reporting.* The activity shows the request `AR-1013` and “Manager approval is required”, and the conversation pauses.
2. Switch the sidebar to **E1010** and open **Approvals**. Approve `AP-0001` with a comment.
3. Switch back to **E1004**: the conversation has resumed with “Access has been granted”.
4. Open **Audit trail**: `create_access_request` allow/ok, the approval decision, then `provision_access` allow/ok.

If E1004 tries to approve their own request, it is refused (`cannot_approve_own_request`), and the refusal is audited too.

### Using the CLI

```bash
uv run aegisdesk agent --as E1004                                    # interactive session
uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"   # one request; prints a thread ID
uv run aegisdesk agent --as E1004 --thread <id> "Show me ticket INC-1001"   # continue a conversation
uv run aegisdesk thread <id> --as E1004                              # read a stored conversation
uv run aegisdesk ask --as E1004 "How do I configure VPN on macOS?"   # direct RAG answer with sources
uv run aegisdesk approvals list --as E1010                           # needs DATA_STORE=postgres (Option B)
uv run aegisdesk approvals approve AP-0001 --as E1010 --comment "Month-end close"
```

<details>
<summary><b>All CLI commands</b></summary>

| Command | What it does | Example |
|---|---|---|
| `config` | Effective configuration, secrets masked | `aegisdesk config` |
| `agent` | Talk to the agents; `--engine multi` (default), `graph` (single agent) or `loop` (hand-written loop); `--tools local\|mcp_inprocess\|mcp_http`; `--trace` prints the span tree | `aegisdesk agent --as E1004 --trace "What laptop is assigned to me?"` |
| `thread` | Show a stored conversation | `aegisdesk thread <id> --as E1004` |
| `ask` | Answer from the documents, with citations | `aegisdesk ask --as E1004 "How do I reset my password?"` |
| `rag ingest` / `rag search` | Index the documents; inspect retrieved chunks and scores | `aegisdesk rag search "vpn keeps disconnecting" --as E1004` |
| `approvals list\|show\|approve\|reject` | Work on approvals assigned to you | `aegisdesk approvals reject AP-0001 --as E1010 --comment "Not needed"` |
| `approvals reconcile` | Finish approved requests that were never provisioned (IT admin) | `aegisdesk approvals reconcile --as E1006` |
| `audit` | List audit events (`AUDIT_STORE=postgres`) | `aegisdesk audit --user E1004` |
| `policy check` | Ask the policy engine for one decision, with reasons | `aegisdesk policy check --as E1004 --agent knowledge --tool create_ticket` |
| `mcp tools` / `mcp serve` | Discover the MCP tools; serve the read and action servers over HTTP | `MCP_TOKEN_SECRET=… aegisdesk mcp serve` |
| `api serve` | Serve the HTTP API (`--host`, `--port`) | `aegisdesk api serve --port 8000` |
| `db init` / `db seed` | Migrations, checkpoint tables and seed rows | `aegisdesk db init` |
| `eval golden` / `eval rag` | Run evaluation suites | `aegisdesk eval golden --config multi --compare single` |
| `telemetry` | Where traces, metrics and logs go | `aegisdesk telemetry` |
| `chat`, `triage`, `repeat` | Model-layer basics: plain chat, structured output, (non-)determinism | `aegisdesk triage "My VPN drops every 10 minutes"` |

</details>

### Using the HTTP API

Every `/api/v1` route needs `X-Employee-Id`, set by an authenticating gateway in a real deployment. The OpenAPI document is at `/openapi.json`, the interactive docs at `/docs`.

```bash
H='-H X-Employee-Id:E1004 -H Content-Type:application/json'
T=$(curl -s -X POST $H localhost:8000/api/v1/threads | jq -r .thread_id)

# one turn, JSON response
curl -s -X POST $H localhost:8000/api/v1/threads/$T/messages -d '{"text":"How do I configure VPN on macOS?"}'

# the same, streamed as server-sent events: activity events, then one result event
curl -sN -X POST $H -H 'Accept: text/event-stream' localhost:8000/api/v1/threads/$T/messages \
     -d '{"text":"Please create an access request for FinanceERP for month-end reporting"}'

# the manager approves; the employee's thread resumes
curl -s -H X-Employee-Id:E1010 localhost:8000/api/v1/approvals
curl -s -X POST -H X-Employee-Id:E1010 -H Content-Type:application/json \
     localhost:8000/api/v1/approvals/AP-0001/approve -d '{"comment":"Month-end close"}'

curl -s -H X-Employee-Id:E1004 localhost:8000/api/v1/audit     # your audit events
```

Send `Idempotency-Key: <your key>` with a message to make client retries safe: a repeat returns the stored response instead of running again. Errors are RFC 9457 `application/problem+json` with a `category`, `request_id` and `trace_id`. Full reference: [docs/API.md](docs/API.md).

---

## Operating AegisDesk

| Task | How |
|---|---|
| Health and readiness | `curl localhost:8000/health` (liveness); `curl localhost:8000/ready` (database, checkpointer, circuit breakers; `503` when not ready) |
| Metrics | `curl localhost:8000/metrics` (Prometheus): requests, tokens, tool calls, policy denials, approvals, latency, model errors, circuit transitions |
| Follow one request | Every message response and error carries a `trace_id`; open it in Grafana Explore (Tempo), or run any CLI request with `--trace` for a span tree without infrastructure |
| Logs | `LOG_FORMAT=json`: every line has `trace_id`, `request_id` and `thread_id` |
| Evaluate | `uv run aegisdesk eval golden --config multi` (60 cases); `--dataset evals/adversarial/security_v1.yaml` (attacks); `--dataset evals/reliability/faults_v1.yaml` (13 failure cases); `uv run aegisdesk eval rag` |
| Rehearse failures | `AEGIS_FAULTS=model_timeout:router`, `db_error:access`, `mcp_unavailable:action`, `tool_error:<tool>`, … (never in production) |
| Finish stuck access requests | `uv run aegisdesk approvals reconcile --as E1006` (idempotent, safe to repeat) |

When something fails, AegisDesk says so instead of guessing: a model outage gives a fixed “temporarily unavailable” answer and a `503` with `Retry-After`; a database outage gives `503 <store>_unavailable` and nothing half-done; a tool or MCP outage is reported in the answer. Symptoms and fixes: [docs/RUNBOOK.md](docs/RUNBOOK.md).

---

## Security model

What is enforced in code, whatever the model writes:

* **Identity** comes from the authenticated session; tools have no employee-ID parameter, and schemas reject extra fields.
* **Least privilege:** each agent sees only its allowlisted tools; the policy engine checks the agent’s grants, the tool’s risk level, the user’s roles, the environment and forbidden actions on every call, and fails closed.
* **Human approval** for high-risk actions, with approval evidence the gateway looks up itself, separation of duties and expiry.
* **Audit before action:** no decision record, no write. The audit table rejects updates and deletes.
* **Bounded agents:** step, tool-call, handoff and per-request write limits.
* **MCP delegation tokens:** short-lived, audience-bound, carry the user, the acting agent and the request ID.
* **Redaction:** telemetry keeps only allowlisted attributes and pseudonymous user IDs.

An adversarial suite (prompt injection, impersonation, cross-user access, approval bypass) runs in CI on every change; a single unauthorized action fails the build.

**Not production-ready as is:** the API trusts the `X-Employee-Id` header and must only be reachable through an authenticating gateway; delegation tokens use a shared HS256 secret; secrets come from `.env`; data is synthetic. The [Productionisation and GenX Platform Design](docs/AegisDesk-Productionisation-and-GenX-Platform-Design.docx) describes the path to production.

---

## Project structure

```
apps/ui/                 Streamlit web UI (talks to the API only)
config/                  models.yaml (allowlist), policy.yaml (governance), pricing.yaml
data/seed/               synthetic employees, assets, tickets, applications, access
data/documents/          the knowledge base (12 policy and how-to documents)
prompts/                 versioned prompt templates per agent
src/aegisdesk/
  api/                   FastAPI app, auth dependency, schemas
  runtime.py             AegisRuntime: the facade shared by the API, UI and CLI
  graphs/                LangGraph graphs: supervisor and tool-agent subgraph
  agents/                agent definitions (router, specialists, tool sets), citation check, M1 loop
  tools/                 typed tools, executor, MCP client, provisioning
  mcp_servers/           read and action MCP servers
  governance/            policy engine and action gateway
  approvals/             approval service, workflow, evidence
  audit/                 audit events, PostgreSQL store
  identity/              user and agent identity, delegation tokens
  domain/                access domain and stores
  rag/                   chunking, embeddings, vector stores, retrieval
  llm/                   model factory, allowlist, fake model, usage
  reliability/           model guard, circuit breakers, error classification
  persistence/           checkpointer and idempotency stores
  observability/         tracing, metrics, logging, redaction
  evals/                 evaluation runner, checks, judge, reports
  cli.py                 the aegisdesk command
evals/                   golden, adversarial, reliability and RAG datasets; CI baselines
migrations/              Alembic migrations (knowledge base, audit, access workflow, idempotency)
infra/observability/     collector, Tempo, Prometheus and Grafana configuration
tests/                   unit, API, integration and live tests
docs/                    design documents, ADRs, milestone notes, API and runbook
```

---

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests apps                                   # strict
uv run pytest -m "not live"                                  # offline and deterministic
AEGIS_TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest -m "not live"   # + PostgreSQL contract tests
uv run pytest -m live                                        # real providers; skipped without keys
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint, format and strict type checks, validates the compose file, sets up PostgreSQL with pgvector and runs the tests. Then come the evaluation gates, on both the in-process and the MCP configuration: a RAG hit rate of at least 0.85, and the golden, adversarial and reliability suites with safety gates (zero unauthorized actions, approvals enforced, every request traced, no secrets in outputs) and regression gates against committed baselines.

The Word design documents and their diagrams are generated from [`docs/design/`](docs/design/README.md) (`npm run all`).

---

## Documentation

| Document | Content |
|---|---|
| [High-Level Design (Word)](docs/AegisDesk-High-Level-Design.docx) | The system as built: context, architecture, data and process flows, governance, observability, evaluation, reliability, deployment, data model, decisions and patterns (20 diagrams) |
| [Productionisation and GenX Platform Design (Word)](docs/AegisDesk-Productionisation-and-GenX-Platform-Design.docx) | The next stage, as a design and tutorial: multi-cloud with DR and one CI/CD pipeline, the enterprise integrations, and the GenX platform (36 diagrams) |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture overview and progress |
| [AGENT_DESIGN.md](docs/AGENT_DESIGN.md) · [MCP_DESIGN.md](docs/MCP_DESIGN.md) · [RAG_DESIGN.md](docs/RAG_DESIGN.md) | Agents and handoffs · MCP servers and tokens · knowledge base and retrieval |
| [GOVERNANCE_DESIGN.md](docs/GOVERNANCE_DESIGN.md) · [APPROVALS_DESIGN.md](docs/APPROVALS_DESIGN.md) | Policy, gateway and audit · human approval |
| [OBSERVABILITY.md](docs/OBSERVABILITY.md) · [EVALUATION.md](docs/EVALUATION.md) | Telemetry · datasets, checks, metrics and gates |
| [API.md](docs/API.md) · [RUNBOOK.md](docs/RUNBOOK.md) | HTTP API reference · operating and troubleshooting |
| [docs/adr/](docs/adr/) | Architecture decision records 0001–0017 |
| [docs/milestones/](docs/milestones/) | Learning notes for each milestone, with real results |

<details>
<summary><b>How it was built: milestones M0–M11</b></summary>

AegisDesk was built milestone by milestone as a learning platform; each milestone has notes with concepts, results and exercises.

| Milestone | What it added |
|---|---|
| [M0 LLM fundamentals](docs/milestones/M0-llm-fundamentals.md) | Provider-agnostic model layer, structured output, versioned prompts, token and latency measurement |
| [M1 Single agent + tools](docs/milestones/M1-single-agent-tools.md) | A hand-written tool-calling loop, trusted identity, idempotent writes, security tests |
| [M2 LangGraph](docs/milestones/M2-langgraph.md) | The agent as an explicit graph with checkpointing and thread ownership |
| [M3 RAG](docs/milestones/M3-rag.md) | Knowledge base, embeddings, pgvector, access filtering, verified citations, retrieval gate |
| [M4 Multi-agent](docs/milestones/M4-multi-agent.md) | Router, deterministic supervisor, Knowledge, Service Desk and Access specialists, handoffs |
| [M5 MCP](docs/milestones/M5-mcp.md) | Read and action MCP servers, signed delegation tokens, discovery, timeouts |
| [M6 Governance](docs/milestones/M6-governance.md) | Policy engine, action gateway, append-only audit |
| [M7 Human approval](docs/milestones/M7-approvals.md) | Durable interrupt and resume, approver rules, provisioning with evidence |
| [M8 Observability](docs/milestones/M8-observability.md) | OpenTelemetry traces, metrics, JSON logs, redaction, Grafana stack |
| [M9 Evaluations](docs/milestones/M9-evaluations.md) | Golden and adversarial suites, safety and regression gates, write budget |
| [M10 API + UI](docs/milestones/M10-api-ui.md) | FastAPI with SSE, Streamlit views, Dockerfile and docker compose |
| [M11 Reliability](docs/milestones/M11-reliability.md) | Fault injection, model guard, circuit breakers, idempotent replay, reconciliation |

</details>

---

## Limitations and what's next

* **M12, CI/CD:** a release pipeline on top of today’s CI gates.
* **Production:** a real identity provider (OIDC), asymmetric tokens, secret management, TLS, backups and multi-region operation are designed in the [Productionisation and GenX Platform Design](docs/AegisDesk-Productionisation-and-GenX-Platform-Design.docx), not built.
* **Quality numbers:** the evaluation results in the milestone notes were measured on the offline fake model; baselines on real models are not recorded yet.
* Circuit breakers are per process, and `Retry-After` for a model outage is a fixed 10 s.

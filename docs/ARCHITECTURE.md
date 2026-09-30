# AegisDesk architecture

This document describes the **target** architecture and marks what has been built so far. It is updated at every milestone.

| Milestone | Status |
|---|---|
| M0 LLM fundamentals | ✅ built ([notes](milestones/M0-llm-fundamentals.md)) |
| M1 Single agent + local tools | ✅ built ([notes](milestones/M1-single-agent-tools.md)) |
| M2 LangGraph | ✅ built ([notes](milestones/M2-langgraph.md)) |
| M3 RAG | not started |
| M4 Multi-agent | not started |
| M5 MCP | not started |
| M6 Governance | not started |
| M7 Human approval | not started |
| M8 Observability | not started |
| M9 Evaluations | not started |
| M10 API + UI | not started |
| M11 Reliability | not started |
| M12 CI/CD | partial: lint, type and unit-test gates run in CI from M0 |

## Guiding principle

LLMs do probabilistic reasoning. Deterministic software does authentication, authorization, policy, approval enforcement, validation, business rules, persistence, audit and retries. **The LLM is never the security authority**: it proposes actions, and deterministic code decides whether they happen.

```
AI reasoning   ──proposes──►   Business workflow   ──guarded by──►   Governance / policy enforcement
 (agents)                       (graph, tools)                        (identity, OPA, approvals, audit)
```

## Logical architecture (target)

```
 Employee / Manager
        │
        ▼
 ┌──────────────┐     ┌────────────────────────────────────────────────────────┐
 │ Streamlit UI │────►│ FastAPI  (threads, messages, approvals, health, metrics)│
 └──────────────┘     └──────────────────────────┬─────────────────────────────┘
                                                 │ trusted identity claims
                                                 ▼
                      ┌──────────────────────────────────────────────┐
                      │ LangGraph main graph (checkpointed state)     │
                      │  supervisor ─► knowledge | service_desk | access│
                      └───────┬───────────────────────┬──────────────┘
                              │ tool requests          │ retrieval
                              ▼                        ▼
                     ┌─────────────────┐       ┌──────────────┐
                     │  Tool Gateway   │       │ RAG retriever │──► pgvector
                     │  + Policy (OPA) │       └──────────────┘
                     └───┬───────┬─────┘
                ALLOW    │  DENY │  APPROVAL ──► interrupt ──► manager ──► resume
                         ▼
          ┌──────────────────────────────┐
          │ MCP servers: read  | action  │──► PostgreSQL (employees, assets, tickets, access…)
          └──────────────────────────────┘

 Cross-cutting: model layer (M0) · audit events · OpenTelemetry + LangSmith · evals
```

## What exists after M2

```
aegisdesk agent --as E1004 --thread T "..."
      │
      ├─► authenticate() ─────────────► UserContext (trusted, built before the model runs)
      │                                       │
      ├─► thread ownership check (before anything is written)
      ▼                                       ▼
ServiceDeskGraphAgent (LangGraph) ──────► ToolExecutor ──► Service Desk tools ──► in-memory repository
      │   start_turn → call_model ⇄ run_tools     lookup · validate · trusted context ·     (data/seed/*.json)
      │   → limit_reached / END                   idempotency key · error shaping
      │   state checkpointed after every node ──► SqliteSaver (.aegisdesk/checkpoints.sqlite)
      ▼
BaseChatModel.bind_tools(...) ◄── build_chat_model(Settings, allowlist)
                                    ├── ChatAnthropic   (hosted)
prompts/service_desk/v1.yaml        ├── ChatOllama      (local)
                                    └── ScriptedChatModel (offline fake, tests)

(ToolCallingAgent, the M1 hand-written loop, is kept as a reference: `--engine loop`.)
```

### LangGraph topology

```mermaid
graph TD;
	__start__([__start__]) --> start_turn;
	start_turn --> call_model;
	call_model -.->|tool calls| run_tools;
	call_model -.->|no tool calls| __end__([__end__]);
	run_tools -.->|under limits| call_model;
	run_tools -.->|limit hit| limit_reached;
	limit_reached --> __end__;
```

* **Graph** (`src/aegisdesk/graphs/`): typed `ServiceDeskState`; our own nodes, with no prebuilt `ToolNode`, so tools always run through `ToolExecutor`; routing in plain Python conditional edges; streaming per node. See [ADR 0004](adr/0004-why-langgraph.md).
* **Threads** (`src/aegisdesk/persistence/`): the SQLite checkpointer saves state after every node, so conversations survive restarts. Threads belong to the employee who started them; others are refused before anything is written.

From M1:

* **Agent loop** (`src/aegisdesk/agents/loop.py`, now the reference engine): the model proposes tool calls, and the application validates and executes them, feeds the results back, and stops at a final answer or a hard limit. Every run returns its trajectory, token usage and request ID.
* **Tools** (`src/aegisdesk/tools/`): Pydantic input and output schemas, plus risk, read/write, idempotency and owner metadata. Model-facing schemas contain **no identity parameters**, because the user comes from `UserContext` ([ADR 0003](adr/0003-tools-take-identity-from-trusted-context.md)).
* **Security boundary, first version:** tool allowlist per agent, strict argument validation, ownership checks, idempotent writes, error shaping and loop limits. It is enforced in code and tested with a scripted malicious model (`tests/security/`). M6 moves the per-tool checks into a central gateway with OPA.

From M0:

* **Model layer** (`src/aegisdesk/llm/`): the only code that knows about providers. Everything above it depends on `BaseChatModel` and `LLMClient`. See [ADR 0002](adr/0002-provider-agnostic-model-layer.md).
* **Model policy:** `config/models.yaml` allowlist, enforced at construction.
* **Prompts:** versioned YAML files. Each call records `prompt_name` and `prompt_version`.
* **Per-call metadata:** provider, model, prompt version, input and output tokens, latency. These are the seeds of later traces, metrics and evaluation records.

## Repository layout

The spec's layout (§36) is followed inside a single installable package, `src/aegisdesk/`. Subpackages are added in the milestone that needs them, rather than created empty up front:

| Spec directory | Location | Introduced |
|---|---|---|
| `prompts/` | `prompts/` (data), `src/aegisdesk/prompts/` (loader) | M0 |
| model abstraction | `src/aegisdesk/llm/` | M0 |
| `tools/` | `src/aegisdesk/tools/` | M1 |
| `identity/` | `src/aegisdesk/identity/` (simulated login; OIDC later) | M1 |
| `data/seed/` | `data/seed/` | M1 |
| `graphs/`, `agents/` | `src/aegisdesk/graphs/`, `src/aegisdesk/agents/` | M1–M2 (M4 adds more agents) |
| `rag/` | `src/aegisdesk/rag/` | M3 |
| `mcp_servers/` | `src/aegisdesk/mcp_servers/` | M5 |
| `governance/` | `src/aegisdesk/governance/` | M6 |
| `persistence/` | `src/aegisdesk/persistence/` (checkpointer) | M2 |
| `approvals/`, `migrations/` | … | M7 |
| `observability/`, `infrastructure/` | … | M8 |
| `evals/` | `evals/` | M9 |
| `apps/api`, `apps/ui` | … | M10 |

## Diagrams still to come

Written as each milestone lands: RAG pipeline (M3), MCP interactions (M5), the full security boundary (M6; its first version is described above), approval workflow (M7), observability architecture (M8), evaluation lifecycle (M9).

## Decisions

See [`docs/adr/`](adr/).

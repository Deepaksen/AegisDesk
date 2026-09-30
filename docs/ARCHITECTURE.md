# AegisDesk architecture

This document describes the **target** architecture and marks what has been built so far. It is updated at every milestone.

| Milestone | Status |
|---|---|
| M0 LLM fundamentals | ✅ built ([notes](milestones/M0-llm-fundamentals.md)) |
| M1 Single agent + local tools | not started |
| M2 LangGraph | not started |
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

## What exists after M0

```
aegisdesk CLI ──► LLMClient ──► BaseChatModel ◄── build_chat_model(Settings, allowlist)
                     │                              ├── ChatAnthropic   (hosted)
       prompts/<name>/vN.yaml                       ├── ChatOllama      (local)
                                                    └── ScriptedChatModel (offline fake, tests)
```

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
| `graphs/`, `agents/` | `src/aegisdesk/graphs/`, `src/aegisdesk/agents/` | M2 / M4 |
| `rag/` | `src/aegisdesk/rag/` | M3 |
| `mcp_servers/` | `src/aegisdesk/mcp_servers/` | M5 |
| `governance/`, `identity/` | `src/aegisdesk/governance/`, `src/aegisdesk/identity/` | M6 |
| `approvals/`, `persistence/`, `migrations/` | … | M7 |
| `observability/`, `infrastructure/` | … | M8 |
| `evals/` | `evals/` | M9 |
| `apps/api`, `apps/ui` | … | M10 |

## Diagrams still to come

Written as each milestone lands: LangGraph topology (M2), RAG pipeline (M3), MCP interactions (M5), security boundary (M6), approval workflow (M7), observability architecture (M8), evaluation lifecycle (M9).

## Decisions

See [`docs/adr/`](adr/).

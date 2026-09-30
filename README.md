# AegisDesk

An enterprise-style **agentic AI service desk** for the fictional Northstar Industries, built milestone by milestone as a learning platform. Employees will ask IT questions, check assets and tickets, and request application access. Sensitive actions go through deterministic policy and human approval, and never on the model's say-so.

All data is synthetic.

## Status

| Milestone | What it adds | Notes |
|---|---|---|
| M0 LLM fundamentals ✅ | Provider-agnostic model layer (Anthropic, local Ollama, offline fake), chat, structured output, versioned prompts, token and latency measurement | [M0](docs/milestones/M0-llm-fundamentals.md) |
| M1 Single agent + tools ✅ | Service Desk agent with a hand-written tool-calling loop; tools for own assets and tickets and for ticket creation; trusted identity; idempotent writes; security tests | [M1](docs/milestones/M1-single-agent-tools.md) |
| M2 LangGraph ✅ | The same agent as an explicit LangGraph graph: typed state, nodes, conditional edges, SQLite checkpointing (threads survive restarts), per-node streaming, thread ownership | [M2](docs/milestones/M2-langgraph.md) |

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

# Service Desk agent (simulated login as a synthetic employee)
uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"   # prints a thread_id
uv run aegisdesk agent --as E1004 --thread <id> "Show me ticket INC-1001"  # continue, even after restart
uv run aegisdesk thread <id> --as E1004      # read a stored conversation
uv run aegisdesk agent --as E1004            # interactive session
uv run aegisdesk agent --as E1004 --engine loop "..."   # the M1 hand-written loop
```

Synthetic users include `E1004` (finance), `E1001` (engineering), `E1005` (contractor), `E1006` (IT admin), `E1010` (manager) and `E1007` (terminated, so login is refused). See [`data/seed/`](data/seed/).

### Choosing a model

Set these in `.env` or your shell. Models must be listed in [`config/models.yaml`](config/models.yaml).

| Provider | Settings |
|---|---|
| Offline fake (default) | `MODEL_PROVIDER=fake` `MODEL_NAME=fake-scripted` |
| Anthropic | `MODEL_PROVIDER=anthropic` `MODEL_NAME=claude-haiku-4-5-20251001` (or `claude-sonnet-5-5`) `ANTHROPIC_API_KEY=…` |
| Ollama (local) | `ollama pull llama3.2`, then `MODEL_PROVIDER=ollama` `MODEL_NAME=llama3.2` (or `qwen2.5:7b`) |

Also available: `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`, `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES`, `OLLAMA_BASE_URL`, `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `CHECKPOINT_DB_PATH` (default `.aegisdesk/checkpoints.sqlite`).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not live"   # offline and deterministic (what CI runs)
uv run pytest -m live         # real providers; skips any without a key or a running Ollama
```

## Documentation

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): target architecture and current state
* [`docs/milestones/`](docs/milestones/): per-milestone learning notes
* [`docs/adr/`](docs/adr/): architecture decision records

# AegisDesk

An enterprise-style **agentic AI service desk** for the fictional Northstar Industries, built milestone by milestone as a learning platform. Employees will ask IT questions, check assets and tickets, and request application access. Sensitive actions go through deterministic policy and human approval, and never on the model's say-so.

All data is synthetic.

## Status

**Milestone 0, LLM fundamentals, is complete:** a provider-agnostic model layer (Anthropic, local Ollama, or an offline fake), plain chat, structured output, versioned prompts, and token and latency measurement.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the target architecture and progress, and [`docs/milestones/M0-llm-fundamentals.md`](docs/milestones/M0-llm-fundamentals.md) for what M0 teaches.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # optional; defaults use the offline fake model

uv run aegisdesk config
uv run aegisdesk chat "How do I clear my DNS cache?"
uv run aegisdesk triage "My VPN drops every 10 minutes" --show-messages
uv run aegisdesk repeat "Suggest a name for a new laptop" --runs 5 --temperature 1.0
```

### Choosing a model

Set these in `.env` or your shell. Models must be listed in [`config/models.yaml`](config/models.yaml).

| Provider | Settings |
|---|---|
| Offline fake (default) | `MODEL_PROVIDER=fake` `MODEL_NAME=fake-scripted` |
| Anthropic | `MODEL_PROVIDER=anthropic` `MODEL_NAME=claude-haiku-4-5-20251001` (or `claude-sonnet-5-5`) `ANTHROPIC_API_KEY=…` |
| Ollama (local) | `ollama pull llama3.2`, then `MODEL_PROVIDER=ollama` `MODEL_NAME=llama3.2` (or `qwen2.5:7b`) |

Also available: `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`, `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES`, `OLLAMA_BASE_URL`.

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

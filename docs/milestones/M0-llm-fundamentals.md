# Milestone 0: LLM fundamentals

**Status:** complete
**Builds:** a provider-agnostic model layer with plain chat, structured output, configuration and token-usage measurement.
**Does not build:** tools, agents, LangGraph, RAG, MCP, policy or persistence. Those arrive in M1 and later.

---

## 1. Concepts introduced

### Messages
A chat model does not take "a prompt". It takes an ordered **list of messages**, each with a role:

| Role | Who writes it | Purpose |
|---|---|---|
| `system` | The application | Standing instructions: who the assistant is, what it may and may not claim |
| `human` / `user` | The end user | The request |
| `ai` / `assistant` | The model | Previous answers, when replaying a conversation |
| `tool` | The application | Results of tool calls (M1) |

The model is **stateless**. It remembers nothing between calls. A "conversation" is the application re-sending the full history every time, which is why `LLMClient.build_messages` takes a `history` argument. Holding and persisting that history is application state, and in M2 it becomes LangGraph state.

Run `aegisdesk chat "..." --show-messages` to see the exact list that is sent.

### Tokens and the context window
Models read and write **tokens**: sub-word pieces, roughly ¾ of an English word each. Everything is measured in tokens:

* **Input tokens:** the system prompt, the history and the new message. You pay for these on *every* call, so a long system prompt costs something on every request.
* **Output tokens:** what the model generates, capped by `MODEL_MAX_TOKENS`.
* **Context window:** the maximum input plus output one call can hold. When history grows past it, something must be dropped or summarised. That is an application decision, not something the model does for you.

Each call's usage is reported in `AIMessage.usage_metadata`, which LangChain normalises across providers (Anthropic reports `input_tokens`, Ollama reports `prompt_eval_count`). `aegisdesk.llm.usage.TokenUsage` turns it into a value we can add up. Later milestones export it as metrics (`input_tokens_total`, `output_tokens_total`) and turn it into cost.

> The fake model counts whitespace-separated words, not real tokens. That is enough to exercise the accounting code, but don't compare its numbers with a real model's.

### Structured output
Free text is fine for people to read, but code cannot reliably branch on it. **Structured output** asks the model to answer in a fixed schema, and the application validates the result:

```
TicketTriage(category=vpn, urgency=medium, summary="...", needs_human=False)
```

How it works with Anthropic: the Pydantic schema is converted to JSON Schema and offered to the model as a **tool** it is *forced* to call (`tool_choice="any"`). The model's tool-call arguments are then validated by Pydantic. With Ollama, LangChain uses the server's native JSON-schema mode instead. Either way we get either a valid `TicketTriage` or an error.

The model can still produce output that is well-formed but invalid, such as an enum value that doesn't exist or a missing field. `LLMClient.structured` raises `StructuredOutputError` and keeps the raw response for debugging. It does **not** silently retry; whether to retry is a policy decision that M11 makes explicitly.

This is the first example of the core rule in the spec: **the model proposes, deterministic code validates.**

### Temperature and the limits of determinism
`MODEL_TEMPERATURE` controls how much randomness sampling uses. At `0` the model almost always picks the most likely token; higher values give more varied answers.

Temperature 0 is **not** a guarantee of identical output. Floating-point non-determinism on GPUs, batching and silent model updates can all change answers. Consequences for this project:

* Never rely on an LLM's exact wording for correctness or security.
* Tests that must be deterministic use the fake model (`MODEL_PROVIDER=fake`).
* Tests against real models (`tests/live`) assert **shape** (valid schema, non-empty text, tokens > 0), not wording.
* Later evaluations measure behaviour statistically over a dataset instead of trusting single runs.

Try `aegisdesk repeat "Suggest a name for a new laptop" --runs 5 --temperature 1.0` against a real model, then again with `--temperature 0`.

## 2. Model responsibilities vs application responsibilities

| The model does | The application does |
|---|---|
| Interpret natural language | Choose which model is allowed (`config/models.yaml`) |
| Produce text or schema-shaped arguments | Hold secrets (`SecretStr`, env only) |
| | Build the message list and choose the prompt version |
| | Validate the output (Pydantic) |
| | Measure tokens and latency |
| | Decide what to do on failure |

Nothing in the right-hand column is delegated to the model, and it stays that way in every later milestone.

## 3. Architecture for this milestone

```
                 .env / environment
                        │
                        ▼
                ┌───────────────┐      config/models.yaml
                │   Settings    │──────────────┐
                └───────┬───────┘              ▼
                        │              ┌───────────────┐
                        └─────────────►│ build_chat_   │── not allowlisted → ModelNotAllowedError
                                       │ model()       │── no API key      → ModelConfigurationError
                                       └───────┬───────┘
                        BaseChatModel ◄────────┘   (ChatAnthropic | ChatOllama | ScriptedChatModel)
                              │
 prompts/<name>/vN.yaml       ▼
        │             ┌───────────────┐
        └────────────►│   LLMClient   │── chat()       → ChatResponse(text, CallMetadata)
                      │               │── structured() → StructuredResponse[T] | StructuredOutputError
                      └───────────────┘
                              ▲
                              │
                         aegisdesk CLI
```

`CallMetadata` records `provider`, `model`, `prompt_name`, `prompt_version`, token usage and latency: the fields the spec later requires in traces and evaluation records (§22, §23).

### Files

| File | Role |
|---|---|
| `src/aegisdesk/config.py` | Typed settings from the environment; `SecretStr` API key |
| `config/models.yaml` | Model allowlist, an early form of the spec's "model policy" (§14) |
| `src/aegisdesk/llm/allowlist.py` | Loads and enforces the allowlist |
| `src/aegisdesk/llm/factory.py` | The only place that knows provider classes |
| `src/aegisdesk/llm/fake.py` | Deterministic offline model for tests and demos |
| `src/aegisdesk/llm/usage.py` | `TokenUsage` value type |
| `src/aegisdesk/llm/client.py` | `LLMClient`: messages, timing, usage, structured-output errors |
| `src/aegisdesk/prompts/loader.py` | Versioned prompt loading |
| `prompts/assistant/v1.yaml`, `prompts/triage/v1.yaml` | The prompts themselves |
| `src/aegisdesk/schemas/triage.py` | `TicketTriage` schema |
| `src/aegisdesk/cli.py` | `aegisdesk config / chat / triage / repeat` |
| `tests/unit/*` | Offline tests (run in CI) |
| `tests/live/*` | Real-provider tests (run by hand) |

## 4. Libraries introduced

**langchain-core, langchain-anthropic, langchain-ollama**
* *Problem solved:* each provider has its own SDK, message format, tool-calling format and usage fields.
* *Abstraction:* `BaseChatModel` with `invoke`, `bind_tools`, `with_structured_output`, standard message classes and `usage_metadata`.
* *Without it:* we would write and maintain two HTTP clients plus a normalisation layer for messages, tool calls and usage.
* *Alternatives:* the raw `anthropic` / `ollama` SDKs behind our own interface; LiteLLM (an OpenAI-shaped proxy over many providers).
* *Why here:* LangGraph (M2) works with `BaseChatModel` directly, and we only use the thin core interface, not LangChain's higher-level chains or agents.

**pydantic / pydantic-settings**
* Typed, validated configuration and schemas. `SecretStr` keeps keys out of `repr`, logs and JSON dumps. The alternative is hand-parsing `os.environ` and hand-written validation.

**PyYAML**: human-reviewable data files for prompts and the allowlist.

**uv, ruff, mypy (strict), pytest**: package and lockfile management, lint and format, static types, tests. Alternatives: Poetry/pip-tools, flake8+black, Pyright.

## 5. Execution path: `aegisdesk triage "My VPN drops every 10 minutes"`

1. `cli.main` parses the arguments and calls `get_settings()`, which reads the environment and validates it (a bad value fails here, before any model call).
2. `load_prompt(prompts_dir, "triage", "v1")` reads `prompts/triage/v1.yaml` and checks that its declared name and version match.
3. `build_chat_model(settings)` checks the allowlist, then constructs the provider's chat model. No network call happens yet.
4. `LLMClient.structured(prompt, text, TicketTriage)`:
   1. builds `[SystemMessage(triage v1), HumanMessage(text)]`;
   2. calls `model.with_structured_output(TicketTriage, include_raw=True)`, which binds the schema as a forced tool (or JSON-schema mode on Ollama);
   3. invokes the model once and times the call;
   4. receives `{raw, parsed, parsing_error}`. A parse error or a missing structure raises `StructuredOutputError(raw=...)`;
   5. reads token usage from `raw.usage_metadata`.
5. The CLI prints the validated JSON and one metadata line:
   `[provider/model prompt=triage@v1 tokens in=… out=… total=… latency=…ms]`.

## 6. How to run it

```bash
uv sync
uv run aegisdesk config
uv run aegisdesk triage "My VPN drops every 10 minutes" --show-messages   # offline fake model

# Anthropic
export MODEL_PROVIDER=anthropic MODEL_NAME=claude-haiku-4-5-20251001 ANTHROPIC_API_KEY=...
uv run aegisdesk chat "How do I clear my DNS cache on macOS?"
uv run aegisdesk triage "I can't log in to FinanceERP since my password reset"
uv run aegisdesk repeat "Suggest a name for a new laptop" --runs 5 --temperature 1.0

# Local Ollama
ollama pull llama3.2
export MODEL_PROVIDER=ollama MODEL_NAME=llama3.2
uv run aegisdesk triage "My laptop screen is flickering"

# Tests
uv run pytest -m "not live"   # offline, deterministic (what CI runs)
uv run pytest -m live         # real providers; skips any that aren't configured
```

## 7. Test results at the milestone boundary

Run in the development container:

* `uv run ruff check .`: all checks passed
* `uv run ruff format --check .`: all files formatted
* `uv run mypy src tests` (strict): no issues
* `uv run pytest`: 48 passed, 4 skipped. The 4 skipped are the live tests: no `ANTHROPIC_API_KEY` and no Ollama server were available in that container, so **real-provider calls were not executed there.** Run `uv run pytest -m live` locally to exercise them.

## 8. What M1 adds

A single Service Desk agent with local tools (`get_asset`, `get_ticket`, `create_ticket`) and a hand-written **agent loop**: the model proposes a tool call, the application validates and runs it, the result goes back as a `tool` message, and the loop repeats up to a fixed step limit. Structured output from this milestone is the building block: a tool call is structured output that the application chooses to execute.

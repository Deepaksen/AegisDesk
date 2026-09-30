# ADR 0002: Provider-agnostic model layer with an allowlist and an offline fake

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
The spec (§21) requires that application code is not coupled to one LLM provider, and that at least two model configurations can be compared without rewriting graph logic. We want Anthropic (hosted) and Ollama (local, no API key), and normal tests must run without a network or credentials (§37). The spec's model policy (§14) also requires an allowlist of permitted model configurations.

## Decision
1. **One interface:** all code depends on LangChain's `BaseChatModel`. Provider classes (`ChatAnthropic`, `ChatOllama`) are referenced only in `aegisdesk.llm.factory`.
2. **Configuration selects the model:** `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS` and related settings come from the environment and are validated by `pydantic-settings`.
3. **Allowlist enforced in code:** `config/models.yaml` lists the permitted `(provider, model)` pairs. `build_chat_model` refuses anything else before constructing a client.
4. **Offline fake:** `ScriptedChatModel` implements `BaseChatModel` (including `bind_tools`, so structured output works) with scripted or deterministic responses and synthetic usage. It is the default provider, so a fresh clone runs without secrets.
5. **Secrets:** API keys exist only in the environment as `SecretStr`. They never go into prompts, results or logs.
6. **A thin wrapper, `LLMClient`,** records provider, model, prompt name and version, token usage and latency for each call, and turns structured-output failures into a typed error. It does not retry or route.

## Consequences
* Switching Anthropic ↔ Ollama is an environment change. Tests prove that the factory builds each provider with the configured parameters.
* LangGraph (M2) can consume the same `BaseChatModel`.
* Adding a model is a reviewed change to `config/models.yaml`.
* We depend on LangChain's provider packages keeping up with provider APIs. We use only the thin `langchain-core` interface to limit exposure.
* Differences remain between providers: structured output uses forced tool calling on Anthropic and JSON-schema mode on Ollama, and small local models follow schemas less reliably. Evaluations (M9) have to measure this rather than assume it away.

## Alternatives considered
* **Raw provider SDKs behind our own interface:** full control, but we would reimplement message, tool-call and usage normalisation, and still need an adapter for LangGraph.
* **LiteLLM:** broad provider coverage through an OpenAI-shaped API. It adds another abstraction and a translation layer, and LangGraph integrates more directly with LangChain chat models.
* **Mocking HTTP in tests instead of a fake model:** brittle and provider-specific. A fake at the `BaseChatModel` boundary tests our code, not the provider's wire format.

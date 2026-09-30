# ADR 0008: Enterprise tools behind two MCP servers, with per-call delegation tokens

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Up to M4 every tool ran inside the agent process. The spec (§12, §13) asks for enterprise tools behind MCP servers, split into read and action, while keeping user identity, agent identity, request/correlation ID and authorization context across the boundary, using standards-compatible token concepts. A server receives JSON-RPC from a client; it must not trust tool arguments (written by the model) or the client's claims about who the user is.

## Decision
1. **Two servers, two audiences:** `aegisdesk-read` (LOW-risk reads) and `aegisdesk-action` (MEDIUM writes). A token's `aud` names one server; the other refuses it.
2. **Per-call delegation token in `_meta`:** the host signs a JWT per tool call with the user (`sub`, roles, department, manager), the acting agent (RFC 8693 `act` claim: id, version, type, environment), the request ID (`rid`), `jti`, and a 60-second expiry. The server derives user, agent and request ID only from the verified token.
3. **Servers reuse `ToolExecutor`:** schema validation, identity-free schemas, ownership checks, idempotency keys and error shaping are unchanged; MCP is a transport and a trust boundary around them.
4. **Host keeps the per-agent allowlist:** discovery (`tools/list`) says what a server offers; `agents/supervisor.py` still says what each agent gets, enforced before any call.
5. **One `ToolRunner` interface:** agents and graphs do not know where a tool runs. `TOOL_TRANSPORT` selects `local`, `mcp_inprocess` or `mcp_http`.
6. **Retry policy by annotation:** read-only tools are retried once on timeout/unavailable; writes never are.
7. **Knowledge and handoff tools stay local.**

## Consequences
* A compromised or buggy client cannot act as another user, cannot replay a read token for a write, and cannot name the user in arguments. Each is tested at the protocol level.
* Every call can be attributed to user + agent + version + request at the server, which is where M6 policy and M8 audit attach.
* Costs: ~1 ms per call in-process, ~4 ms over local HTTP; new failure modes (timeout, unavailable) that the model must be able to explain; a shared secret to manage.
* The spec's scenarios produce the same results over `local` and `mcp_inprocess` (parametrized e2e tests).
* With `mcp_http` in one process pair, the servers own the in-memory data; separate services need the shared database (later milestone).

## Alternatives considered
* **Identity as a tool argument (e.g. `employee_id`):** the model writes arguments. Rejected (ADR 0003).
* **Session-level bearer token only (MCP's OAuth for HTTP):** authenticates the client connection, not each end user and agent per call; one host serves many users over one session. Kept as a later, complementary layer.
* **Trusting a plain `user_id` field in `_meta`:** unsigned, so any client could claim to be anyone.
* **One MCP server for everything:** simpler, but read and write would share one audience and one blast radius.
* **Asymmetric keys now (RS256 + JWKS):** the right production choice; HS256 keeps local setup to one env var. Claims are unchanged when switching.

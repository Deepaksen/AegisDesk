# Milestone 5: Tools behind MCP servers

**Goal:** move the enterprise tools out of the agent process and behind two MCP servers (read and action), without losing anything the local tools guaranteed: the user's identity, the agent's identity, the request ID (idempotency, audit) and the authorization context.

**What you can run now:**

```bash
uv run aegisdesk mcp tools                                   # MCP discovery (tools/list) on both servers
uv run aegisdesk agent --as E1004 --tools mcp_inprocess \
  "Please create an access request for FinanceERP for month-end reporting"

# Over HTTP, as separate processes:
export MCP_TOKEN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
uv run aegisdesk mcp serve                                   # terminal 1: :8765/read/mcp, /action/mcp
uv run aegisdesk mcp tools --remote                          # terminal 2
TOOL_TRANSPORT=mcp_http uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"
```

With `mcp_http`, the servers own the data: a ticket created through them lives in the server process, not in the CLI's own in-memory copy. Moving the repository to PostgreSQL (so separate processes share it) comes with the API work.

Design reference: [`docs/MCP_DESIGN.md`](../MCP_DESIGN.md). Decision: [ADR 0008](../adr/0008-mcp-servers-with-delegation-tokens.md).

---

## 1. Concepts introduced

### Host, client, server
MCP separates three roles:

| Role | In AegisDesk | Owns |
|---|---|---|
| **Host** | our application (CLI now, API in M10) | the model, the user session, the signing key, which agent gets which tools |
| **Client** | `McpGateway`: one MCP `Client` session per server | a connection; nothing else |
| **Server** | `aegisdesk-read`, `aegisdesk-action` | the tools and the data; verifies every call |

The model never talks to an MCP server. It still proposes tool calls; the host decides which agent may make them and forwards them.

### Discovery and schemas
`tools/list` returns each tool's name, description and JSON Schema input, plus standard **annotations** (`readOnlyHint`, `idempotentHint`, `destructiveHint`, `openWorldHint`) and our own `_meta` (`aegisdesk/risk`, `aegisdesk/access`, `aegisdesk/owner`). The schemas come from the same Pydantic models as before (`additionalProperties: false`, no `employee_id`), so a test proves the model sees **exactly** the same tool definitions over MCP as locally (`test_remote_tools_look_identical_to_the_model`).

The host still decides the per-agent allowlist. Discovery tells the host what a server *offers*; it does not decide what an agent *gets*. An allowlisted tool the server does not offer fails at startup, not mid-conversation.

### Request and response
`tools/call` sends `{name, arguments, _meta}`. The server answers with `CallToolResult{content: [text], isError}`. Our results are the same JSON as before: the tool's output model, or `{"error": {"category", "message"}}`. The client passes known error shapes to the model and replaces anything else (e.g. a stack trace from a broken server) with a generic `remote_error`.

### Transports
| `TOOL_TRANSPORT` | What happens | Used for |
|---|---|---|
| `local` (default) | tools run in the agent process (M1–M4) | baseline, fastest |
| `mcp_inprocess` | full MCP protocol and token checks against in-process servers, no network | tests, demos |
| `mcp_http` | MCP Streamable HTTP to `aegisdesk mcp serve` | real deployment shape |

### Identity across the boundary: delegation tokens
The server cannot trust anything the client says, and arguments are written by the model. So each call carries a **short-lived signed JWT** in `_meta["aegisdesk/token"]`, minted by the host per call:

```json
{
  "iss": "aegisdesk-host", "aud": "aegisdesk-mcp-action",
  "sub": "E1004", "roles": ["employee"], "department": "finance", "manager_id": "E1010",
  "act": {"sub": "access", "ver": "0.1.0", "typ": "specialist", "env": "development"},
  "rid": "1f581b32-…", "jti": "…", "iat": 1790000000, "exp": 1790000060
}
```

* `sub` + user claims: **who** (from the authenticated session, never from arguments).
* `act`: **which agent acts for them** (RFC 8693 actor claim, the OAuth token-exchange convention for delegation).
* `rid`: **which request**, so the server derives the same idempotency key as before.
* `aud`: **which server**. A read-server token is refused by the action server.
* `exp` 60 s, `jti` unique: a leaked token is useful briefly and only for one server.

The server verifies signature, issuer, audience, expiry and required claims, then runs the tool with the token's user and request ID. HS256 with a shared secret keeps local setup simple; see "Production" below.

### Authorization boundary
Three layers now check every enterprise tool call:

1. **Host (client side):** per-agent allowlist, before any network I/O. The Knowledge agent has no path to `create_ticket` at all.
2. **Server:** token must be valid and for this server; the tool must be on this server (the read server does not offer writes).
3. **Tool:** strict schema, identity from context, ownership checks, idempotency (unchanged from M1).

M6 adds the policy engine (OPA) at the server boundary, where it can read user, agent, tool and risk from the verified token.

### Timeouts and retries
Every call runs under `MCP_TIMEOUT_SECONDS`. Transport failures become structured errors the model can explain: `timeout`, `unavailable`, `protocol_error`.

* **Read tools** (`readOnlyHint`) are retried once.
* **Write tools are never retried by the client.** After a timeout the write may or may not have happened. (The server would deduplicate by idempotency key, but blind retries of actions are exactly what the spec forbids.) The model is told the call timed out.

---

## 2. What was built

| File | Purpose |
|---|---|
| `src/aegisdesk/identity/agent.py` | `AgentIdentity(agent_id, agent_version, agent_type, environment)` |
| `src/aegisdesk/identity/tokens.py` | `TokenIssuer` / `TokenVerifier`: delegation JWTs, `CallerContext` |
| `src/aegisdesk/mcp_servers/server.py` | `build_tool_server`: MCP `Server` over a `ToolExecutor`, token check in `tools/call` |
| `src/aegisdesk/mcp_servers/catalogue.py` | read/action split, audiences, `build_servers` |
| `src/aegisdesk/mcp_servers/http.py` | both servers in one Starlette app (`/read/mcp`, `/action/mcp`, `/healthz`) |
| `src/aegisdesk/tools/executor.py` | `ToolRunner` protocol; `CompositeToolRunner` |
| `src/aegisdesk/tools/remote.py` | `McpGateway` (sessions on a background event loop), `RemoteToolRunner` |
| `src/aegisdesk/tools/transport.py` | `ToolFactory`: each agent's runner for the configured transport |
| `src/aegisdesk/tools/service_desk.py` | new write tool `add_ticket_comment` (MEDIUM, idempotent, own tickets only) |
| `src/aegisdesk/cli.py` | `mcp serve`, `mcp tools [--remote]`, `agent --tools` |

Which tools moved:

| Server | Tools |
|---|---|
| `aegisdesk-read` (LOW, retried once) | `get_employee_profile`, `get_my_assets`, `list_my_access`, `get_application`, `check_access_eligibility`, `get_ticket`, `list_my_tickets` |
| `aegisdesk-action` (MEDIUM, never retried) | `create_ticket`, `add_ticket_comment`, `create_access_request` |
| stay local | `search_knowledge_base`, `retrieve_document` (the agents' own retrieval), `request_handoff` (a control signal for the supervisor, not an integration) |

Agents and graphs did not change: they depend on the `ToolRunner` interface, and `ToolFactory` decides what is behind it.

---

## 3. Local vs remote tools

Measured once in the development container: 200 calls of `get_my_assets` per transport after one warm-up call (an ad-hoc script, not part of the test suite; numbers vary by machine):

| Transport | p50 | p95 |
|---|---|---|
| local | 0.017 ms | 0.027 ms |
| mcp_inprocess | 1.105 ms | 1.409 ms |
| mcp_http (localhost) | 3.857 ms | 4.360 ms |

| | Local tools | MCP tools |
|---|---|---|
| Latency | microseconds | milliseconds (JSON-RPC, token sign/verify, HTTP). Still tiny next to a model call (hundreds of ms) |
| Failure modes | exceptions in-process | timeouts, unavailable server, protocol errors: all need handling |
| Trust | caller and tool share a process | the server trusts nothing but the token |
| Ownership | the agent team owns the tool code | the system's owning team runs the server; agents just use it |
| Reuse | one application | any MCP host (another agent, an IDE) can use the same servers |
| Deployment | one process | separate processes, scaled and secured separately |
| Discovery | code import | `tools/list` at runtime |

**When to keep a tool local:** it is part of the agent's own capability (retrieval over a store the agent already uses), it is a control-flow signal (handoff), or the latency matters and nothing else needs it. **When to put it behind MCP:** it touches a system of record owned by another team, it should be reusable across hosts, or it needs its own security boundary.

---

## 4. Tests (real results)

Full suite, with the pgvector integration tests enabled (`AEGIS_TEST_DATABASE_URL`):

```
301 passed, 21 skipped in 11.08s
```

The 21 skipped are the `live` provider tests (no `ANTHROPIC_API_KEY`, no Ollama in this environment). `ruff check`, `ruff format --check` and `mypy` (105 files) are clean. The retrieval gate still passes (hit rate@k 0.89, 0 access violations).

New in this milestone:

| File | What it proves |
|---|---|
| `tests/unit/test_tokens.py` (11) | round trip; standard and `act` claims; missing, forged, tampered, expired, unsigned (`alg: none`) and actor-less tokens refused; wrong audience refused; short secret rejected |
| `tests/security/test_mcp_boundaries.py` (9) | discovery splits by risk, no identity in any schema; no token / forged token refused; read token replayed against the action server refused and nothing written; read server has no write tools; `employee_id` argument rejected; cannot comment on another employee's ticket; same request → same ticket, new request → new ticket |
| `tests/unit/test_remote_tools.py` (12) | client-side allowlist refuses before any call; missing tool fails at startup; fresh token per call with correct audience, user, request and agent; read retried once, then gives up; **write never retried**; unknown error text not passed to the model; remote tool definitions identical to local; local tools stay local; `mcp_http` needs a secret |
| `tests/integration/test_mcp_http.py` (4) | real uvicorn server on a free port: health; supervisor creates an access request over HTTP; host with the wrong secret is refused; unreachable server → `unavailable` |
| `tests/e2e/test_spec_scenarios.py` | every spec scenario now runs over both `local` and `mcp_inprocess` (12 runs), same assertions |
| `tests/unit/test_service_desk_tools.py` | `add_ticket_comment`: idempotent on own ticket, refused on others' |

---

## 5. Trace of one request over MCP

```
$ aegisdesk agent --as E1004 --tools mcp_inprocess "Please create an access request for FinanceERP for month-end reporting"
Tools: mcp_inprocess
→ router  in=161 out=16 7ms -> access: '...'
  · [access] step 1 model  in=184 out=22 1ms -> create_access_request
  · [access] step 1 tool   create_access_request({...}) ok 4.0ms
  · [access] step 2 model  in=197 out=17 0ms -> final answer
← access done
Assistant: [fake model] Tool results: {"request_id":"AR-1013","application":"FinanceERP","status":"awaiting_approval","approvals_required":["manager"],...}
```

What happened at the tool step:

1. The Access agent's runner is a `CompositeToolRunner`: `request_handoff` local, read tools on `aegisdesk-read`, `create_access_request` on `aegisdesk-action`.
2. `create_access_request` is on the agent's allowlist, so the host mints a token: `sub=E1004`, `act.sub=access`, `rid=<request id>`, `aud=aegisdesk-mcp-action`, valid 60 s.
3. `tools/call` goes to the action server with the token in `_meta`. The server verifies it, then runs the same `ToolExecutor` as before, with the token's user and request ID. Eligibility is recomputed; the request is recorded as `awaiting_approval`; the idempotency key is derived from (user, request, tool, arguments).
4. The server writes an INFO log record (logger `aegisdesk.mcp_servers.server`; the CLI does not print INFO logs by default). Captured with logging enabled, for the laptop question:
   `mcp aegisdesk-read: tool=get_my_assets user=E1004 agent=service_desk@0.1.0 env=development request_id=8cb446da-… status=ok`: who did what, on whose behalf, from which agent. M8 turns this into structured audit events and traces.
5. The JSON result returns through the client to the agent, which answers.

(The fake model fills arguments from the request text; real models write a proper justification.)

---

## 6. Production notes (not built)

* **Asymmetric keys and a real authorization server.** Replace HS256 with RS256/ES256 tokens from an OAuth authorization server (token exchange, RFC 8693: user token + agent credentials → narrowly scoped delegation token). Servers then hold only public keys (JWKS), and cannot mint tokens.
* **Standard MCP authorization.** MCP's HTTP transport defines OAuth 2.1 bearer tokens (`Authorization` header, protected-resource metadata). That authenticates the *client*. Our per-call token in `_meta` carries the *end user and agent* per call, which a session-level bearer cannot do when one host serves many users. Both can coexist.
* **mTLS or a private network** between host and servers.
* **Replay protection** by remembering `jti` values until expiry (not needed for idempotent tools, useful for audit).
* **Separate services** once the repository is in PostgreSQL.

## 7. What's next (M6)

A central policy decision (OPA) at the server boundary: "may *this agent* call *this tool* for *this user* with *these arguments*?", using the verified token's claims, with audit events for every allow and deny.

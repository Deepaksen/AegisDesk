# MCP design

How AegisDesk's enterprise tools are served over the Model Context Protocol (Milestone 5). Walkthrough and test results: [M5 notes](milestones/M5-mcp.md). Decision record: [ADR 0008](adr/0008-mcp-servers-with-delegation-tokens.md).

## Interactions

```
 Host (aegisdesk CLI / later API)                                  MCP servers
 ─────────────────────────────────                                 ─────────────────────────────
 authenticate() → UserContext
 supervisor → specialist (AgentIdentity: access@0.1.0, env)
   model proposes: create_access_request{application, justification}
        │
        ▼
 CompositeToolRunner ── local? ──► ToolExecutor (search_knowledge_base, retrieve_document, request_handoff)
        │ remote
        ▼
 RemoteToolRunner(action)
   1. on this agent's allowlist?  no → unknown_tool (no call made)
   2. TokenIssuer.issue(user, agent, request_id, aud=aegisdesk-mcp-action)   (60 s, fresh jti)
   3. McpGateway.call_tool ── tools/call {name, arguments, _meta:{aegisdesk/token}} ──►  aegisdesk-action
        timeout = MCP_TIMEOUT_SECONDS                                                     │
                                                                                          ▼
                                                                  TokenVerifier(aud=aegisdesk-mcp-action)
                                                                     sig · iss · aud · exp · required claims
                                                                     fail → isError {unauthenticated}
                                                                          │ CallerContext(user, agent, rid)
                                                                          ▼
                                                                  ToolExecutor.execute(name, args,
                                                                     user=token user, request_id=token rid)
                                                                     schema · ownership · idempotency key
                                                                          │
                                                                  log: tool, user, agent@ver, env, rid, status
   4. ◄──────────────────────── CallToolResult {content:[JSON], isError} ─────────────────┘
   5. map: ok → result · known error → as is · unknown error → remote_error
           TimeoutError → timeout · connection failure → unavailable
           read-only tool: one retry · write tool: no retry
```

## Server catalogue

| Server | Audience | Tools | Annotations |
|---|---|---|---|
| `aegisdesk-read` | `aegisdesk-mcp-read` | `get_employee_profile`, `get_my_assets`, `list_my_access`, `get_application`, `check_access_eligibility`, `get_ticket`, `list_my_tickets` | `readOnlyHint`, `idempotentHint`, `_meta.aegisdesk/risk=low` |
| `aegisdesk-action` | `aegisdesk-mcp-action` | `create_ticket`, `add_ticket_comment`, `create_access_request` | `idempotentHint` (by key), `_meta.aegisdesk/risk=medium` |

Over HTTP both run in one process (`aegisdesk mcp serve`): `/read/mcp`, `/action/mcp`, `/healthz`. Localhost binds get the SDK's DNS-rebinding protection.

## Token claims

| Claim | Meaning | Source |
|---|---|---|
| `iss` | `aegisdesk-host` | RFC 7519 |
| `aud` | target server | RFC 7519 |
| `sub` | employee ID | authenticated session |
| `roles`, `department`, `manager_id` | user claims | identity provider (simulated) |
| `act.sub`, `act.ver`, `act.typ`, `act.env` | acting agent | RFC 8693 actor claim |
| `rid` | request/correlation ID | host |
| `jti`, `iat`, `exp` | uniqueness, 60 s lifetime | RFC 7519 |

Verification accepts only HS256 (never `none`), requires all of `exp iat sub aud iss jti rid act`, and allows 5 s clock skew.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `TOOL_TRANSPORT` | `local` | `local` · `mcp_inprocess` · `mcp_http` (multi-agent engine) |
| `MCP_TOKEN_SECRET` | unset | ≥ 32 chars; required for `mcp_http` and `mcp serve`; throwaway key for `mcp_inprocess` |
| `MCP_READ_URL` | `http://127.0.0.1:8765/read/mcp` | |
| `MCP_ACTION_URL` | `http://127.0.0.1:8765/action/mcp` | |
| `MCP_TIMEOUT_SECONDS` | `10` | per call, per attempt |

## Failure handling

| Failure | Category the model sees | Client retry |
|---|---|---|
| server slow | `timeout` | reads: once; writes: never |
| server down / connection lost | `unavailable` (session dropped, reconnect next call) | reads: once; writes: never |
| JSON-RPC error | `protocol_error` | no |
| bad / missing / wrong-audience token | `unauthenticated` | no |
| policy refused on the server (M6) | `policy_denied` / `approval_required` | no |
| tool refused (schema, ownership, eligibility) | the tool's own category | no |
| unexpected error text from server | `remote_error` (text discarded) | no |

## Known limits

* HS256 shared secret: every holder can mint tokens. Production: asymmetric keys from an authorization server; servers hold public keys only.
* No `jti` replay cache.
* Discovery is read at agent construction; tools added to a server later need a restart of the host (MCP's `tools/list_changed` notification is not used).
* Servers share the host's in-memory repository in `mcp_inprocess`; with `mcp_http` the server process owns its own copy until the repository moves to PostgreSQL.

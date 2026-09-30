# Milestone 6: Governance (policy, action gateway, audit)

**Goal:** every tool call goes through deterministic governance before it can run:
- a policy decision that knows the agent, the user, the tool, its risk and the environment;
- an audit record that cannot be rewritten.

This must hold even if the model is fully manipulated and even if the host's own allowlist is broken.

**What you can run now:**

```bash
uv run aegisdesk policy check --as E1004 --agent knowledge --tool create_ticket     # DENY + reasons
uv run aegisdesk policy check --as E1004 --agent access --tool create_access_request # ALLOW
uv run aegisdesk policy check --as E1004 --agent service_desk --tool create_ticket \
  --environment production --agent-environment development                        # DENY, 2 reasons

# Persistent audit trail (needs Postgres, `alembic upgrade head`)
export DATABASE_URL=postgresql+psycopg://aegisdesk:aegisdesk@localhost:5432/aegisdesk AUDIT_STORE=postgres
uv run aegisdesk agent --as E1004 --tools mcp_inprocess \
  "My VPN keeps disconnecting. I already followed the troubleshooting guide. Create a ticket."
uv run aegisdesk audit --user E1004
```

Design reference: [`docs/GOVERNANCE_DESIGN.md`](../GOVERNANCE_DESIGN.md). Decisions:
- [ADR 0009](../adr/0009-deterministic-policy-engine.md): a policy engine in Python, with the rules as data.
- [ADR 0010](../adr/0010-append-only-audit-store.md): an append-only audit store.

---

## 1. Concepts introduced

### Governance is middleware, not an agent
The spec says: do **not** build a "governance agent". A model asked "is this allowed?" can be talked out of its answer. Governance here is ordinary code on the path between a tool request and the tool:

```
agent ─ tool request ─► lookup ─► schema validation ─► ACTION GATEWAY ─► handler
                                                        │  policy decision
                                                        │  audit "decision" event
                                          ┌─────────────┼──────────────┐
                                        DENY          ALLOW     REQUIRE_APPROVAL
                                    policy_denied   tool runs   approval_required
                                                        │        (human review, M7)
                                                  audit "outcome" event
```

### Identities
Every decision uses two identities:
- **User identity:** the authenticated employee (`UserContext`), from the session or, over MCP, from the signed token.
- **Agent identity:** `AgentIdentity(agent_id, version, type, environment)`. Each specialist, and the single agent (`service_desk_single`), has one.

The policy asks: *may this agent do this, on behalf of this user, here?*

### Policy as data
`config/policy.yaml` is the authority. It holds:
- the risk classification of each tool;
- which writes are authorized;
- the forbidden actions;
- each agent's allowed tools;
- per-environment restrictions;
- role requirements.

`PolicyEngine` (`governance/policy.py`) is ~100 lines of deterministic code that evaluates it. Changing who may do what means changing a reviewed data file, not agent code.

### Deny reasons, all of them
Like a Rego `deny` set, every rule runs and every failing rule adds a reason. One request can be wrong in several ways, and the audit record should say all of them:

| Reason | Rule |
|---|---|
| `forbidden_action` | the tool is in `forbidden_tools` (e.g. `direct_grant_production_admin`), for every agent |
| `unclassified_tool` | the policy does not classify the tool (default deny) |
| `unknown_agent` | no agent identity, or an agent the policy does not know |
| `agent_not_authorized_for_tool` | the tool is not in that agent's grant |
| `environment_mismatch` | the agent's claimed environment ≠ where the check runs |
| `not_approved_for_environment` | e.g. production allows reads only, for now |
| `write_not_authorized` | MEDIUM risk and not in `authorized_writes` |
| `missing_role` | the tool needs a role the user lacks (used by approvals, M7) |
| `policy_error` | the engine itself failed: fail closed |

Any reason → **DENY**. Otherwise **HIGH** risk → **REQUIRE_APPROVAL**. Otherwise **ALLOW**. This is the spec's §11 table: LOW is automatic, MEDIUM only if explicitly authorized, HIGH needs human approval.

### Defense in depth
The host allowlist from M4 (`specialist_tools`) is still there. A tool not on an agent's list is `unknown_tool` before anything else. The policy is a second, independent check, and the **MCP servers run it too**, from the verified token's agent. A test checks that the allowlists and the policy grants stay identical, so they cannot drift apart silently.

The risk in the policy is the one that counts. If a code change labels a HIGH tool as LOW, the gateway still applies HIGH (`test_high_risk_needs_approval_even_if_code_says_low`).

### Audit events
Each call writes two append-only events, linked by `call_id`:
- **decision:** written *before* the tool runs.
- **outcome:** written after (`ok` or the error category, plus latency).

Fields follow spec §35:
- when and where: timestamp, request_id, trace_id (M8), thread_id;
- who: user_id, agent_id, agent_version, environment;
- what: action, tool, risk, resource;
- the policy decision, its reasons and the policy version;
- approval_id and approver_id (M7);
- the outcome and latency.

Events hold only IDs, never free text: `resource` keeps only `ticket_id` / `application` / `document_id`. Descriptions and justifications are left out.

**Fail closed:** if the decision event cannot be written, a write tool does not run (`audit_unavailable`). A read proceeds and the failure is logged, because reads change nothing.

In PostgreSQL (`audit_events`, migration 0002), triggers reject `UPDATE`, `DELETE` and `TRUNCATE`. History cannot be rewritten by this code, by a bug, or by anyone with ordinary write access. Changing that takes a schema migration, which is reviewed.

---

## 2. What was built

| File | Purpose |
|---|---|
| `config/policy.yaml` | the policy: risk classes, grants, forbidden actions, environments, roles |
| `src/aegisdesk/governance/policy.py` | `PolicyEngine`, `PolicyInput`, `PolicyDecision`, deny reasons; strict schema validation at load |
| `src/aegisdesk/governance/gateway.py` | `ActionGateway`: decide, audit, fail closed |
| `src/aegisdesk/governance/factory.py` | build the gateway and audit store from settings |
| `src/aegisdesk/audit/events.py` | `AuditEvent`, `AuditLog` protocol, `InMemoryAuditLog` |
| `src/aegisdesk/audit/postgres.py` | `PgAuditLog` (insert/select only) |
| `migrations/versions/0002_audit_events.py` | table, indexes, append-only triggers |
| `src/aegisdesk/tools/executor.py` | the gateway step between validation and handler; `agent=` and `thread_id=` per call |
| `src/aegisdesk/mcp_servers/*` | servers build governed executors and pass the token's agent and thread |
| `src/aegisdesk/identity/tokens.py` | optional `tid` (thread) claim, so remote audit events carry the thread |
| `src/aegisdesk/tools/transport.py`, `agents/*.py` | every production path builds governed executors |
| `src/aegisdesk/cli.py` | `policy check`, `audit` |

Settings: `POLICY_PATH` (default `config/policy.yaml`) and `AUDIT_STORE` (`memory` | `postgres`). The environment is `AEGIS_ENV`. A missing or invalid policy file stops startup with a configuration error.

---

## 3. Bypass attempts (all fail)

`tests/security/test_governance_bypass.py`, plus the gateway and policy tests:

| Attempt | Result |
|---|---|
| Manipulated Knowledge agent, **with the host allowlist broken** so it holds every enterprise tool, calls `create_ticket`, `create_access_request`, `get_employee_profile` | all `policy_denied`, nothing written, three `deny` audit events naming `knowledge`. Tested over both `local` and `mcp_inprocess` |
| A client with a validly signed token for `act=knowledge` calls the action server directly (no host at all) | `policy_denied` from the server |
| A token claiming `environment=production` presented to a development server | `environment_mismatch` |
| A made-up agent (`admin_agent`) with a valid signature | `unknown_agent` |
| `direct_grant_production_admin` registered by mistake, labelled LOW in code, tried by every agent | `forbidden_action`; handler never called |
| A HIGH tool labelled LOW in code | `approval_required`; handler never called |
| Audit store down while creating a ticket | `audit_unavailable`; no ticket |
| Policy engine throws | `policy_error` → deny |
| Broken or missing policy file | startup refused |

What policy does **not** do here: row-level checks like "this ticket is yours". Those stay in the tools (ownership checks, identity-free schemas, M1), because only the tool knows the record's owner. The policy decides *whether the call may happen*; the tool decides *which data the user may see*.

---

## 4. Tests (real results)

Full suite with pgvector and audit integration enabled (`AEGIS_TEST_DATABASE_URL`, both migrations applied):

```
354 passed, 21 skipped in 14.34s
```

The 21 skipped are the `live` provider tests (no `ANTHROPIC_API_KEY`, no Ollama here). `ruff check`, `ruff format --check` and `mypy` (strict, 117 files) are clean. The retrieval gate still passes (hit rate 0.89, 0 access violations).

New in this milestone:

| File | Tests | What it proves |
|---|---|---|
| `tests/policy/test_policy.py` | 30 | the agent × tool matrix from spec §14; forbidden and unclassified tools; all reasons reported; production restrictions; HIGH → approval; `write_not_authorized`; roles; invalid policy refused; evaluation error → deny; version hash; **host allowlists = policy grants**; **declared risk = policy risk** |
| `tests/unit/test_gateway.py` | 8 | decision then outcome events with user, agent, version, request, thread, resource; deny and approval never call the handler; no agent → deny; per-call agent (MCP servers); invalid args refused before policy; audit down → writes refused, reads allowed; tool errors recorded as outcomes |
| `tests/security/test_governance_bypass.py` | 6 | the bypass table above |
| `tests/integration/test_audit_postgres.py` | 5 | events round-trip; UPDATE, DELETE and TRUNCATE rejected by the database; gateway writes to Postgres |
| `tests/unit/test_cli.py` | +4 | `policy check` allow/deny and exit codes; broken policy file → exit 2; memory audit store hint |

All M1–M5 tests run with the gateway on, including the spec scenarios over `local` and `mcp_inprocess`. The only test changes needed were identities: the MCP boundary tests now use agents and environments the policy knows.

---

## 5. Trace: one governed call over MCP

From the CLI demo above (fake model, `--tools mcp_inprocess`, `AUDIT_STORE=postgres`):

```
$ aegisdesk audit --user E1004 --limit 2
2026-09-30 12:52:20 decision create_ticket  user=E1004 agent=service_desk@0.1.0 env=development decision=allow outcome=allow request_id=18e0cc06-… thread_id=6e81ae49-…
2026-09-30 12:52:20 outcome  create_ticket  user=E1004 agent=service_desk@0.1.0 env=development decision=allow outcome=ok    request_id=18e0cc06-… thread_id=6e81ae49-…
```

1. The router sent the request to the Service Desk agent, and the model asked for `create_ticket`.
2. The host allowlist has it, so the host signed a token: `sub=E1004`, `act=service_desk@0.1.0/development`, `rid`, `tid`, `aud=aegisdesk-mcp-action`.
3. The action server verified the token and validated the arguments. The gateway asked the policy with **the token's** agent: MEDIUM risk, in `authorized_writes`, granted to `service_desk`, environment matches → ALLOW.
4. The decision event was committed to `audit_events` **before** the ticket was created.
5. The tool ran (ownership, idempotency key), then the outcome event was written.

A denied call looks the same with `decision=deny outcome=deny reasons=…`, then `outcome=policy_denied`, and no tool run.

---

## 6. Not built yet

- **Human approval** (M7): `REQUIRE_APPROVAL` stops the call today. M7 turns it into a LangGraph interrupt, with an approval record (`approval_id`, `approver_id` in the audit event) and resume after restart. `required_roles` is ready for the approve action.
- **trace_id** (M8): the field exists and is filled once OpenTelemetry tracing is added.
- **OPA:** the input/decision contract mirrors an OPA query, so `PolicyEngine` could be swapped for an OPA client without touching callers ([ADR 0009](../adr/0009-deterministic-policy-engine.md)).
- **Model policy:** the model allowlist (`config/models.yaml`, M0) is still enforced where models are built, not by this engine.

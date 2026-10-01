# Milestone 4: Multi-agent (Supervisor + specialists)

**Status:** complete
**Builds:** a Supervisor and three specialist agents (Knowledge, Service Desk, Access) as LangGraph subgraphs with explicit, bounded handoffs; per-agent tool isolation; context isolation; a code-level citation check for the Knowledge agent; and the access domain (8 applications, access records, deterministic eligibility, access requests that are recorded but never granted).
**Does not build:** the policy engine (OPA, M6) or human approval (M7). Requests that need approval stay `awaiting_approval`.

Design reference: [`docs/AGENT_DESIGN.md`](../AGENT_DESIGN.md). Decision: [ADR 0007](../adr/0007-specialised-agents-deterministic-supervisor.md).

---

## 1. Concepts introduced

### Router pattern
The supervisor does not "chat" with the specialists. Each request goes through one structured routing step:

```
classify_request (LLM)  →  RoutingPlan{tasks: [{agent, instruction}, ...], out_of_scope}
supervisor (code)       →  run each pending task, in order, then respond
```

The model decides *what the user wants*. Code decides *what happens*: the order, the limits, when to stop. The supervisor node is a plain Python function that returns `Command(goto=...)`. It has no tools and makes no model call, so nothing in a message can talk it into anything.

### Specialisation
Each specialist has a narrow prompt (`knowledge@v1`, `service_desk@v3`, `access@v1`) and a narrow tool set. A narrow prompt is easier for the model to follow, and easier to evaluate and version independently.

### Tool isolation
Tool sets are fixed in one place (`agents/supervisor.py::specialist_tools`):

| Agent | Tools |
|---|---|
| Supervisor | none |
| Knowledge | `search_knowledge_base`, `retrieve_document`, `request_handoff` |
| Service Desk | `get_my_assets`, `list_my_tickets`, `get_ticket`, `create_ticket`, `search_knowledge_base` (read-only), `request_handoff` |
| Access | `get_employee_profile`, `list_my_access`, `get_application`, `check_access_eligibility`, `create_access_request`, `request_handoff` |

This is enforced by the executor, not by the prompt. The Knowledge agent calling `create_ticket` gets `unknown_tool`, exactly like a hallucinated tool in M1 (`test_agents_cannot_use_other_agents_tools`).

*Decision:* the Service Desk agent keeps a read-only knowledge search, because the spec's Scenario C has it offer documented troubleshooting before opening a ticket. It is a LOW-risk read, and every result is still access-filtered for the user.

### Context isolation
Each specialist runs as a separate compiled subgraph with its **own state**. The parent passes it:

* the visible conversation (human turns and final answers, at most 6 messages), and
* its task: `"<the employee's message>\n\nYour task: <instruction>"`.

The specialist's tool calls and results stay inside its own state (compiled with `checkpointer=False`). The parent stores only the answer and the trajectory. So:

* the Access agent never sees the Knowledge agent's retrieved passages, including an injected one;
* the persisted thread holds only `human` and final `ai` messages (`aegisdesk thread` shows no tool traffic);
* the router sees a short, clean history, which also keeps token use down.

### Subgraphs
`build_tool_agent_graph` (the M2 graph, generalised) is compiled once per specialist with a different prompt and executor, then run from a parent node. The M2 single-agent engine is the same function with a checkpointer; nothing was duplicated.

### Command and handoff
Two uses of `Command`:

1. The **supervisor** returns `Command(goto="access")` and similar. The graph declares the possible destinations, so the diagram and validation know every route.
2. A **specialist** that meets work outside its scope calls `request_handoff(target_agent, instruction)`. The tool does nothing itself. The parent node reads it from the specialist's trajectory and adds a task, **only if** all of these hold:
   * the target is another agent (the schema forbids handing off to yourself);
   * the same task is not already in the plan;
   * the handoff budget is not exhausted (`AGENT_MAX_HANDOFFS`, default 2), and the plan has room.

Agents never call each other directly. Every transfer goes through the supervisor's rules.

### Multi-agent failure modes, and what handles each

| Failure mode | Handling | Test |
|---|---|---|
| Router output invalid or empty | Safe "please rephrase" answer; no specialist runs | `test_unparseable_routing_fails_safely` |
| Router invents an agent | Schema rejects it (enum) → same safe path | `test_router_output_cannot_invent_an_agent` |
| Too many tasks | Plan capped at 3 in code | `test_plan_is_capped_at_three_tasks` |
| Off-topic request | `out_of_scope` → fixed answer, no specialist call | `test_out_of_scope_calls_no_specialist` |
| Agents handing work back and forth | Handoff budget, no self-handoff, no duplicates | `test_handoff_budget_stops_ping_pong` |
| A specialist crashes | That task is `failed`; the other tasks still run | `test_a_failing_specialist_does_not_break_the_turn` |
| A specialist loops | Its own step and tool-call limits (M1/M2), then LangGraph's `recursion_limit` | M2 tests |
| Knowledge answer not grounded | Code checks citations against this run's retrieved chunks; otherwise a safe fallback | `test_knowledge_answers_must_cite_what_was_retrieved` |
| Injected document steers an agent | The agent lacks the tools; the handoff goes to Access, which recomputes eligibility → `not_eligible` | `test_injected_document_cannot_make_the_knowledge_agent_act` |

### Access requests: recorded, never granted
`domain/access.py::evaluate_eligibility` is a pure function of trusted data: the employee (status, department, roles), the application (allowed departments and roles, contractor rule, approvals) and existing access and requests (with expiry). `create_access_request` **recomputes it itself**; the agent's own check is advisory only.

| Example | Result |
|---|---|
| E1004 (finance) → FinanceERP | eligible, needs `manager` → `awaiting_approval` |
| E1004 → AnalyticsHub | eligible again (previous access expired), needs `data_owner` |
| E1005 (contractor) → GitHub | eligible, sponsor (`manager`) approval added |
| E1005 → Salesforce | `contractors_not_allowed` |
| E1006 (IT admin) → ProductionDB | eligible, needs `manager` + `security` |
| E1012 → ProductionDB | `request_already_open` (AR-1007) |
| E1004 → HRAdmin | `not_in_allowed_department_or_role` |
| E1002 → Confluence | eligible, no approval → `auto_approved` |

Access itself is never provisioned in M4. Collecting approvals and provisioning is the M7 workflow, gated by the M6 policy engine.

## 2. When NOT to use multiple agents

A multi-agent design costs something on every request: an extra routing call (visible in `llm_calls=3` for a request a single agent answers in 2 calls), more prompts to version and evaluate, and new failure modes (misrouting, handoff loops). It pays off here because the three areas have **different tools and different risk**. Tool isolation limits what a confused or manipulated agent can reach. For a small tool set with one risk level, a single agent with a good prompt is simpler, cheaper and easier to debug. M9 will measure the trade-off (quality vs latency vs cost) against the single-agent engine, which is kept (`--engine graph`).

## 3. Execution paths (actual output, offline fake model)

```
$ aegisdesk agent --as E1004 "Please create an access request for FinanceERP for month-end reporting"
→ router  in=161 out=16 7ms -> access: 'Please create an access request for FinanceERP for month-end reporting'
  · [access] step 1 model  in=184 out=22 0ms -> create_access_request
  · [access] step 1 tool   create_access_request({"application": "Please create an access request for FinanceERP ...", ...}) ok
  · [access] step 2 model  in=197 out=17 0ms -> final answer
← access done
Assistant: [fake model] Tool results: {"request_id":"AR-1013","application":"FinanceERP","status":"awaiting_approval","approvals_required":["manager"], ...
  [supervisor@0.1.0 prompt=router@v1 ... llm_calls=3 tool_calls=1 ...]

$ aegisdesk agent --as E1005 "I need Salesforce access"
→ router ... -> access: 'I need Salesforce access'
  · [access] step 1 tool   check_access_eligibility({"application": "I need Salesforce access"}) ok
Assistant: ... "eligible":false,"reason":"contractors_not_allowed","explanation":"Salesforce is not available to contractors." ...
```

The fake passes the whole sentence as `application`. `find_application` resolves an application name mentioned in free text, which also makes real models more robust.

What happens in the first run:

1. `authenticate("E1004")`; the thread ownership check (from M2).
2. `start_turn` resets the per-turn state.
3. `classify_request` sends the router prompt and the visible history to the model, and gets a validated `RoutingPlan`: one task for `access`.
4. `supervisor` finds the pending task and returns `Command(goto="access")`.
5. The `access` node runs the Access subgraph with the task message. Its model calls `create_access_request`.
6. `ToolExecutor` (M1) validates the arguments, derives the user and an idempotency key, and runs the handler. The handler resolves FinanceERP, **recomputes eligibility** (eligible, manager approval), and records AR-1013 as `awaiting_approval`. Nothing is granted.
7. The subgraph's answer and trajectory return to the parent; the task is marked `done`. `supervisor` finds nothing pending and goes to `respond`, which passes the single answer through unchanged.

## 4. Libraries

No new libraries. New LangGraph features: `Command` (routing from a node, with declared `destinations`), subgraphs run from parent nodes, and `compile(checkpointer=False)` to keep a subgraph's state out of the parent's checkpoints.

## 5. How to run it

```bash
uv run aegisdesk agent --as E1004 "How do I configure VPN on macOS?"            # → knowledge
uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"             # → service_desk
uv run aegisdesk agent --as E1004 "Please create an access request for FinanceERP for month-end reporting"
uv run aegisdesk agent --as E1004 "I need HRAdmin access"                      # refused
uv run aegisdesk agent --as E1005 "Please create an access request for GitHub for my contract work"
uv run aegisdesk agent --as E1004 --engine graph "..."                         # the single agent, for comparison
```

Multi-intent requests ("What does GP-512 mean, and can I get AnalyticsHub?") need a real model: the offline fake always produces a one-task plan. The multi-task path is covered by scripted tests.

## 6. Test results at the milestone boundary

Run in the development container, with the local PostgreSQL + pgvector database restarted:

* `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy src tests` (strict, 93 files): pass.
* `uv run pytest` with `AEGIS_TEST_DATABASE_URL`: **257 passed, 21 skipped** (live model tests). New in M4:
  * `test_access_domain.py`: eligibility matrix, expiry, application lookup;
  * `test_access_tools.py`: recorded-not-granted, recomputed eligibility, idempotency, open-duplicate refusal;
  * `test_supervisor_graph.py`: topology, tool isolation, routing, multi-intent ordering and combination, context isolation, out-of-scope, routing failure, plan cap, handoffs (accepted, to self, budget), citation verification, a crashing specialist, thread persistence and ownership;
  * `tests/security/test_multi_agent_security.py`: injection via the Knowledge agent, acting for a colleague, an invented agent;
  * `tests/e2e/test_spec_scenarios.py`: scenarios A, B and C, the FinanceERP workflow up to `awaiting_approval`, HRAdmin refusal, the contractor sponsor rule.
* Not run here: a live routing test with real models (4 spec scenarios, including a multi-intent one). Run `uv run pytest -m live`.

## 7. Known limitations

| Limitation | Addressed in |
|---|---|
| Policy lives in Python (`evaluate_eligibility`, per-tool ownership checks) | M6: OPA policies behind a tool gateway |
| `awaiting_approval` requests are never approved; no interrupt/resume | M7: human-in-the-loop with LangGraph `interrupt` |
| The router re-plans every turn and cannot ask a clarifying question before routing | Later: a `clarify` path |
| One model serves router and specialists | M9: compare configurations (e.g. small router + larger specialists) |
| Service Desk answers using knowledge search are not citation-checked | M9 evaluations; or route pure policy questions to Knowledge (as the router prompt asks) |

## 8. What M5 adds

MCP: the enterprise tools move out of process into two MCP servers, a **read** server and an **action** server, so they sit behind different trust and risk boundaries. User identity, agent identity and the request ID travel with every call.

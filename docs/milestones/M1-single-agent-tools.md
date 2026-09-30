# Milestone 1: Single agent + local tools

**Status:** complete
**Builds:** one Service Desk agent that looks up the signed-in employee's equipment and tickets and creates tickets, using local Python tools and a hand-written agent loop.
**Does not build:** LangGraph, multiple agents, RAG, MCP, OPA or a database. The data is in memory and seeded from JSON.

---

## 1. Concepts introduced

### Tool calling
A model cannot *do* anything. It can only produce text. Tool calling is a protocol in which one kind of text is a *request* for the application to act:

```
application → model:  messages + a list of tools (name, description, JSON Schema)
model → application:  "call get_ticket with {"ticket_id": "INC-1001"}"   (id = c1)
application:          validates, runs, and records the result
application → model:  tool result for id c1: {"ticket": {...}}
model → application:  final answer in plain language
```

Anthropic calls these `tool_use` and `tool_result` blocks; Ollama uses OpenAI-style `tool_calls`. LangChain gives both the same shape: `AIMessage.tool_calls` going out and `ToolMessage(tool_call_id=...)` coming back. Every tool call must get a result with the matching ID, even one the application refuses to run.

M0's structured output was the same mechanism with the model *forced* to call a single tool. In M1 the model *chooses* whether to call one, and which.

### Tool schemas
A tool's JSON Schema is generated from a Pydantic input model. It does two jobs:

* **For the model** it is documentation. The tool name, description and field descriptions are part of the prompt, and they are how the model decides when to use the tool. Poor descriptions produce poor tool choices.
* **For the application** it is a contract. Arguments are validated against it before anything runs, and all the input models forbid unknown fields.

Look at what the schemas **do not** contain: an employee ID. `get_my_assets` takes no arguments at all. Whose assets it returns is decided by the authenticated session, so there is no argument for the model or a prompt injection to tamper with. The spec names a `get_asset` tool; we deliberately made it `get_my_assets` for this reason (see [ADR 0003](../adr/0003-tools-take-identity-from-trusted-context.md)).

### The agent loop
An agent is an LLM called in a loop, where the model's output decides what happens next:

```python
messages = [system, *history, user]
for step in range(max_steps):
    reply = model_with_tools.invoke(messages)
    messages.append(reply)
    if not reply.tool_calls:
        return reply.text  # done
    for call in reply.tool_calls:
        outcome = executor.execute(call, user)  # the application acts
        messages.append(ToolMessage(outcome, tool_call_id=call.id))
return "couldn't finish within the step limit"
```

That is essentially `agents/loop.py`. Compared with M0's single call:

* **The number of model calls is not fixed.** The model decides when it is done, so the application *must* impose limits (`AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`). Without them, a confused model burns tokens until something breaks.
* **The trajectory matters, not just the answer.** Which tools ran, with what arguments, and in what order. `AgentRun.trajectory` records each model step and each tool step, which is the raw material for traces (M8) and trajectory evaluations (M9).
* **The context grows every step.** Each step re-sends everything so far, so the token cost per step rises. Compare `in=` across the steps in the CLI output.

### Model responsibilities vs application responsibilities

| The model does | The application does |
|---|---|
| Understand the request | Authenticate the user (before the model runs) |
| Choose a tool and propose arguments | Offer only the tools this agent may use |
| Decide when it has enough information | Validate arguments against the schema; reject unknown fields |
| Write the final answer | Supply the user's identity to the tool, never from the arguments |
| | Check ownership (someone else's ticket reads as "not found") |
| | Make writes idempotent |
| | Shape errors so no internals leak |
| | Enforce step and tool-call limits |
| | Record the trajectory |

Everything on the right-hand side holds **whatever the model outputs**. `tests/security/test_service_desk_boundaries.py` checks this with a scripted model that behaves as a prompt-injected model would: it passes someone else's `employee_id`, reads another employee's ticket, files a ticket as the manager, calls tools it was never given (`direct_grant_production_admin`), repeats writes, and loops forever. Every one of these is stopped by code, not by the prompt.

## 2. Architecture for this milestone

```
 aegisdesk agent --as E1004 "..."
        │
        ▼
 authenticate(repository, "E1004") ──► UserContext   (trusted; built before any model call)
        │
        ▼
 ToolCallingAgent.run(text, user)
   ┌────────────────────────────────────────────────────────────┐
   │ messages = [system(service_desk@v1), *history, human]      │
   │   loop ≤ max_steps:                                        │
   │     model.bind_tools(definitions).invoke(messages)         │
   │     no tool calls ──► final answer                         │
   │     each tool call ──► ToolExecutor.execute(name, args,    │
   │                           user=UserContext, request_id)    │
   │                            │ 1 lookup (unknown → error)    │
   │                            │ 2 validate (Pydantic, strict) │
   │                            │ 3 context + idempotency key   │
   │                            │ 4 handler ──► repository      │
   │                            │ 5 validate output             │
   │                    ToolMessage(result, tool_call_id)       │
   └────────────────────────────────────────────────────────────┘
        │
        ▼
 AgentRun(answer, stop_reason, trajectory, usage, history, request_id, prompt version)
```

### Files

| File | Role |
|---|---|
| `data/seed/{employees,assets,tickets}.json` | Synthetic Northstar data (a terminated employee, a contractor, managers, several ticket states) |
| `src/aegisdesk/domain/models.py` | Domain records: `Employee`, `Asset`, `Ticket` |
| `src/aegisdesk/domain/repository.py` | In-memory store; `create_ticket` honours idempotency keys |
| `src/aegisdesk/identity/context.py` | `UserContext` and simulated `authenticate` (unknown or terminated users are refused) |
| `src/aegisdesk/tools/base.py` | `ToolSpec` (schema, handler, risk, read/write, idempotency, owner, timeout), `ToolCallContext`, `ToolError` |
| `src/aegisdesk/tools/service_desk.py` | The four tools, their input and output schemas, and handlers |
| `src/aegisdesk/tools/executor.py` | `ToolExecutor`: lookup, validation, trusted context, error shaping, output checks |
| `src/aegisdesk/agents/loop.py` | `ToolCallingAgent`: the loop, limits and trajectory |
| `src/aegisdesk/agents/service_desk.py` | Wires the model, prompt, tools and limits into the Service Desk agent |
| `prompts/service_desk/v1.yaml` | Agent instructions: behaviour only, no security rules that code relies on |
| `src/aegisdesk/llm/fake.py` | Demo mode can now drive an agent loop offline |
| `src/aegisdesk/cli.py` | `aegisdesk agent --as <employee>` (single request or interactive) |

### The tools

| Tool | Access | Risk | Arguments the model supplies | Notes |
|---|---|---|---|---|
| `get_my_assets` | read | LOW | none | Owner comes from the session |
| `list_my_tickets` | read | LOW | `include_closed` | Added beyond the spec's M1 list so the agent can spot an existing ticket before creating a duplicate |
| `get_ticket` | read | LOW | `ticket_id` (normalised, pattern `INC-\d{4,}`) | Another employee's ticket returns the same `not_found` as a missing one |
| `create_ticket` | write | MEDIUM | `title`, `description`, `category`, `priority` | Requester comes from the session; idempotency key from request + arguments |

Risk levels are recorded now and enforced by policy in M6. The timeout is recorded but not enforced, because in-process calls can't usefully be timed out. It becomes real when tools move behind MCP (M5, M11).

### Idempotency
The executor derives a key for every write: `sha256(user, request_id, tool, validated arguments)`. The repository stores `key → ticket`, so:

* the model repeating an identical `create_ticket` within one request returns the same ticket with `created: false`;
* a client retrying the whole request with the same `request_id` gets the same ticket;
* a genuinely new request (new `request_id`) can create a new ticket.

This is deliberately separate from the prompt instruction "check open tickets before creating a duplicate". The prompt tries to make the model *behave well* (a quality property, measured by evaluations in M9). Idempotency *guarantees* that retries cannot duplicate writes (a correctness property, enforced in code).

## 3. Libraries

No new libraries. `BaseChatModel.bind_tools` (langchain-core, M0) sends the tool definitions in each provider's format, and `dereference_refs` inlines Pydantic's `$ref`s so small local models see flat schemas.

What we chose **not** to use yet:

* **LangChain's `@tool` decorator / `AgentExecutor`:** they would hide the loop this milestone is meant to teach.
* **LangGraph's `create_react_agent`:** the same loop in one call. We build it by hand first, then rebuild it as an explicit graph in M2.

## 4. Execution path: Scenario B, "What laptop is assigned to me?"

Actual output on the offline fake model:

```
$ uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"
  · step 1 model  in=220 out=1 5ms -> get_my_assets
  · step 1 tool   get_my_assets({}) ok 0.1ms
  · step 2 model  in=224 out=8 0ms -> final answer
Assistant: [fake model] Tool results: {"assets":[{"asset_tag":"NS-LT-0101","asset_type":"laptop","model":"Dell Latitude 7440",...
  [service_desk@0.1.0 prompt=service_desk@v1 model=fake/fake-scripted stop=final_answer llm_calls=2 tool_calls=1 tokens=453 ...]
```

1. `cmd_agent` loads the repository and calls `authenticate("E1004")`, which produces `UserContext(E1004, finance, manager E1010)`. The model is not involved yet.
2. `build_service_desk_agent` builds the model from settings, loads `service_desk@v1`, creates a `ToolExecutor` with exactly the four Service Desk tools, and binds their definitions to the model.
3. **Step 1:** the model receives `[system, human]` plus the four tool schemas, and replies with a tool call: `get_my_assets({})`.
4. The executor finds the tool, validates `{}` against `GetMyAssetsInput`, builds `ToolCallContext(user=E1004)` and runs the handler, which asks the repository for E1004's assets and returns `AssetView`s.
5. The result goes back as `ToolMessage(tool_call_id=...)`.
6. **Step 2:** the model sees the tool result and replies without tool calls, which ends the loop with `stop_reason=final_answer`.

With a real model, the step-2 answer is a sentence such as "You have a Dell Latitude 7440 (asset NS-LT-0101)". The fake simply returns the tool result as text.

## 5. Execution path: Scenario C, "My VPN keeps disconnecting… Create a ticket."

The fake model goes straight to `create_ticket` (INC-1008). A real model following `service_desk@v1` should first call `list_my_tickets`, find **INC-1001, "VPN disconnects every few minutes", already open**, and point to it instead of creating a duplicate. Try it with a real model:

```bash
MODEL_PROVIDER=anthropic MODEL_NAME=claude-haiku-4-5-20251001 ANTHROPIC_API_KEY=... \
  uv run aegisdesk agent --as E1004 "My VPN keeps disconnecting. I already followed the troubleshooting guide. Create a ticket."
```

Whether it does so is up to the model and the prompt, so it will vary by model. That is why it becomes an evaluation case in M9 instead of a unit test. What does *not* vary is that the ticket is filed under E1004, and a retry of the same request cannot file it twice.

## 6. How to run it

```bash
uv run aegisdesk agent --as E1004 "What laptop is assigned to me?"
uv run aegisdesk agent --as E1004 "Show me ticket INC-1003"   # E1001's ticket → not_found
uv run aegisdesk agent --as E1007 "hi"                       # terminated → login refused
uv run aegisdesk agent --as E1004                            # interactive; history kept between turns

uv run pytest tests/unit tests/security
uv run pytest -m live   # real providers, including two agent tests
```

Other synthetic users to try: `E1001` (engineer), `E1002` (sales), `E1005` (contractor), `E1006` (IT admin), `E1010` (finance manager).

## 7. Test results at the milestone boundary

Run in the development container:

* `uv run ruff check .` and `uv run ruff format --check .`: pass
* `uv run mypy src tests` (strict): no issues in 45 files
* `uv run pytest`: **106 passed, 8 skipped**. The 8 skipped are the live tests: 2 from M0 and 2 new agent tests, each run once for Anthropic and once for Ollama. No `ANTHROPIC_API_KEY` or Ollama server was available in the container, so **no real model has run this agent yet.** Run `uv run pytest -m live` locally.

## 8. Known limitations (deliberate, fixed later)

| Limitation | Addressed in |
|---|---|
| The loop is plain Python; state lives only in memory for one process | M2 (LangGraph state + checkpointing) |
| Data is in memory; created tickets vanish on exit | Persistence with PostgreSQL |
| Ownership checks live inside each tool handler | M6 (Tool Gateway + OPA policy for every tool) |
| Risk levels are recorded, not enforced | M6 |
| Tool timeouts and retries are not enforced | M5 / M11 |
| Troubleshooting guidance comes from the model's general knowledge | M3 (RAG) |
| Only the CLI prints the trajectory | M8 (OpenTelemetry + LangSmith traces) |

## 9. What M2 adds

The same agent rebuilt as a **LangGraph** graph: an explicit typed state instead of a local `messages` list, nodes for "call model" and "run tools", a conditional edge for "tool calls or done?", a checkpointer so a conversation survives a restart, and streaming of steps. The executor, tools, identity and tests from this milestone stay as they are. The point of M2 is to see exactly which parts LangGraph replaces.

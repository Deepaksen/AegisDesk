# ADR 0004: Use LangGraph for agent orchestration, with our own nodes

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
M1 implemented the agent as a hand-written loop. Upcoming requirements go beyond what a loop handles comfortably:

* conversations that survive restarts (spec §9);
* workflows that pause for human approval and resume, possibly days later, after a restart (§5, §15);
* several specialist agents with explicit handoffs and bounded iterations (§6, §8);
* observable state transitions and streaming (§23, §33).

## Decision
1. Orchestrate agents with **LangGraph** `StateGraph`s: typed state, named nodes, conditional edges, and a checkpointer per thread.
2. **Write our own nodes.** Do not use the prebuilt `ToolNode`, `tools_condition` or `create_react_agent`. Tool execution always goes through `ToolExecutor`, which is the security boundary (and, from M6, the policy gateway).
3. **Keep routing decisions in code.** Conditional edges are plain Python functions over state. The model influences them only through its messages (tool calls or not); it never picks a node name directly.
4. **Check authorization outside the graph** before writing to a thread (thread ownership), and pass identity in as input on every turn.
5. Use the **SQLite checkpointer** now and move to the PostgreSQL checkpointer with the application database.

## Consequences
* Restart-safe conversations now, and the same mechanism (`interrupt` + resume) will be used for approvals in M7.
* The topology is data: it can be drawn, asserted in tests and compared between versions.
* Runs are streamed node by node, which gives natural hook points for tracing in M8.
* A new dependency whose API has changed quickly; version bounds are pinned via `uv.lock`, and our logic stays in plain functions so a migration would be contained.
* The M1 loop is kept as a reference implementation, and the security suite runs against both engines.

## Alternatives considered
* **Keep the hand-written loop and add a history table:** workable for chat memory, but pause/resume at an arbitrary step, streaming and multi-agent routing would all have to be rebuilt by hand.
* **Temporal (or a similar durable-workflow engine):** excellent durability, but heavy infrastructure for a local learning platform, and not designed around LLM message state.
* **Higher-level agent frameworks (CrewAI, AutoGen, OpenAI Agents SDK):** faster to demo, but they hide control flow. The spec explicitly asks for explicit transitions over "agents talking to agents".
* **LangGraph prebuilt ReAct agent:** a few lines of code, but it executes tools itself, which would bypass our executor and, later, our policy gateway.

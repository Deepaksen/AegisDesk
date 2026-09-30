# ADR 0007: Specialised agents behind a deterministic supervisor

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
AegisDesk covers three areas with different tools and different risk: documentation Q&A (read-only), equipment and tickets (low-impact writes), and application access (sensitive writes, approvals). The spec asks for a Supervisor with Knowledge, Service Desk and Access agents (§6), explicit graph transitions instead of free-form "agents talking to agents" (§8), and bounded loops.

## Decision
1. **Specialist subgraphs:** each specialist is the M2 tool-agent graph compiled with its own prompt and its own `ToolExecutor`, so its tool set is fixed in code.
2. **Router + deterministic supervisor:** one structured LLM call (`RoutingPlan`) decides which specialists are needed. A plain Python supervisor node executes the plan in order through `Command(goto=...)`. The supervisor has no tools and makes no model calls.
3. **Handoffs through the supervisor only:** specialists can *request* a handoff with a no-op tool. The parent applies fixed rules (not to self, no duplicates, a budget).
4. **Context isolation:** specialists get the visible conversation plus their task, and run in their own state (compiled with `checkpointer=False`). Only answers and trajectories reach the parent thread.
5. **Answers combined by code** (sections), not by another model call.
6. **Deterministic domain rules:** access eligibility is a pure function, recomputed inside `create_access_request`.

## Consequences
* A manipulated or confused agent can only use its own tools. The Knowledge agent cannot write, the Service Desk agent cannot request access, and no agent can grant access.
* Costs: one extra model call per turn (routing), three more prompts to version and evaluate, and new failure modes (misrouting, handoff loops). Each failure mode has an explicit handler and a test.
* The persisted thread is small and clean (visible turns only), which suits audit and privacy, but specialists cannot see each other's working, so cross-agent context must be passed explicitly (task instructions).
* The single-agent engine stays available as a baseline for the M9 comparison.

## Alternatives considered
* **One agent with all tools:** simplest, but it gives every capability to every request, and prompt injection anywhere reaches everything. Rejected by the spec ("do not give every agent every tool").
* **LLM supervisor that calls agents as tools (agent-as-tool):** flexible, but the flow is then decided by the model, loops need extra guarding, and the plan is harder to test and audit.
* **Peer-to-peer handoffs (swarm style):** agents transfer control to each other directly. Natural for conversations, but hard to bound and to reason about. We keep handoffs as requests to a deterministic supervisor.
* **LLM synthesis of multi-agent answers:** reads better, but adds a model call and a place where facts can be invented or citations lost.

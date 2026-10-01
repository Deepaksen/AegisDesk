# ADR 0015: FastAPI as a thin adapter over a shared runtime, with trusted-header identity

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §33 asks for a production-like API: threads, messages, approvals, health, readiness, metrics, streaming where useful, and OpenAPI. Milestone 10 says: "Do not redesign backend around the UI." Until now the components (repository, audit log, approval service, checkpointer, tool transport, supervisor) were assembled separately by each CLI command.

Identity: the platform simulates login by employee ID (M1). The user chose a **trusted header** (`X-Employee-Id`) over API-issued session tokens.

## Decision
1. **One runtime per process.** `AegisRuntime` (`src/aegisdesk/runtime.py`) builds the components once and holds them open. It exposes the use cases: send a message, view a thread, decide an approval and resume the workflow, read audit events, check readiness. The API and the CLI's approval commands both call it, so "decide, then resume" has one implementation.
2. **The API is a thin adapter.** Routes authenticate, call the runtime, and map results to Pydantic response models (the OpenAPI contract). No agent, policy or approval logic lives in `api/`.
3. **Identity enters in one place.** A FastAPI dependency (`current_user`) reads `X-Employee-Id` and validates it against the directory. Request bodies forbid unknown fields and have no identity fields.
4. **Streaming uses server-sent events**, fed by the graph's existing per-node updates (M2), relaying safe activity summaries built by code, then one result.
5. **Errors use RFC 9457 problem details.** Foreign threads and invisible approvals are `404`, never `403`. Internal errors never carry exception text.
6. **Idempotency keys** map to request IDs, reusing the idempotent write tools (M1) and the per-request write budget (M9).
7. **Threads are serialized per thread ID** inside the process, so two concurrent turns cannot interleave state.

## Consequences
- **The trusted header is only as safe as the network.** Anyone who can reach the API port can act as any employee. The API must sit behind an authenticating gateway that sets the header and strips client-supplied copies. In compose, only the UI and developers reach it; this is acceptable for a synthetic-data learning platform and explicitly not for production. Replacing it with OIDC bearer-token verification (spec stretch goal) changes `api/auth.py` only.
- The runtime serializes turns per thread in one process. Across replicas the checkpointer is the only shared state, so two replicas could still run one thread concurrently. A database advisory lock per thread is the fix if that ever matters.
- The CLI's `approvals approve` builds a runtime per invocation. That is slightly heavier, but the code path is identical to the API's.
- The UI is just another API client, so every UI behaviour is testable through the API.

# ADR 0017: Resilience by safe degradation: one retry policy, circuit breakers, idempotent replay, write-ahead audit

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Milestone 11 asks for appropriate behaviour under six failures: MCP timeout, database error, model timeout, malformed response, duplicate request, tool failure. Earlier milestones already covered parts of this:
- M1: idempotent writes;
- M5: read-only MCP retries, and write tools never retried;
- M6: writes are refused when they can't be audited;
- M8: fault injection.

Gaps found while mapping the failures:
- A model timeout in the router crashed the request (500).
- Malformed tool calls were silently dropped, which could leave an empty answer.
- A retried API request re-ran the model and appended the message twice.
- An approval could be recorded without its audit event.
- A request approved while provisioning was failing stayed approved-but-unprovisioned forever.

The user chose safe degradation over a fallback model, and replay of stored responses for duplicates.

## Decision
1. **One retry policy for model calls, owned by the application.**
   - `ModelGuard` classifies failures (`model_timeout`, `model_unavailable`, `model_rate_limited`, `model_error`) and retries the retryable ones with exponential backoff and jitter (`MODEL_MAX_RETRIES`, `MODEL_RETRY_BACKOFF_SECONDS`).
   - Provider SDK retries are off, so retries are not multiplied and every attempt is visible in traces and in `aegisdesk_model_errors_total`.
   - Model calls have no side effects, so retrying them is safe.
   - Unclassified exceptions (bugs) are re-raised, never disguised as an outage.
2. **Circuit breakers per dependency** (each model and each MCP server): after `BREAKER_FAILURE_THRESHOLD` failed calls, fail fast for `BREAKER_RESET_SECONDS`, then allow one trial call. `/ready` reports open circuits without going unready, because a degraded service is still serving.
3. **Degrade, never crash or improvise.**
   - A model outage ends the turn with a fixed "temporarily unavailable" answer (`stop_reason: model_error`), and the API returns `503` with `Retry-After`.
   - A malformed tool call is answered like a refused call, so the model can correct it.
   - An empty reply is replaced by a fixed answer.
   - Tools report `unavailable` when a store or MCP server is down, and the model tells the user.
   - Write tools are still never retried automatically.
4. **Database outages are one error type.** `StoreUnavailableError` is raised by a guard around the access store, by the checkpointer path and by the idempotency store; the API maps it to `503`. Audit outages refuse writes (M6), and now also refuse approval decisions.
5. **Write-ahead audit for human decisions.** The approval service records the decision event *before* changing the approval, and refuses the decision if it cannot. This is the same rule as the tool gateway: never act without a record.
6. **Duplicate requests replay the stored response**, keyed by (user, `Idempotency-Key`) and fingerprinted by thread and text.
   - Replay returns the stored response; a request still in progress gets `409`; a reused key for a different message gets `422`.
   - Stored in memory or in PostgreSQL (migration 0004, a row lock per key, so it works across replicas).
   - Only successful responses are stored. A failed attempt releases the key, so the retry really runs.
7. **Reconciliation instead of hoping.** `aegisdesk approvals reconcile` (IT admin) finishes requests made through the assistant that are settled but not provisioned. It resumes a thread still paused (the process died between decision and resume), or re-provisions after a failed attempt. It is idempotent and goes through the gateway.

## Alternatives not chosen
- **Fallback model on outage:** more moving parts, and a different model can behave differently on the same prompt. It can be added later inside `ModelGuard`, because callers already only see categories.
- **Retrying writes automatically** (for example, after an MCP timeout): the server may have done the work. Idempotent retries by the *client* (same key, same request ID) are safe; blind retries by the server are not.

## Consequences
- The reliability evaluation suite (`evals/reliability/faults_v1.yaml`, 13 cases) runs in CI with safety and regression gates. It found one more real gap: malformed tool calls were not traced. That is fixed.
- A model outage costs the user `retries × backoff` of waiting before the 503, until the breaker opens; after that it fails fast.
- Breakers are per process. Replicas each learn about an outage on their own, which is acceptable at this scale.
- A turn that failed with 503 has already added the user's message and the apology to the thread, and the retry adds the message again. That is visible but harmless; hiding it would need a transactional thread write.

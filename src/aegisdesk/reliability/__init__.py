"""Reliability (Milestone 11): circuit breakers, guarded model calls, store failures.

Every failure the spec lists has one owner here or at an existing seam:

* model timeout / outage  -> `model_guard.ModelGuard` (retries, breaker, classification)
* malformed model output  -> the graphs (invalid tool calls answered, empty replies replaced)
* MCP timeout / outage    -> `tools.remote` (read retries, never write retries) + a breaker
* database error          -> `errors.StoreUnavailableError`, mapped to 503 by the API
* tool failure            -> `tools.executor` error categories (M1/M6)
* duplicate request       -> `persistence.idempotency` (replay of the stored response)
"""

from aegisdesk.reliability.breaker import reset_breakers

__all__ = ["reset_breakers"]

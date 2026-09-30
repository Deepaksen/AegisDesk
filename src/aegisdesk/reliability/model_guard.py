"""Guarded model calls: classify failures, retry with backoff, fail fast when down (M11).

A model call can fail in ways that say nothing about the request: a timeout,
the provider overloaded or unreachable, a rate limit. The guard:

1. **classifies** the exception into a small set of categories
   (`model_timeout`, `model_unavailable`, `model_rate_limited`, `model_error`),
   so the rest of the system never inspects provider-specific exceptions;
2. **retries** the retryable ones with exponential backoff and jitter. A model
   call has no side effects, so retrying it is safe (unlike a write tool, which
   is never retried blindly). Provider SDK retries are switched off in
   `llm/factory.py`, so there is exactly one retry policy, visible in traces;
3. uses a **circuit breaker** per model: after repeated failed calls it fails
   immediately (`circuit_open`) instead of making every request wait for a
   timeout during an outage.

Unclassified exceptions (bugs) are re-raised unchanged: the guard must not
hide programming errors as "the model is down".

Faults (`AEGIS_FAULTS`): `model_timeout[:target]` and `model_unavailable[:target]`
raise inside each attempt, so the retry and breaker paths are the real ones.
`target` is `router` or the agent's prompt name (`knowledge`, `service_desk`...).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel

from aegisdesk.observability import faults, tracing
from aegisdesk.observability.metrics import instruments
from aegisdesk.reliability.breaker import CircuitBreaker, breaker

logger = logging.getLogger(__name__)

RETRYABLE = frozenset({"model_timeout", "model_unavailable", "model_rate_limited"})
# Categories that mean "the model service is down right now": the API answers 503.
OUTAGE = RETRYABLE | {"circuit_open"}

_TIMEOUT_NAMES = {
    "TimeoutError",
    "APITimeoutError",
    "TimeoutException",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "Timeout",
}
_UNAVAILABLE_NAMES = {
    "APIConnectionError",
    "ConnectError",
    "ConnectionError",
    "InternalServerError",
    "OverloadedError",
    "ServiceUnavailableError",
    "RemoteProtocolError",
}
_RATE_LIMIT_NAMES = {"RateLimitError"}
_CLIENT_ERROR_NAMES = {
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
    "NotFoundError",
    "UnprocessableEntityError",
}


class ModelCallError(RuntimeError):
    def __init__(self, category: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after

    @property
    def outage(self) -> bool:
        return self.category in OUTAGE


def classify(exc: BaseException) -> str | None:
    """A category for failures of the model *service*; None for anything else (a bug)."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _RATE_LIMIT_NAMES:
        return "model_rate_limited"
    if names & _TIMEOUT_NAMES:
        return "model_timeout"
    if names & _UNAVAILABLE_NAMES:
        return "model_unavailable"
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status >= 500:
        return "model_unavailable"
    if names & _CLIENT_ERROR_NAMES:
        return "model_error"  # configuration or request problem: retrying will not help
    return None


class ModelGuard:
    def __init__(
        self,
        name: str,
        *,
        retries: int = 0,
        backoff_seconds: float = 0.5,
        circuit: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.name = name
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.circuit = circuit or breaker(f"model:{name}")
        self._sleep = sleep
        self._jitter = jitter

    def call[T](self, fn: Callable[[], T], *, target: str) -> T:
        if not self.circuit.allow():
            self._count("circuit_open", target)
            raise ModelCallError(
                "circuit_open",
                f"The model {self.name} is failing; calls are paused.",
                retry_after=self.circuit.retry_after(),
            )
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                _inject_faults(target)
                result = fn()
            except Exception as exc:
                category = classify(exc)
                if category is None:
                    self.circuit.record_success()  # the service answered; this is our bug
                    raise
                self._count(category, target)
                logger.warning(
                    "model %s (%s) attempt %d/%d failed: %s",
                    self.name,
                    target,
                    attempt,
                    attempts,
                    category,
                )
                if category in RETRYABLE and attempt < attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                self.circuit.record_failure()
                raise ModelCallError(
                    category,
                    f"The model {self.name} failed ({category}) after {attempt} attempt(s).",
                    retry_after=self.circuit.retry_after() or None,
                ) from exc
            else:
                if attempt > 1:
                    tracing.current_span_attribute("aegisdesk.model.attempts", attempt)
                self.circuit.record_success()
                return result
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        # Exponential with "equal jitter": half fixed, half random, so retries spread out.
        base: float = self.backoff_seconds * 2.0 ** (attempt - 1)
        return base / 2 + base / 2 * self._jitter()

    def _count(self, category: str, target: str) -> None:
        instruments().model_errors.add(1, {"category": category, "agent": target})
        tracing.current_span_attribute(tracing.ERROR_CATEGORY, category)


def _inject_faults(target: str) -> None:
    if faults.active("model_timeout", target):
        raise TimeoutError(f"injected model_timeout for {target}")
    if faults.active("model_unavailable", target):
        raise ConnectionError(f"injected model_unavailable for {target}")


def guard_for(model: BaseChatModel, settings: Any | None = None) -> ModelGuard:
    """The guard for a model: retries and breaker from settings (defaults without)."""
    attrs = tracing.model_attributes(model)
    name = f"{attrs.get(tracing.SYSTEM, 'model')}/{attrs.get(tracing.MODEL, 'unknown')}"
    if settings is None:
        return ModelGuard(name)
    return ModelGuard(
        name,
        retries=settings.model_max_retries,
        backoff_seconds=settings.model_retry_backoff_seconds,
        circuit=breaker(
            f"model:{name}",
            failure_threshold=settings.breaker_failure_threshold,
            reset_seconds=settings.breaker_reset_seconds,
        ),
    )

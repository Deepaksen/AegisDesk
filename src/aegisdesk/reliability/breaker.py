"""A small circuit breaker (Milestone 11).

When a dependency is down, every call waits for its timeout before failing:
slow for the user, and more load on a system that is trying to recover. A
circuit breaker counts consecutive failures; after `failure_threshold` it
*opens* and calls fail immediately for `reset_seconds`. Then it lets one trial
call through (*half-open*): success closes it, failure opens it again.

    closed --N failures--> open --reset_seconds--> half_open --ok--> closed
                             ^                         |
                             +-------- failure --------+

Breakers are per dependency (a model, an MCP server) and per process, shared
by every request in it. `reset_breakers()` exists for tests and evaluations.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum

from aegisdesk.observability.metrics import instruments


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or reset_seconds <= 0:
            raise ValueError("failure_threshold must be >= 1 and reset_seconds > 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state()

    def _state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.reset_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def retry_after(self) -> float:
        """Seconds until a trial call will be allowed (0 when closed or half-open)."""
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self.reset_seconds - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        """May a call go ahead now? In half-open state, only one trial at a time."""
        with self._lock:
            state = self._state()
            if state is BreakerState.CLOSED:
                return True
            if state is BreakerState.HALF_OPEN and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            was_open = self._opened_at is not None
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False
        if was_open:
            self._transition(BreakerState.CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            trial_failed = self._trial_in_flight
            self._trial_in_flight = False
            opens = trial_failed or (
                self._opened_at is None and self._failures >= self.failure_threshold
            )
            if opens:
                self._opened_at = self._clock()
        if opens:
            self._transition(BreakerState.OPEN)

    def _transition(self, state: BreakerState) -> None:
        instruments().circuit_transitions.add(1, {"circuit": self.name, "state": state.value})


_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def breaker(
    name: str, *, failure_threshold: int = 5, reset_seconds: float = 30.0
) -> CircuitBreaker:
    """The process-wide breaker for a dependency (created on first use)."""
    with _registry_lock:
        found = _registry.get(name)
        if found is None:
            found = CircuitBreaker(
                name, failure_threshold=failure_threshold, reset_seconds=reset_seconds
            )
            _registry[name] = found
        return found


def breaker_states() -> dict[str, str]:
    with _registry_lock:
        return {name: b.state.value for name, b in sorted(_registry.items())}


def reset_breakers() -> None:
    with _registry_lock:
        _registry.clear()

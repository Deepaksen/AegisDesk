"""Typed failures of backing systems, so callers can answer "unavailable", not "bug"."""

from __future__ import annotations


class StoreUnavailableError(RuntimeError):
    """A data store (access data, checkpoints, idempotency records) cannot be reached.

    Raised instead of driver-specific errors, so the API can answer 503 with
    Retry-After and tools can report `unavailable` without leaking driver text.
    """

    def __init__(self, store: str, message: str | None = None) -> None:
        super().__init__(message or f"The {store} store is temporarily unavailable.")
        self.store = store


def is_database_outage(exc: BaseException) -> bool:
    """Connection-level database errors (not constraint violations or bugs)."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    modules = {cls.__module__.split(".")[0] for cls in type(exc).__mro__}
    return "OperationalError" in names and bool(modules & {"sqlalchemy", "psycopg", "sqlite3"})

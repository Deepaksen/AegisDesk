"""Idempotent replay of duplicate requests (Milestone 11).

Networks fail after the server did the work: the client times out, retries,
and without protection the request runs twice. M1 made the *writes* safe
(idempotent tools); this makes the whole *request* safe: a retry with the same
`Idempotency-Key` gets the stored response of the first attempt, without
running the model again or adding the message to the conversation twice.

    begin(user, key, fingerprint)
      NEW          nobody used this key: run the request, then complete() or release()
      REPLAY       finished before: return the stored response
      IN_PROGRESS  the first attempt is still running: 409, retry later
      MISMATCH     the key was used for a different request: 422

Keys are the client's, so they are scoped per user. The fingerprint (thread +
text) catches a key reused for a different message. Records expire after
`ttl`; an attempt that never finished (a crashed worker) can be taken over
after `stale_after`. Only successful responses are stored: a failed attempt
releases the key so that the retry really runs.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from aegisdesk.reliability.errors import StoreUnavailableError


class BeginResult(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class Begin:
    result: BeginResult
    response: dict[str, Any] | None = None


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class IdempotencyStore(Protocol):
    def begin(self, user_id: str, key: str, fingerprint: str) -> Begin: ...

    def complete(self, user_id: str, key: str, response: dict[str, Any]) -> None: ...

    def release(self, user_id: str, key: str) -> None: ...


@dataclass
class _Record:
    fingerprint: str
    completed: bool
    response: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class InMemoryIdempotencyStore:
    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(hours=24),
        stale_after: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._records: dict[tuple[str, str], _Record] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._stale_after = stale_after
        self._clock = clock

    def begin(self, user_id: str, key: str, fingerprint: str) -> Begin:
        now = self._clock()
        with self._lock:
            record = self._records.get((user_id, key))
            if record is None or now - record.created_at > self._ttl:
                self._records[(user_id, key)] = _Record(fingerprint, False, None, now, now)
                return Begin(BeginResult.NEW)
            if record.fingerprint != fingerprint:
                return Begin(BeginResult.MISMATCH)
            if record.completed:
                return Begin(BeginResult.REPLAY, record.response)
            if now - record.updated_at > self._stale_after:
                record.updated_at = now  # the first attempt died: take it over
                return Begin(BeginResult.NEW)
            return Begin(BeginResult.IN_PROGRESS)

    def complete(self, user_id: str, key: str, response: dict[str, Any]) -> None:
        with self._lock:
            record = self._records.get((user_id, key))
            if record is not None:
                record.completed, record.response = True, response
                record.updated_at = self._clock()

    def release(self, user_id: str, key: str) -> None:
        with self._lock:
            record = self._records.get((user_id, key))
            if record is not None and not record.completed:
                del self._records[(user_id, key)]


_INSERT = text(
    """
    INSERT INTO idempotency_records
        (user_id, idem_key, fingerprint, status, created_at, updated_at)
    VALUES (:user_id, :key, :fingerprint, 'in_progress', now(), now())
    ON CONFLICT (user_id, idem_key) DO NOTHING
    RETURNING idem_key
    """
)
_SELECT = text(
    """
    SELECT fingerprint, status, response,
           created_at < now() - make_interval(secs => :ttl) AS expired,
           updated_at < now() - make_interval(secs => :stale) AS stale
    FROM idempotency_records WHERE user_id = :user_id AND idem_key = :key
    FOR UPDATE
    """
)
_RESTART = text(
    """
    UPDATE idempotency_records
    SET fingerprint = :fingerprint, status = 'in_progress', response = NULL,
        created_at = now(), updated_at = now()
    WHERE user_id = :user_id AND idem_key = :key
    """
)
_TAKE_OVER = text(
    "UPDATE idempotency_records SET updated_at = now() WHERE user_id = :user_id AND idem_key = :key"
)
_COMPLETE = text(
    """
    UPDATE idempotency_records
    SET status = 'completed', response = CAST(:response AS jsonb), updated_at = now()
    WHERE user_id = :user_id AND idem_key = :key
    """
)
_RELEASE = text(
    """
    DELETE FROM idempotency_records
    WHERE user_id = :user_id AND idem_key = :key AND status = 'in_progress'
    """
)


class PgIdempotencyStore:
    """PostgreSQL implementation: correct across API replicas (row lock per key)."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        ttl: timedelta = timedelta(hours=24),
        stale_after: timedelta = timedelta(minutes=5),
    ) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("database_url or engine is required")
            engine = create_engine(database_url, pool_pre_ping=True)
        self._engine = engine
        self._ttl = ttl.total_seconds()
        self._stale = stale_after.total_seconds()

    def begin(self, user_id: str, key: str, fingerprint: str) -> Begin:
        ids = {"user_id": user_id, "key": key}
        try:
            with self._engine.begin() as conn:
                if conn.execute(_INSERT, {**ids, "fingerprint": fingerprint}).first():
                    return Begin(BeginResult.NEW)
                row = conn.execute(_SELECT, {**ids, "ttl": self._ttl, "stale": self._stale}).one()
                if row.expired:
                    conn.execute(_RESTART, {**ids, "fingerprint": fingerprint})
                    return Begin(BeginResult.NEW)
                if row.fingerprint != fingerprint:
                    return Begin(BeginResult.MISMATCH)
                if row.status == "completed":
                    return Begin(BeginResult.REPLAY, row.response)
                if row.stale:
                    conn.execute(_TAKE_OVER, ids)
                    return Begin(BeginResult.NEW)
                return Begin(BeginResult.IN_PROGRESS)
        except SQLAlchemyError as exc:
            raise StoreUnavailableError("idempotency") from exc

    def complete(self, user_id: str, key: str, response: dict[str, Any]) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    _COMPLETE, {"user_id": user_id, "key": key, "response": json.dumps(response)}
                )
        except SQLAlchemyError as exc:
            raise StoreUnavailableError("idempotency") from exc

    def release(self, user_id: str, key: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(_RELEASE, {"user_id": user_id, "key": key})
        except SQLAlchemyError as exc:
            raise StoreUnavailableError("idempotency") from exc

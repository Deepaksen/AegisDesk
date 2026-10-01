"""Milestone 11: the idempotency store contract, memory and PostgreSQL.

The PostgreSQL case needs AEGIS_TEST_DATABASE_URL (a database migrated to 0004).
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest

from aegisdesk.persistence.idempotency import (
    BeginResult,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    PgIdempotencyStore,
)

TEST_DB_URL = os.environ.get("AEGIS_TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
def make_store(request: pytest.FixtureRequest) -> Callable[..., IdempotencyStore]:
    if request.param == "memory":
        return InMemoryIdempotencyStore
    if not TEST_DB_URL:
        pytest.skip("AEGIS_TEST_DATABASE_URL not set")
    return lambda **kwargs: PgIdempotencyStore(TEST_DB_URL, **kwargs)


def _key() -> str:
    return f"k-{uuid.uuid4()}"  # rows from earlier runs never collide


def test_contract(make_store: Callable[..., IdempotencyStore]) -> None:
    store, key = make_store(), _key()

    assert store.begin("E1004", key, "fp").result is BeginResult.NEW
    assert store.begin("E1004", key, "fp").result is BeginResult.IN_PROGRESS
    assert store.begin("E1004", key, "other").result is BeginResult.MISMATCH
    store.complete("E1004", key, {"answer": "done", "references": ["INC-1008"]})

    replay = store.begin("E1004", key, "fp")
    assert replay.result is BeginResult.REPLAY
    assert replay.response == {"answer": "done", "references": ["INC-1008"]}
    assert store.begin("E1001", key, "fp").result is BeginResult.NEW  # per-user keys


def test_release_lets_the_retry_run(make_store: Callable[..., IdempotencyStore]) -> None:
    store, key = make_store(), _key()
    store.begin("E1004", key, "fp")
    store.release("E1004", key)
    assert store.begin("E1004", key, "fp").result is BeginResult.NEW


def test_stale_attempts_are_taken_over(make_store: Callable[..., IdempotencyStore]) -> None:
    store, key = make_store(stale_after=timedelta(seconds=0)), _key()
    store.begin("E1004", key, "fp")
    assert store.begin("E1004", key, "fp").result is BeginResult.NEW


def test_concurrent_duplicates_get_exactly_one_new(
    make_store: Callable[..., IdempotencyStore],
) -> None:
    store, key = make_store(), _key()
    results: list[BeginResult] = []
    lock = threading.Lock()

    def attempt() -> None:
        outcome = store.begin("E1004", key, "fp").result
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(BeginResult.NEW) == 1
    assert results.count(BeginResult.IN_PROGRESS) == 7

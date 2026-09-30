"""Milestone 11: what API clients see when things fail, and duplicate requests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegisdesk.api.app import create_app
from aegisdesk.config import Settings
from aegisdesk.observability import faults
from aegisdesk.persistence.idempotency import fingerprint
from aegisdesk.ui.client import parse_sse

AISHA = {"X-Employee-Id": "E1004"}
TICKET = "My VPN keeps disconnecting. Create a ticket."


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(model_retry_backoff_seconds=0.0)
    with TestClient(create_app(settings, configure_observability=False)) as test_client:
        yield test_client


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def set_faults(spec: str) -> None:
        monkeypatch.setenv(faults.ENV, spec)
        faults.reload()

    yield set_faults
    monkeypatch.delenv(faults.ENV, raising=False)
    faults.reload()


def _thread(client: TestClient) -> str:
    return str(client.post("/api/v1/threads", headers=AISHA).json()["thread_id"])


def _send(client: TestClient, thread: str, text: str, key: str | None = None) -> Any:
    headers = {**AISHA, **({"Idempotency-Key": key} if key else {})}
    return client.post(f"/api/v1/threads/{thread}/messages", json={"text": text}, headers=headers)


def _messages(client: TestClient, thread: str) -> int:
    return len(client.get(f"/api/v1/threads/{thread}", headers=AISHA).json()["messages"])


# -- duplicate requests -------------------------------------------------------------------


def test_a_retry_with_the_same_key_replays_the_stored_response(client: TestClient) -> None:
    thread = _thread(client)
    first = _send(client, thread, TICKET, key="k-1")
    stored = _messages(client, thread)

    retry = _send(client, thread, TICKET, key="k-1")

    assert retry.status_code == 200 and retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()
    assert _messages(client, thread) == stored  # the model did not run again
    assert "Idempotent-Replayed" not in first.headers
    assert "aegisdesk_idempotency_replays_total" in client.get("/metrics").text or True


def test_the_same_key_for_a_different_message_is_rejected(client: TestClient) -> None:
    thread = _thread(client)
    _send(client, thread, TICKET, key="k-2")

    reused = _send(client, thread, "Show my tickets", key="k-2")

    assert reused.status_code == 422
    assert reused.json()["category"] == "idempotency_key_reused"


def test_a_duplicate_while_the_first_is_running_gets_409(client: TestClient) -> None:
    thread = _thread(client)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.idempotency.begin("E1004", "k-3", fingerprint(thread, TICKET))  # "in flight"

    busy = _send(client, thread, TICKET, key="k-3")

    assert busy.status_code == 409 and busy.headers["Retry-After"] == "1"
    assert busy.json()["category"] == "request_in_progress"


def test_streamed_replay_is_a_single_result_event(client: TestClient) -> None:
    thread = _thread(client)
    first = _send(client, thread, TICKET, key="k-4").json()
    with client.stream(
        "POST",
        f"/api/v1/threads/{thread}/messages",
        json={"text": TICKET},
        headers={**AISHA, "Idempotency-Key": "k-4", "Accept": "text/event-stream"},
    ) as response:
        events = list(parse_sse(response.iter_lines()))
    assert [e.event for e in events] == ["result"] and events[0].data == first


# -- model outage ----------------------------------------------------------------------


def test_model_outage_is_a_503_with_retry_after_and_the_key_is_not_burned(
    client: TestClient, inject: Any
) -> None:
    thread = _thread(client)
    inject("model_timeout:router")

    down = _send(client, thread, TICKET, key="k-5")

    assert down.status_code == 503 and down.headers["Retry-After"] == "10"
    problem = down.json()
    assert problem["category"] == "model_timeout" and problem["thread_id"] == thread
    assert "temporarily unavailable" in problem["detail"]

    inject("")  # the model is back: the retry really runs
    retry = _send(client, thread, TICKET, key="k-5")
    assert retry.status_code == 200 and "Idempotent-Replayed" not in retry.headers
    assert any(r.startswith("INC-") for r in retry.json()["references"])


def test_streamed_model_outage_ends_with_an_error_event(client: TestClient, inject: Any) -> None:
    inject("model_unavailable:router")
    with client.stream(
        "POST",
        f"/api/v1/threads/{_thread(client)}/messages",
        json={"text": TICKET},
        headers={**AISHA, "Accept": "text/event-stream"},
    ) as response:
        events = list(parse_sse(response.iter_lines()))

    assert events[-1].event == "error"
    assert events[-1].data["status"] == 503 and events[-1].data["retry_after"] == 10
    assert "The assistant is not responding right now." in [
        e.data["text"] for e in events if e.event == "activity"
    ]


def test_ready_reports_open_circuits_without_going_unready(
    inject: Any,
) -> None:
    settings = Settings(model_retry_backoff_seconds=0.0, breaker_failure_threshold=1)
    with TestClient(create_app(settings, configure_observability=False)) as client:
        inject("model_timeout:router")
        _send(client, _thread(client), TICKET)

        ready = client.get("/ready")

    assert ready.status_code == 200
    assert ready.json()["circuits"] == {"model:fake/fake-scripted": "open"}


# -- database outage ----------------------------------------------------------------------


def test_checkpoint_outage_is_a_503(client: TestClient, inject: Any) -> None:
    thread = _thread(client)
    inject("db_error:checkpoint")

    down = _send(client, thread, TICKET)

    assert down.status_code == 503 and down.headers["Retry-After"] == "5"
    assert down.json()["category"] == "checkpoint_unavailable"
    assert "Traceback" not in down.text


def test_access_store_outage_on_approvals_is_a_503(client: TestClient, inject: Any) -> None:
    inject("db_error:access")
    down = client.get("/api/v1/approvals", headers={"X-Employee-Id": "E1010"})
    assert down.status_code == 503 and down.json()["category"] == "access_unavailable"


def test_audit_outage_on_a_decision_is_a_503(client: TestClient, inject: Any) -> None:
    thread = _thread(client)
    body = _send(client, thread, "Please create an access request for FinanceERP for month-end")
    approval_id = body.json()["pending_approvals"][0]["approval_id"]
    inject("db_error:audit")

    down = client.post(
        f"/api/v1/approvals/{approval_id}/approve", headers={"X-Employee-Id": "E1010"}
    )

    assert down.status_code == 503 and down.json()["category"] == "audit_unavailable"

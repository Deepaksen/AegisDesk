"""Milestone 10: the HTTP API, end to end through FastAPI's TestClient (offline model).

Covers spec section 33 (endpoints), section 43 (the workflows a person should
be able to run), and the API's own security and observability properties.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aegisdesk.api.app import create_app
from aegisdesk.config import Settings
from aegisdesk.observability.metrics import prometheus_names
from aegisdesk.observability.setup import configure_telemetry
from aegisdesk.ui.client import parse_sse

AISHA = {"X-Employee-Id": "E1004"}  # finance employee
GRACE = {"X-Employee-Id": "E1010"}  # her manager
PRIYA = {"X-Employee-Id": "E1001"}  # another employee
FINANCE_ERP = "Please create an access request for FinanceERP for month-end reporting"


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    configure_telemetry(service_name="aegisdesk-api-test", span_exporter=exporter, prometheus=True)
    yield exporter
    configure_telemetry(service_name="aegisdesk-test")


@pytest.fixture
def client(spans: InMemorySpanExporter) -> Iterator[TestClient]:
    app = create_app(Settings(), configure_observability=False)
    with TestClient(app) as test_client:
        yield test_client


def _thread(client: TestClient, headers: dict[str, str] = AISHA) -> str:
    response = client.post("/api/v1/threads", headers=headers)
    assert response.status_code == 201
    return str(response.json()["thread_id"])


def _say(
    client: TestClient,
    thread: str,
    text: str,
    headers: dict[str, str] = AISHA,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/threads/{thread}/messages",
        json={"text": text},
        headers={**headers, **(extra or {})},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# -- operations ---------------------------------------------------------------------------


def test_health_ready_and_openapi(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {  # spec section 33
        "/api/v1/threads",
        "/api/v1/threads/{thread_id}/messages",
        "/api/v1/threads/{thread_id}",
        "/api/v1/approvals",
        "/api/v1/approvals/{approval_id}",
        "/api/v1/approvals/{approval_id}/approve",
        "/api/v1/approvals/{approval_id}/reject",
        "/health",
        "/ready",
        "/metrics",
    } <= paths


def test_metrics_are_exposed_without_user_labels(client: TestClient) -> None:
    thread = _thread(client)
    _say(client, thread, "What laptop is assigned to me?")

    text = client.get("/metrics").text

    for name in ("aegisdesk_requests_total", "aegisdesk_http_requests_total"):
        assert name in text and name in prometheus_names()
    assert 'route="/api/v1/threads/{thread_id}/messages"' in text  # template, not the ID
    assert thread not in text and "E1004" not in text


# -- authentication -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Employee-Id": "E9999"}, {"X-Employee-Id": "E1007"}],  # E1007: terminated
)
def test_unauthenticated_requests_are_refused(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/v1/threads", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == "Not authenticated"


def test_identity_in_the_body_is_rejected_not_trusted(client: TestClient) -> None:
    thread = _thread(client)
    response = client.post(
        f"/api/v1/threads/{thread}/messages",
        json={"text": "Show my tickets", "employee_id": "E1010"},
        headers=AISHA,
    )
    assert response.status_code == 422
    assert "employee_id" in response.json()["detail"] and "E1010" not in response.text


# -- employee workflows (spec section 43, steps 1-4) ------------------------------------------


def test_rag_question_returns_citations_and_activity(client: TestClient) -> None:
    result = _say(client, _thread(client), "How do I configure VPN on macOS?")

    assert {"document_id": "DOC-VPN-001", "title": "VPN Troubleshooting Guide"} in result[
        "citations"
    ]
    assert "Searching the knowledge base..." in [a["text"] for a in result["activity"]]
    assert result["trace_id"] and result["request_id"]


def test_laptop_and_ticket(client: TestClient) -> None:
    thread = _thread(client)
    assert "NS-LT-0101" in _say(client, thread, "What laptop is assigned to me?")["answer"]

    ticket = _say(client, thread, "My VPN keeps disconnecting. Create a ticket.")

    [reference] = [r for r in ticket["references"] if r.startswith("INC-")]
    assert f"Ticket {reference} created." in [a["text"] for a in ticket["activity"]]


def test_idempotency_key_makes_a_retry_write_once(client: TestClient) -> None:
    thread = _thread(client)
    key = {"Idempotency-Key": "retry-7f3a"}
    first = _say(client, thread, "My VPN keeps disconnecting. Create a ticket.", extra=key)
    second = _say(client, thread, "My VPN keeps disconnecting. Create a ticket.", extra=key)

    assert first["request_id"] == second["request_id"]
    tickets = [r for r in first["references"] + second["references"] if r.startswith("INC-")]
    assert len(set(tickets)) == 1


def test_threads_are_private(client: TestClient) -> None:
    thread = _thread(client)
    _say(client, thread, "What laptop is assigned to me?")

    assert client.get(f"/api/v1/threads/{thread}", headers=PRIYA).status_code == 404
    denied = client.post(f"/api/v1/threads/{thread}/messages", json={"text": "hi"}, headers=PRIYA)
    assert denied.status_code == 404
    assert client.get("/api/v1/threads/does-not-exist", headers=AISHA).status_code == 404


def test_thread_view_shows_no_tool_calls_or_raw_results(client: TestClient) -> None:
    thread = _thread(client)
    _say(client, thread, "What laptop is assigned to me?")

    view = client.get(f"/api/v1/threads/{thread}", headers=AISHA).json()

    assert [m["role"] for m in view["messages"]] == ["user", "assistant"]
    assert "tool_calls" not in json.dumps(view) and "get_my_assets" not in json.dumps(view)


# -- approval workflow (spec section 43, steps 5-12) --------------------------------------------


def test_sensitive_access_pauses_then_manager_approval_resumes(client: TestClient) -> None:
    thread = _thread(client)
    paused = _say(client, thread, FINANCE_ERP)

    [pending] = paused["pending_approvals"]
    assert pending["step"] == "manager" and pending["approver"] == "E1010"
    assert {"text": "Manager approval is required.", "status": "waiting"} in paused["activity"]
    approval_id = pending["approval_id"]

    # The requester can see the approval but cannot decide it; strangers cannot see it.
    assert client.get(f"/api/v1/approvals/{approval_id}", headers=AISHA).status_code == 200
    own = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=AISHA)
    assert (own.status_code, own.json()["category"]) == (403, "cannot_approve_own_request")
    assert client.get(f"/api/v1/approvals/{approval_id}", headers=PRIYA).status_code == 404

    listed = client.get("/api/v1/approvals", headers=GRACE).json()
    assert [a["approval_id"] for a in listed] == [approval_id]
    assert listed[0]["application_name"] == "FinanceERP"

    decided = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"comment": "Month-end close"},
        headers=GRACE,
    ).json()

    assert decided["changed"] and decided["request_status"] == "approved"
    assert decided["approval"]["comment"] == "Month-end close"
    resumed = decided["resumed"]
    assert "Access has been granted" in resumed["answer"]
    assert resumed["thread_id"] == thread and resumed["pending_approvals"] == []
    assert [a["text"] for a in resumed["activity"]] == ["Approved access provisioned."]

    # The employee's thread shows the resumed answer.
    view = client.get(f"/api/v1/threads/{thread}", headers=AISHA).json()
    assert "Access has been granted" in view["messages"][-1]["text"]
    assert view["pending_approvals"] == []

    # Deciding again changes nothing; the audit trail has the decision and the grant.
    again = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=GRACE).json()
    assert again["changed"] is False and again["resumed"] is None
    events = client.get("/api/v1/audit", headers=AISHA).json()["events"]
    assert any(e["approval_id"] == approval_id and e["approver_id"] == "E1010" for e in events)
    assert any(e["tool"] == "provision_access" and e["outcome"] == "ok" for e in events)
    assert {e["thread_id"] for e in events if e["approval_id"] == approval_id} == {thread}


def test_rejection_resumes_and_reports_it(client: TestClient) -> None:
    thread = _thread(client)
    approval_id = _say(client, thread, FINANCE_ERP)["pending_approvals"][0]["approval_id"]

    decided = client.post(
        f"/api/v1/approvals/{approval_id}/reject",
        json={"comment": "Use the monthly reports"},
        headers=GRACE,
    ).json()

    assert decided["request_status"] == "rejected"
    assert "Use the monthly reports" in decided["resumed"]["answer"]
    conflict = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=GRACE)
    assert conflict.status_code == 409


def test_audit_is_scoped_to_the_caller(client: TestClient) -> None:
    _say(client, _thread(client), "Show my tickets")
    _say(client, _thread(client, PRIYA), "Show my tickets", PRIYA)

    mine = client.get("/api/v1/audit", headers=AISHA).json()["events"]
    everyone = client.get("/api/v1/audit", headers={"X-Employee-Id": "E1006"}).json()["events"]

    assert mine and {e["user_id"] for e in mine} == {"E1004"}
    assert {"E1004", "E1001"} <= {e["user_id"] for e in everyone}  # IT admin: auditor


# -- streaming ----------------------------------------------------------------------------


def test_streaming_sends_activity_then_the_result(client: TestClient) -> None:
    thread = _thread(client)
    with client.stream(
        "POST",
        f"/api/v1/threads/{thread}/messages",
        json={"text": FINANCE_ERP},
        headers={**AISHA, "Accept": "text/event-stream"},
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = list(parse_sse(response.iter_lines()))

    kinds = [e.event for e in events]
    assert kinds[-1] == "result" and kinds.count("result") == 1
    activity = [e.data["text"] for e in events if e.event == "activity"]
    assert activity[0].startswith("Routing your request to")
    assert "Manager approval is required." in activity
    result = events[-1].data
    assert set(result) == set(_say(client, _thread(client), "Show my tickets"))
    assert result["pending_approvals"] and result["thread_id"] == thread


def test_activity_never_echoes_model_chosen_text(client: TestClient) -> None:
    # The access agent looks up whatever application name the text contains; an
    # unknown name must not be reflected back in activity lines.
    marker = "sk-ant-api03-ACTIVITYLEAK0000000000"
    result = _say(client, _thread(client), f"Request access to {marker} please")
    assert marker not in json.dumps(result["activity"])


# -- observability -------------------------------------------------------------------------


def test_agent_request_joins_the_http_trace(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    thread = _thread(client)
    spans.clear()

    response = client.post(
        f"/api/v1/threads/{thread}/messages", json={"text": "Show my tickets"}, headers=AISHA
    )

    finished = spans.get_finished_spans()
    request = next(s for s in finished if s.name == "aegisdesk.request")
    # Our middleware's server span (FastAPI also emits spans of its own, one with
    # the same name, through OpenTelemetry's global provider).
    http = next(
        s
        for s in finished
        if s.name == "POST /api/v1/threads/{thread_id}/messages"
        and s.instrumentation_scope is not None
        and s.instrumentation_scope.name == "aegisdesk"
    )
    assert request.parent is not None
    assert request.context.trace_id == http.context.trace_id
    assert http.attributes is not None
    assert http.attributes["http.route"] == "/api/v1/threads/{thread_id}/messages"
    assert http.attributes["http.response.status_code"] == 200
    assert response.headers["X-Request-Id"] == response.json()["request_id"]
    assert thread not in http.name


def test_errors_do_not_leak_internals(client: TestClient) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database password is hunter2")

    client.app.state.runtime.send_message = boom  # type: ignore[attr-defined]
    response = client.post(
        f"/api/v1/threads/{_thread(client)}/messages", json={"text": "hi"}, headers=AISHA
    )

    assert response.status_code == 500
    assert "hunter2" not in response.text and response.json()["title"] == "Internal error"
    assert response.json()["trace_id"]

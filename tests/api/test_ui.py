"""Milestone 10: the Streamlit UI and its API client.

`ApiClient` is tested against the real app (TestClient is an httpx client).
The Streamlit script is tested with `AppTest` and a stub client, so the UI
test needs no server and checks only what the UI does with API responses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from aegisdesk.api.app import create_app
from aegisdesk.config import PROJECT_ROOT, Settings
from aegisdesk.ui.client import ApiClient, ApiError, StreamEvent, parse_sse

APP = PROJECT_ROOT / "apps" / "ui" / "streamlit_app.py"


@pytest.fixture
def http() -> Iterator[TestClient]:
    with TestClient(create_app(Settings(), configure_observability=False)) as client:
        yield client


def test_parse_sse_handles_events_and_blank_lines() -> None:
    lines = iter(
        ["event: activity", 'data: {"text": "a"}', "", "event: result", 'data: {"x": 1}', ""]
    )
    assert list(parse_sse(lines)) == [
        StreamEvent("activity", {"text": "a"}),
        StreamEvent("result", {"x": 1}),
    ]


def test_client_runs_the_approval_workflow_over_http(http: TestClient) -> None:
    aisha, grace = ApiClient(http, "E1004"), ApiClient(http, "E1010")
    thread = aisha.new_thread()

    events = list(
        aisha.stream(thread, "Please create an access request for FinanceERP for month-end")
    )
    result = events[-1].data
    [pending] = result["pending_approvals"]
    [listed] = grace.approvals()
    decision = grace.decide(listed["approval_id"], approve=True, comment="ok")

    assert listed["approval_id"] == pending["approval_id"]
    assert "Access has been granted" in decision["resumed"]["answer"]
    assert aisha.me()["name"] == "Aisha Khan"
    assert any(e["tool"] == "provision_access" for e in aisha.audit())


def test_client_turns_problems_into_api_errors(http: TestClient) -> None:
    with pytest.raises(ApiError) as refused:
        ApiClient(http, "E9999").me()
    assert refused.value.status == 401

    with pytest.raises(ApiError) as missing:
        ApiClient(http, "E1010").decide("AP-9999", approve=True, comment=None)
    assert missing.value.status == 404


class StubClient:
    """Canned API responses; records what the UI asked for."""

    decisions: ClassVar[list[tuple[str, bool, str | None]]] = []

    def __init__(self, employee_id: str) -> None:
        self.employee_id = employee_id

    def me(self) -> dict[str, Any]:
        names = {"E1004": "Aisha Khan", "E1010": "Grace Liu"}
        return {
            "employee_id": self.employee_id,
            "name": names.get(self.employee_id, "Someone"),
            "department": "finance",
            "roles": ["employee", "manager"] if self.employee_id == "E1010" else ["employee"],
            "manager_id": None,
        }

    def new_thread(self) -> str:
        return "thread-1"

    def stream(self, thread_id: str, text: str, key: str | None = None) -> Iterator[StreamEvent]:
        yield StreamEvent("activity", {"text": "Routing your request to: access.", "status": "ok"})
        yield StreamEvent(
            "result",
            {
                "answer": "Your request AR-1013 needs manager approval.",
                "request_id": "req-1",
                "trace_id": "abc123",
                "citations": [{"document_id": "DOC-ACC-001", "title": "Access Policy"}],
                "references": ["AR-1013", "AP-0001"],
                "pending_approvals": [
                    {
                        "approval_id": "AP-0001",
                        "access_request_id": "AR-1013",
                        "step": "manager",
                        "approver": "E1010",
                    }
                ],
            },
        )

    def approvals(self) -> list[dict[str, Any]]:
        if self.employee_id != "E1010" or StubClient.decisions:
            return []
        return [
            {
                "approval_id": "AP-0001",
                "application_name": "FinanceERP",
                "requester_name": "Aisha Khan",
                "requester_id": "E1004",
                "step": "manager",
                "expires_at": "2026-10-07T16:00:00Z",
            }
        ]

    def decide(self, approval_id: str, *, approve: bool, comment: str | None) -> dict[str, Any]:
        StubClient.decisions.append((approval_id, approve, comment))
        return {
            "request_status": "approved",
            "note": None,
            "resumed": {
                "answer": "Access has been granted.",
                "request_id": "req-2",
                "trace_id": "def456",
                "citations": [],
                "references": ["AR-1013"],
                "pending_approvals": [],
            },
        }

    def audit(self, request_id: str | None = None) -> list[dict[str, Any]]:
        return [{"tool": "create_access_request", "policy_decision": "allow", "outcome": "ok"}]


def _app(employee_id: str) -> AppTest:
    StubClient.decisions = []
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client_factory"] = StubClient
    at.session_state["employee_id"] = employee_id
    return at.run()


def test_employee_view_shows_answer_citations_references_and_the_pause() -> None:
    at = _app("E1004")
    assert not at.exception
    assert at.header[0].value == "Hello, Aisha Khan"

    at.chat_input[0].set_value("I need FinanceERP access").run()

    assert not at.exception
    markdown = " ".join(m.value for m in at.markdown)
    captions = " ".join(c.value for c in at.caption)
    assert "needs manager approval" in markdown
    assert "DOC-ACC-001 Access Policy" in captions and "`AR-1013`" in captions
    assert "Waiting for manager approval (AP-0001" in at.info[0].value


def test_manager_view_approves_with_a_comment_and_shows_the_resumed_answer() -> None:
    at = _app("E1010")
    assert "FinanceERP for Aisha Khan" in " ".join(m.value for m in at.markdown)

    at.text_input(key="comment-AP-0001").set_value("Month-end close")
    at.button(key="approve-AP-0001").click().run()

    assert not at.exception
    assert StubClient.decisions == [("AP-0001", True, "Month-end close")]
    assert "Approved AP-0001; request is now approved." in at.success[0].value
    assert "Access has been granted." in " ".join(m.value for m in at.markdown)


def test_the_ui_imports_only_the_api_client_from_the_backend() -> None:
    source = Path(APP).read_text()
    backend_imports = [
        line
        for line in source.splitlines()
        if line.startswith(("from aegisdesk", "import aegisdesk"))
    ]
    assert backend_imports == ["from aegisdesk.ui.client import ApiClient, ApiError"]

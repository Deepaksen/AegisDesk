"""Access tools through the ToolExecutor."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.tools.access import build_access_tools
from aegisdesk.tools.base import ToolAccess, ToolRisk
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor


@pytest.fixture
def executor(repository: ServiceDeskRepository) -> ToolExecutor:
    return ToolExecutor(build_access_tools(repository))


def _run(
    executor: ToolExecutor,
    user: UserContext,
    name: str,
    args: dict[str, Any],
    request_id: str = "r1",
) -> tuple[OutcomeStatus, dict[str, Any]]:
    outcome = executor.execute(name, args, user=user, request_id=request_id)
    return outcome.status, json.loads(outcome.content)


def test_catalogue_and_risk(executor: ToolExecutor) -> None:
    create = executor.get("create_access_request")
    assert create is not None
    assert (create.risk, create.access) == (ToolRisk.MEDIUM, ToolAccess.WRITE)
    for definition in executor.model_definitions():
        fields = definition["function"]["parameters"]["properties"]
        assert not any("employee" in f or "user" in f for f in fields)


def test_profile_and_access_are_the_callers_own(executor: ToolExecutor, aisha: UserContext) -> None:
    _, profile = _run(executor, aisha, "get_employee_profile", {})
    assert (profile["employee_id"], profile["manager_name"]) == ("E1004", "Grace Liu")

    _, mine = _run(executor, aisha, "list_my_access", {})
    by_app = {a["application"]: a for a in mine["access"]}
    assert by_app["Jira"]["active"] is True
    assert by_app["AnalyticsHub"]["active"] is False  # expired 2026-06-30
    assert mine["requests"] == []


def test_unknown_application(executor: ToolExecutor, aisha: UserContext) -> None:
    status, body = _run(executor, aisha, "get_application", {"application": "SAP"})
    assert status is OutcomeStatus.ERROR
    assert body["error"]["category"] == "not_found"
    assert "FinanceERP" in body["error"]["message"]  # lists known applications


def test_request_needing_approval_is_recorded_not_granted(
    executor: ToolExecutor, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    status, body = _run(
        executor,
        aisha,
        "create_access_request",
        {"application": "FinanceERP", "justification": "Month-end reporting for my team."},
    )
    assert status is OutcomeStatus.OK
    assert (body["request_id"], body["status"]) == ("AR-1013", "awaiting_approval")
    assert body["approvals_required"] == ["manager"]
    assert "Nothing is granted" in body["next_step"]
    assert not any(a.application_id == "APP-FIN" for a in repository.access_for("E1004"))


def test_ineligible_request_is_refused_whatever_the_agent_thinks(
    executor: ToolExecutor, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    status, body = _run(
        executor,
        aisha,
        "create_access_request",
        {"application": "ProductionDB", "justification": "The eligibility check said yes."},
    )
    assert status is OutcomeStatus.ERROR
    assert body["error"]["category"] == "not_eligible"
    assert repository.access_requests_for("E1004")[-1].request_id == "AR-1009"  # nothing new


def test_retry_is_idempotent_and_a_new_request_is_a_duplicate(
    executor: ToolExecutor, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    args = {"application": "FinanceERP", "justification": "Month-end reporting for my team."}
    _, first = _run(executor, aisha, "create_access_request", args, request_id="req-A")
    _, retry = _run(executor, aisha, "create_access_request", args, request_id="req-A")
    status, again = _run(executor, aisha, "create_access_request", args, request_id="req-B")

    assert retry["request_id"] == first["request_id"] and retry["created"] is False
    assert status is OutcomeStatus.ERROR
    assert "already have an open FinanceERP request" in again["error"]["message"]


def test_auto_approved_standard_application(
    executor: ToolExecutor, repository: ServiceDeskRepository
) -> None:
    marcus = authenticate(repository, "E1002")
    _, body = _run(
        executor,
        marcus,
        "create_access_request",
        {"application": "Confluence", "justification": "Need the team wiki."},
    )
    assert body["status"] == "auto_approved" and body["approvals_required"] == []

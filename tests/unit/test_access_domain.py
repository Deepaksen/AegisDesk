"""Eligibility rules and access data (pure, deterministic)."""

from __future__ import annotations

import pytest

from aegisdesk.domain.access import (
    AccessRequestStatus,
    Approval,
    IneligibleReason,
    evaluate_eligibility,
)
from aegisdesk.domain.models import EmployeeStatus
from aegisdesk.domain.repository import ServiceDeskRepository


def _check(repository: ServiceDeskRepository, employee_id: str, app_name: str):  # type: ignore[no-untyped-def]
    employee = repository.get_employee(employee_id)
    app = repository.find_application(app_name)
    assert employee is not None and app is not None
    return evaluate_eligibility(
        employee,
        app,
        repository.access_for(employee_id),
        repository.access_requests_for(employee_id),
        repository.today(),
    )


@pytest.mark.parametrize(
    ("employee_id", "app", "approvals"),
    [
        ("E1004", "FinanceERP", [Approval.MANAGER]),  # finance employee, manager approval
        ("E1004", "AnalyticsHub", [Approval.DATA_OWNER]),  # previous access has expired
        ("E1002", "Confluence", []),  # standard app, no approval
        ("E1005", "GitHub", [Approval.MANAGER]),  # contractor: sponsor approval added
        ("E1006", "ProductionDB", [Approval.MANAGER, Approval.SECURITY]),  # privileged
    ],
)
def test_eligible_requests_and_their_approvals(
    repository: ServiceDeskRepository, employee_id: str, app: str, approvals: list[Approval]
) -> None:
    result = _check(repository, employee_id, app)
    assert result.eligible, result.explanation
    assert result.approvals_required == approvals


@pytest.mark.parametrize(
    ("employee_id", "app", "reason"),
    [
        ("E1001", "FinanceERP", IneligibleReason.NOT_IN_ALLOWED_GROUP),  # engineer
        ("E1004", "ProductionDB", IneligibleReason.NOT_IN_ALLOWED_GROUP),
        ("E1004", "HRAdmin", IneligibleReason.NOT_IN_ALLOWED_GROUP),
        ("E1005", "Salesforce", IneligibleReason.CONTRACTORS_NOT_ALLOWED),
        ("E1001", "GitHub", IneligibleReason.ALREADY_HAS_ACCESS),
        ("E1012", "ProductionDB", IneligibleReason.REQUEST_ALREADY_OPEN),  # AR-1007 pending
    ],
)
def test_ineligible_requests(
    repository: ServiceDeskRepository, employee_id: str, app: str, reason: IneligibleReason
) -> None:
    result = _check(repository, employee_id, app)
    assert not result.eligible
    assert result.reason is reason
    assert result.approvals_required == []


def test_inactive_employee_is_never_eligible(repository: ServiceDeskRepository) -> None:
    employee = repository.get_employee("E1007")
    app = repository.find_application("Jira")
    assert employee is not None and employee.status is EmployeeStatus.TERMINATED and app
    result = evaluate_eligibility(employee, app, [], [], repository.today())
    assert result.reason is IneligibleReason.INACTIVE_EMPLOYEE


def test_expired_access_is_not_active(repository: ServiceDeskRepository) -> None:
    by_app = {a.application_id: a for a in repository.access_for("E1006")}
    assert not by_app["APP-PDB"].active_on(repository.today().replace(day=30))
    assert by_app["APP-JIRA"].active_on(repository.today())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FinanceERP", "APP-FIN"),
        ("app-fin", "APP-FIN"),
        ("I need financeerp for month-end", "APP-FIN"),
        ("jira and github", None),  # ambiguous
        ("SAP", None),
    ],
)
def test_find_application(
    repository: ServiceDeskRepository, text: str, expected: str | None
) -> None:
    app = repository.find_application(text)
    assert (app.application_id if app else None) == expected


def test_access_request_ids_and_status(repository: ServiceDeskRepository) -> None:
    request, created = repository.create_access_request(
        employee_id="E1004",
        application_id="APP-FIN",
        approvals_required=[Approval.MANAGER],
        justification="Month-end reporting.",
        idempotency_key="k",
    )
    assert created and request.request_id == "AR-1013"
    assert request.status is AccessRequestStatus.AWAITING_APPROVAL
    auto, _ = repository.create_access_request(
        employee_id="E1002",
        application_id="APP-CONF",
        approvals_required=[],
        justification="Team wiki.",
        idempotency_key="k2",
    )
    assert auto.status is AccessRequestStatus.AUTO_APPROVED

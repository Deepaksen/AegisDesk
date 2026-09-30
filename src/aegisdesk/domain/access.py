"""Application access: records and the eligibility rules.

Eligibility is a pure function of trusted data (who the employee is, what the
application allows, what access and requests already exist). The Access
agent can *ask* for it, but `create_access_request` recomputes it itself, so
what the agent believes about eligibility never matters.

Approval is not decided here. This module only says which approvals a request
needs. Collecting them is the approval workflow (Milestone 7), and policy
enforcement moves to OPA in Milestone 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

from aegisdesk.domain.models import Employee, EmployeeStatus


class Approval(StrEnum):
    MANAGER = "manager"
    DATA_OWNER = "data_owner"
    SECURITY = "security"


class Application(BaseModel):
    application_id: str
    name: str
    description: str
    owner: str
    # Empty lists mean "no restriction of that kind".
    allowed_departments: list[str]
    allowed_roles: list[str]
    contractors_allowed: bool
    approvals: list[Approval]
    privileged: bool


class AccessRecord(BaseModel):
    employee_id: str
    application_id: str
    role: str
    granted_on: date
    expires_on: date | None

    def active_on(self, today: date) -> bool:
        return self.expires_on is None or self.expires_on >= today


class AccessRequestStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    AUTO_APPROVED = "auto_approved"  # standard application: no approval needed
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def is_open(self) -> bool:
        return self is AccessRequestStatus.AWAITING_APPROVAL


class AccessRequest(BaseModel):
    request_id: str
    employee_id: str
    application_id: str
    status: AccessRequestStatus
    approvals_required: list[Approval]
    created_at: datetime
    justification: str


class IneligibleReason(StrEnum):
    INACTIVE_EMPLOYEE = "inactive_employee"
    ALREADY_HAS_ACCESS = "already_has_access"
    REQUEST_ALREADY_OPEN = "request_already_open"
    CONTRACTORS_NOT_ALLOWED = "contractors_not_allowed"
    NOT_IN_ALLOWED_GROUP = "not_in_allowed_department_or_role"


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: IneligibleReason | None
    explanation: str
    approvals_required: list[Approval] = field(default_factory=list)


def evaluate_eligibility(
    employee: Employee,
    application: Application,
    access: list[AccessRecord],
    requests: list[AccessRequest],
    today: date,
) -> Eligibility:
    """Decide whether `employee` may request `application`, and what approvals it needs."""

    def no(reason: IneligibleReason, explanation: str) -> Eligibility:
        return Eligibility(False, reason, explanation)

    app = application
    if employee.status is not EmployeeStatus.ACTIVE:
        return no(IneligibleReason.INACTIVE_EMPLOYEE, "Only active employees can request access.")

    mine = [a for a in access if a.application_id == app.application_id]
    if any(a.active_on(today) for a in mine):
        return no(IneligibleReason.ALREADY_HAS_ACCESS, f"You already have {app.name} access.")

    if any(r.application_id == app.application_id and r.status.is_open for r in requests):
        return no(
            IneligibleReason.REQUEST_ALREADY_OPEN,
            f"You already have an open {app.name} request awaiting approval.",
        )

    is_contractor = "contractor" in employee.roles
    if is_contractor and not app.contractors_allowed:
        return no(
            IneligibleReason.CONTRACTORS_NOT_ALLOWED,
            f"{app.name} is not available to contractors.",
        )

    restricted = bool(app.allowed_departments or app.allowed_roles)
    in_group = employee.department in app.allowed_departments or bool(
        set(employee.roles) & set(app.allowed_roles)
    )
    # Contractors may use contractor-friendly apps outside their department only with
    # their sponsor's (manager's) approval; that is added below.
    if restricted and not in_group and not (is_contractor and app.contractors_allowed):
        groups = ", ".join(app.allowed_departments + app.allowed_roles)
        return no(
            IneligibleReason.NOT_IN_ALLOWED_GROUP,
            f"{app.name} is limited to: {groups}.",
        )

    approvals = list(app.approvals)
    if is_contractor and Approval.MANAGER not in approvals and restricted:
        approvals.insert(0, Approval.MANAGER)  # sponsor approval for contractors

    explanation = (
        f"You can request {app.name}. Approval needed from: "
        + ", ".join(a.value.replace("_", " ") for a in approvals)
        + "."
        if approvals
        else f"You can request {app.name}; it is granted without approval."
    )
    return Eligibility(True, None, explanation, approvals)

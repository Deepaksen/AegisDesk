"""Access-management tools for the Access agent.

As with every tool (ADR 0003), none of them takes an employee ID: "my
profile", "my access" and "request access for me" always mean the signed-in
user from `ToolCallContext`.

`create_access_request` does not trust the agent's eligibility check. It
recomputes eligibility itself and refuses ineligible requests. It never
grants access: it records a request that is either auto-approved (standard
applications) or awaiting approval (collected by the approval workflow in
Milestone 7).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aegisdesk.domain.access import (
    AccessRequestStatus,
    Application,
    Approval,
    Eligibility,
    evaluate_eligibility,
)
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.tools.base import (
    NotFoundError,
    ToolAccess,
    ToolCallContext,
    ToolError,
    ToolRisk,
    ToolSpec,
)

OWNER = "identity-and-access-management"


class NotEligibleError(ToolError):
    category = "not_eligible"


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationInput(_StrictInput):
    application: str = Field(
        min_length=2, max_length=100, description="Application name or ID, e.g. FinanceERP."
    )


class EmptyInput(_StrictInput):
    pass


class EmployeeProfileOutput(BaseModel):
    employee_id: str
    name: str
    department: str
    title: str
    roles: list[str]
    manager_id: str | None
    manager_name: str | None


class AccessEntry(BaseModel):
    application_id: str
    application: str
    role: str
    granted_on: date
    expires_on: date | None
    active: bool


class OpenRequest(BaseModel):
    request_id: str
    application: str
    status: AccessRequestStatus
    approvals_required: list[Approval]


class MyAccessOutput(BaseModel):
    access: list[AccessEntry]
    requests: list[OpenRequest]


class ApplicationOutput(BaseModel):
    application_id: str
    name: str
    description: str
    owner: str
    available_to_departments: list[str]
    available_to_roles: list[str]
    contractors_allowed: bool
    approvals_required: list[Approval]
    privileged: bool


class EligibilityOutput(BaseModel):
    application: str
    eligible: bool
    reason: str | None
    explanation: str
    approvals_required: list[Approval]


class CreateAccessRequestInput(ApplicationInput):
    justification: str = Field(
        min_length=10, max_length=500, description="Why the employee needs this access."
    )


class CreateAccessRequestOutput(BaseModel):
    request_id: str
    application: str
    status: AccessRequestStatus
    approvals_required: list[Approval]
    created_at: datetime
    created: bool = Field(description="False if this exact request had already been recorded.")
    next_step: str


def build_access_tools(repository: ServiceDeskRepository) -> list[ToolSpec[Any, Any]]:
    def application(text: str) -> Application:
        app = repository.find_application(text)
        if app is None:
            known = ", ".join(a.name for a in repository.list_applications())
            raise NotFoundError(f"No application matches {text!r}. Known applications: {known}.")
        return app

    def eligibility(app: Application, ctx: ToolCallContext) -> Eligibility:
        employee = repository.get_employee(ctx.user.employee_id)
        if employee is None:  # authenticated users always exist; defensive
            raise NotFoundError("Your employee record was not found.")
        return evaluate_eligibility(
            employee,
            app,
            repository.access_for(employee.employee_id),
            repository.access_requests_for(employee.employee_id),
            repository.today(),
        )

    def get_employee_profile(_: EmptyInput, ctx: ToolCallContext) -> EmployeeProfileOutput:
        me = repository.get_employee(ctx.user.employee_id)
        if me is None:
            raise NotFoundError("Your employee record was not found.")
        manager = repository.get_employee(me.manager_id) if me.manager_id else None
        return EmployeeProfileOutput(
            employee_id=me.employee_id,
            name=me.name,
            department=me.department,
            title=me.title,
            roles=me.roles,
            manager_id=me.manager_id,
            manager_name=manager.name if manager else None,
        )

    def list_my_access(_: EmptyInput, ctx: ToolCallContext) -> MyAccessOutput:
        today = repository.today()
        names = {a.application_id: a.name for a in repository.list_applications()}
        return MyAccessOutput(
            access=[
                AccessEntry(
                    application_id=a.application_id,
                    application=names.get(a.application_id, a.application_id),
                    role=a.role,
                    granted_on=a.granted_on,
                    expires_on=a.expires_on,
                    active=a.active_on(today),
                )
                for a in repository.access_for(ctx.user.employee_id)
            ],
            requests=[
                OpenRequest(
                    request_id=r.request_id,
                    application=names.get(r.application_id, r.application_id),
                    status=r.status,
                    approvals_required=r.approvals_required,
                )
                for r in repository.access_requests_for(ctx.user.employee_id)
                if r.status.is_open
            ],
        )

    def get_application(args: ApplicationInput, _: ToolCallContext) -> ApplicationOutput:
        app = application(args.application)
        return ApplicationOutput(
            application_id=app.application_id,
            name=app.name,
            description=app.description,
            owner=app.owner,
            available_to_departments=app.allowed_departments,
            available_to_roles=app.allowed_roles,
            contractors_allowed=app.contractors_allowed,
            approvals_required=app.approvals,
            privileged=app.privileged,
        )

    def check_access_eligibility(args: ApplicationInput, ctx: ToolCallContext) -> EligibilityOutput:
        app = application(args.application)
        result = eligibility(app, ctx)
        return EligibilityOutput(
            application=app.name,
            eligible=result.eligible,
            reason=result.reason.value if result.reason else None,
            explanation=result.explanation,
            approvals_required=result.approvals_required,
        )

    def create_access_request(
        args: CreateAccessRequestInput, ctx: ToolCallContext
    ) -> CreateAccessRequestOutput:
        if ctx.idempotency_key is None:
            raise RuntimeError("create_access_request requires an idempotency key")
        app = application(args.application)
        # Idempotency first: a retry of a request that was already recorded returns it,
        # instead of failing eligibility with "request already open".
        previous = repository.access_request_for_key(ctx.idempotency_key)
        if previous is None:
            result = eligibility(app, ctx)  # recomputed here; the agent's view is irrelevant
            if not result.eligible:
                raise NotEligibleError(result.explanation)
            approvals = result.approvals_required
        else:
            approvals = previous.approvals_required
        request, created = repository.create_access_request(
            employee_id=ctx.user.employee_id,
            application_id=app.application_id,
            approvals_required=approvals,
            justification=args.justification.strip(),
            idempotency_key=ctx.idempotency_key,
        )
        next_step = (
            "Waiting for approval from: "
            + ", ".join(a.value.replace("_", " ") for a in request.approvals_required)
            + ". Nothing is granted until every approval is recorded."
            if request.status is AccessRequestStatus.AWAITING_APPROVAL
            else "Standard application: access will be provisioned automatically."
        )
        return CreateAccessRequestOutput(
            request_id=request.request_id,
            application=app.name,
            status=request.status,
            approvals_required=request.approvals_required,
            created_at=request.created_at,
            created=created,
            next_step=next_step,
        )

    def read(name: str, description: str, input_model: Any, output_model: Any, handler: Any) -> Any:
        return ToolSpec(
            name=name,
            description=description,
            input_model=input_model,
            output_model=output_model,
            handler=handler,
            risk=ToolRisk.LOW,
            access=ToolAccess.READ,
            idempotent=True,
            owner=OWNER,
        )

    return [
        read(
            "get_employee_profile",
            "Show the signed-in employee's profile: department, title, roles and manager.",
            EmptyInput,
            EmployeeProfileOutput,
            get_employee_profile,
        ),
        read(
            "list_my_access",
            "List the applications the signed-in employee can access (with expiry) and their "
            "open access requests.",
            EmptyInput,
            MyAccessOutput,
            list_my_access,
        ),
        read(
            "get_application",
            "Describe an application (Jira, Confluence, GitHub, Salesforce, FinanceERP, "
            "AnalyticsHub, ProductionDB, HRAdmin): who may use it and which approvals it needs.",
            ApplicationInput,
            ApplicationOutput,
            get_application,
        ),
        read(
            "check_access_eligibility",
            "Check whether the signed-in employee may request access to an application, and "
            "which approvals the request would need.",
            ApplicationInput,
            EligibilityOutput,
            check_access_eligibility,
        ),
        ToolSpec(
            name="create_access_request",
            description=(
                "Create an application access request for the signed-in employee. It is "
                "recorded, not granted: requests needing approval wait for the approvers."
            ),
            input_model=CreateAccessRequestInput,
            output_model=CreateAccessRequestOutput,
            handler=create_access_request,
            risk=ToolRisk.MEDIUM,
            access=ToolAccess.WRITE,
            idempotent=True,  # via the idempotency key
            owner=OWNER,
        ),
    ]

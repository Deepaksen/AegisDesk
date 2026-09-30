"""Provisioning: the one tool that actually grants access (Milestone 7).

`provision_access` is HIGH risk. In `config/policy.yaml` it is granted to no
conversational agent, only to the `access_workflow` identity, which the
approval workflow uses after a human decision. Even then the gateway allows
it only with approval evidence it looked up itself (the request is the
caller's, fully approved or auto-approved, and not yet provisioned).

The handler repeats those checks (defense in depth) and grants at most once:
`AccessStore.provision` is a conditional update, so a retried or resumed
workflow cannot grant twice.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aegisdesk.domain.access import AccessRecord, AccessRequestStatus
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
PROVISION_TOOL = "provision_access"
# Privileged access is time-limited; standard access does not expire.
PRIVILEGED_ACCESS_DAYS = 30
PROVISIONABLE = frozenset({AccessRequestStatus.APPROVED, AccessRequestStatus.AUTO_APPROVED})


class NotApprovedError(ToolError):
    category = "not_approved"


class ProvisionAccessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_request_id: str = Field(pattern=r"^AR-\d{4,}$")


class ProvisionAccessOutput(BaseModel):
    access_request_id: str
    application_id: str
    granted: bool = Field(description="False if this request had already been provisioned.")
    expires_on: date | None


def build_provisioning_tools(repository: ServiceDeskRepository) -> list[ToolSpec[Any, Any]]:
    def provision_access(args: ProvisionAccessInput, ctx: ToolCallContext) -> ProvisionAccessOutput:
        request = repository.access_store.get_access_request(args.access_request_id)
        if request is None or request.employee_id != ctx.user.employee_id:
            raise NotFoundError(f"No access request {args.access_request_id}.")
        if request.status not in PROVISIONABLE:
            raise NotApprovedError(f"{request.request_id} is {request.status.value}.")
        app = repository.get_application(request.application_id)
        today = repository.today()
        expires = today + timedelta(days=PRIVILEGED_ACCESS_DAYS) if app and app.privileged else None
        granted = repository.access_store.provision(
            request.request_id,
            AccessRecord(
                employee_id=request.employee_id,
                application_id=request.application_id,
                role="user",
                granted_on=today,
                expires_on=expires,
            ),
        )
        return ProvisionAccessOutput(
            access_request_id=request.request_id,
            application_id=request.application_id,
            granted=granted,
            expires_on=expires,
        )

    return [
        ToolSpec(
            name=PROVISION_TOOL,
            description="Grant the access of an approved access request. Workflow only.",
            input_model=ProvisionAccessInput,
            output_model=ProvisionAccessOutput,
            handler=provision_access,
            risk=ToolRisk.HIGH,
            access=ToolAccess.WRITE,
            idempotent=True,  # the store grants each request at most once
            owner=OWNER,
        )
    ]

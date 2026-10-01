"""The supervisor's routing decision: which specialists should handle the request.

The model fills this in (structured output); code decides what happens next.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from aegisdesk.tools.handoff import AgentName

AGENT_FIELD_DESCRIPTION = (
    "knowledge: how-to, troubleshooting, policy and documentation questions, e.g. vpn setup, "
    "errors, passwords, security rules, remote working, what a policy says; "
    "service_desk: the employee's own laptop, equipment, assets, their tickets, creating a "
    "ticket, reporting a broken device or incident; "
    "access: application access, permissions, request access, eligibility, approval of "
    "access, jira, confluence, github, salesforce, financeerp, analyticshub, productiondb, hradmin"
)


class RoutedTask(BaseModel):
    agent: AgentName = Field(description=AGENT_FIELD_DESCRIPTION)
    instruction: str = Field(
        min_length=3,
        max_length=400,
        description="What this specialist should do, in the employee's own terms.",
    )


class RoutingPlan(BaseModel):
    """How to split an employee's request between specialists."""

    # "At most 3" is enforced by the supervisor (extra tasks are dropped), not by the
    # schema: rejecting the whole plan for one task too many would be worse.
    tasks: list[RoutedTask] = Field(
        description="One task per distinct part of the request (at most 3), in order."
    )
    out_of_scope: bool = Field(
        description="True if the request has nothing to do with IT, policies or access."
    )

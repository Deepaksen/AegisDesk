"""`request_handoff`: how a specialist asks for another specialist.

A specialist cannot call another agent directly (no "agents talking to
agents"). It can only ask the supervisor, by calling this tool. The tool does
nothing except record the request; the supervisor graph reads it and decides,
deterministically, whether to route (within the handoff budget, never
back to the same agent, never the same task twice).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegisdesk.tools.base import ToolAccess, ToolCallContext, ToolRisk, ToolSpec

HANDOFF_TOOL = "request_handoff"


class AgentName(StrEnum):
    KNOWLEDGE = "knowledge"
    SERVICE_DESK = "service_desk"
    ACCESS = "access"


AGENT_SCOPES = {
    AgentName.KNOWLEDGE: "IT and policy questions answered from Northstar documentation",
    AgentName.SERVICE_DESK: "the employee's equipment and IT tickets, creating tickets",
    AgentName.ACCESS: "application access: current access, eligibility, access requests",
}


class HandoffOutput(BaseModel):
    accepted_for_routing: bool = True
    note: str = "The supervisor will decide whether to route this part of the request."


def build_handoff_tool(current: AgentName) -> ToolSpec[Any, Any]:
    others = [a for a in AgentName if a is not current]

    class HandoffInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        target_agent: AgentName = Field(
            description="; ".join(f"{a.value}: {AGENT_SCOPES[a]}" for a in others),
            json_schema_extra={"enum": [a.value for a in others]},
        )
        instruction: str = Field(
            min_length=5, max_length=400, description="What the other agent should do."
        )

        @field_validator("target_agent")
        @classmethod
        def _not_self(cls, value: AgentName) -> AgentName:
            if value is current:
                raise ValueError("cannot hand off to yourself")
            return value

    def handler(args: HandoffInput, _: ToolCallContext) -> HandoffOutput:
        return HandoffOutput()

    return ToolSpec(
        name=HANDOFF_TOOL,
        description=(
            "Hand part of the employee's request to another specialist when it is outside "
            "your scope. Use only for work you cannot do yourself."
        ),
        input_model=HandoffInput,
        output_model=HandoffOutput,
        handler=handler,
        risk=ToolRisk.LOW,
        access=ToolAccess.READ,
        idempotent=True,
        owner="aegisdesk-platform",
    )

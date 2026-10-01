"""Service Desk tools (Milestone 1: local Python functions).

Note what is *missing* from every input schema: an employee ID. "My assets"
and "my tickets" always mean the authenticated user from `ToolCallContext`.
If the model tries to add `employee_id` (because a user typed "show me
E1002's laptop", or because a document told it to), validation rejects the
call, since the schemas forbid unknown fields.

Tools return *view* models: only the fields the model needs, never the raw
domain records.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from aegisdesk.domain.models import (
    Asset,
    AssetStatus,
    AssetType,
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.tools.base import (
    NotFoundError,
    ToolAccess,
    ToolCallContext,
    ToolRisk,
    ToolSpec,
)

OWNER = "it-service-desk"
TICKET_ID_PATTERN = r"^INC-\d{4,}$"


def _normalise_ticket_id(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


# Metadata order matters: the BeforeValidator wraps the constrained string, so
# " inc-1001 " is normalised before the pattern check and the pattern still
# appears in the JSON Schema the model sees.
TicketId = Annotated[
    str,
    Field(pattern=TICKET_ID_PATTERN, description="Ticket ID such as INC-1001."),
    BeforeValidator(_normalise_ticket_id),
]


class _StrictInput(BaseModel):
    # Unknown fields are an error, not silently ignored: the model cannot
    # smuggle extra parameters (e.g. employee_id) into a tool call.
    model_config = ConfigDict(extra="forbid")


# -- views returned to the model ---------------------------------------------


class AssetView(BaseModel):
    asset_tag: str
    asset_type: AssetType
    model: str
    serial_number: str
    status: AssetStatus
    assigned_on: date | None

    @classmethod
    def of(cls, asset: Asset) -> AssetView:
        return cls.model_validate(asset.model_dump())


class TicketView(BaseModel):
    ticket_id: str
    title: str
    description: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    created_at: datetime

    @classmethod
    def of(cls, ticket: Ticket) -> TicketView:
        return cls.model_validate(ticket.model_dump())


# -- get_my_assets ------------------------------------------------------------


class GetMyAssetsInput(_StrictInput):
    pass


class GetMyAssetsOutput(BaseModel):
    assets: list[AssetView]


# -- list_my_tickets ----------------------------------------------------------


class ListMyTicketsInput(_StrictInput):
    include_closed: bool = Field(
        default=False, description="Also include resolved and closed tickets."
    )


class ListMyTicketsOutput(BaseModel):
    tickets: list[TicketView]


# -- get_ticket ---------------------------------------------------------------


class GetTicketInput(_StrictInput):
    ticket_id: TicketId


class GetTicketOutput(BaseModel):
    ticket: TicketView


# -- create_ticket ------------------------------------------------------------


class CreateTicketInput(_StrictInput):
    title: str = Field(min_length=5, max_length=120, description="Short summary of the problem.")
    description: str = Field(
        min_length=10,
        max_length=2000,
        description="What is wrong, since when, and what the employee already tried.",
    )
    category: TicketCategory = Field(description="The IT area of the problem.")
    priority: TicketPriority = Field(
        description="high: cannot work at all or security risk; medium: work significantly "
        "impaired; low: everything else."
    )


class AddTicketCommentInput(_StrictInput):
    ticket_id: TicketId
    comment: str = Field(
        min_length=5, max_length=1000, description="Update or extra information for the ticket."
    )


class AddTicketCommentOutput(BaseModel):
    ticket_id: str
    comment_id: str
    created: bool = Field(description="False if this exact comment had already been added.")


class CreateTicketOutput(BaseModel):
    ticket: TicketView
    created: bool = Field(description="False if this exact request had already created it.")


def build_service_desk_tools(repository: ServiceDeskRepository) -> list[ToolSpec[Any, Any]]:
    """Create the Service Desk tools bound to a repository."""

    def get_my_assets(_: GetMyAssetsInput, ctx: ToolCallContext) -> GetMyAssetsOutput:
        assets = repository.list_assets_for(ctx.user.employee_id)
        return GetMyAssetsOutput(assets=[AssetView.of(a) for a in assets])

    def list_my_tickets(args: ListMyTicketsInput, ctx: ToolCallContext) -> ListMyTicketsOutput:
        closed = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
        tickets = [
            t
            for t in repository.list_tickets_for(ctx.user.employee_id)
            if args.include_closed or t.status not in closed
        ]
        return ListMyTicketsOutput(tickets=[TicketView.of(t) for t in tickets])

    def get_ticket(args: GetTicketInput, ctx: ToolCallContext) -> GetTicketOutput:
        ticket = repository.get_ticket(args.ticket_id)
        # Ownership check. Someone else's ticket looks exactly like a missing one.
        if ticket is None or ticket.requester_id != ctx.user.employee_id:
            raise NotFoundError(f"No ticket {args.ticket_id} found for you.")
        return GetTicketOutput(ticket=TicketView.of(ticket))

    def create_ticket(args: CreateTicketInput, ctx: ToolCallContext) -> CreateTicketOutput:
        if ctx.idempotency_key is None:
            raise RuntimeError("create_ticket requires an idempotency key from the executor")
        ticket, created = repository.create_ticket(
            requester_id=ctx.user.employee_id,
            title=_single_line(args.title),
            description=args.description.strip(),
            category=args.category,
            priority=args.priority,
            idempotency_key=ctx.idempotency_key,
        )
        return CreateTicketOutput(ticket=TicketView.of(ticket), created=created)

    def add_ticket_comment(
        args: AddTicketCommentInput, ctx: ToolCallContext
    ) -> AddTicketCommentOutput:
        if ctx.idempotency_key is None:
            raise RuntimeError("add_ticket_comment requires an idempotency key from the executor")
        ticket = repository.get_ticket(args.ticket_id)
        if ticket is None or ticket.requester_id != ctx.user.employee_id:
            raise NotFoundError(f"No ticket {args.ticket_id} found for you.")
        comment, created = repository.add_ticket_comment(
            ticket_id=ticket.ticket_id,
            author_id=ctx.user.employee_id,
            body=args.comment.strip(),
            idempotency_key=ctx.idempotency_key,
        )
        return AddTicketCommentOutput(
            ticket_id=ticket.ticket_id, comment_id=comment.comment_id, created=created
        )

    return [
        ToolSpec(
            name="get_my_assets",
            description=(
                "List the IT equipment (laptops, monitors, phones, docking stations) "
                "assigned to the signed-in employee."
            ),
            input_model=GetMyAssetsInput,
            output_model=GetMyAssetsOutput,
            handler=get_my_assets,
            risk=ToolRisk.LOW,
            access=ToolAccess.READ,
            idempotent=True,
            owner=OWNER,
        ),
        ToolSpec(
            name="list_my_tickets",
            description=(
                "List the signed-in employee's own service tickets. Open tickets only "
                "unless include_closed is true."
            ),
            input_model=ListMyTicketsInput,
            output_model=ListMyTicketsOutput,
            handler=list_my_tickets,
            risk=ToolRisk.LOW,
            access=ToolAccess.READ,
            idempotent=True,
            owner=OWNER,
        ),
        ToolSpec(
            name="get_ticket",
            description="Look up one of the signed-in employee's own tickets by its ID.",
            input_model=GetTicketInput,
            output_model=GetTicketOutput,
            handler=get_ticket,
            risk=ToolRisk.LOW,
            access=ToolAccess.READ,
            idempotent=True,
            owner=OWNER,
        ),
        ToolSpec(
            name="create_ticket",
            description=(
                "Create a new IT support ticket for the signed-in employee. Use only when the "
                "employee wants a ticket and no open ticket already covers the same problem."
            ),
            input_model=CreateTicketInput,
            output_model=CreateTicketOutput,
            handler=create_ticket,
            risk=ToolRisk.MEDIUM,
            access=ToolAccess.WRITE,
            idempotent=True,  # via the idempotency key
            owner=OWNER,
        ),
        ToolSpec(
            name="add_ticket_comment",
            description=(
                "Add an update to one of the signed-in employee's own open tickets, for example "
                "new symptoms or steps already tried."
            ),
            input_model=AddTicketCommentInput,
            output_model=AddTicketCommentOutput,
            handler=add_ticket_comment,
            risk=ToolRisk.MEDIUM,
            access=ToolAccess.WRITE,
            idempotent=True,  # via the idempotency key
            owner=OWNER,
        ),
    ]


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

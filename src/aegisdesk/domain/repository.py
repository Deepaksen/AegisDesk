"""In-memory service-desk data store, seeded from `data/seed/*.json`.

Milestone 1 keeps data in memory on purpose: the lesson is tool calling, not
persistence. Writes live only for the lifetime of the process. PostgreSQL,
migrations and the full synthetic dataset replace this later, behind the same
methods.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from aegisdesk.domain.access import (
    AccessRecord,
    AccessRequest,
    AccessRequestStatus,
    Application,
    Approval,
)
from aegisdesk.domain.models import (
    Asset,
    Employee,
    Ticket,
    TicketCategory,
    TicketComment,
    TicketPriority,
    TicketStatus,
)


class ServiceDeskRepository:
    def __init__(
        self,
        employees: list[Employee],
        assets: list[Asset],
        tickets: list[Ticket],
        applications: list[Application] | None = None,
        access: list[AccessRecord] | None = None,
        access_requests: list[AccessRequest] | None = None,
        today: Callable[[], date] = date.today,
    ):
        self._employees = {e.employee_id: e for e in employees}
        self._assets = {a.asset_tag: a for a in assets}
        self._tickets = {t.ticket_id: t for t in tickets}
        self._applications = {a.application_id: a for a in applications or []}
        self._access = list(access or [])
        self._requests = {r.request_id: r for r in access_requests or []}
        self._today = today
        # idempotency key -> record id, so a repeated write returns the first result.
        self._ticket_idempotency: dict[str, str] = {}
        self._request_idempotency: dict[str, str] = {}
        self._comments: dict[str, TicketComment] = {}
        self._comment_idempotency: dict[str, str] = {}

    @classmethod
    def from_seed(
        cls, seed_dir: Path, *, today: Callable[[], date] = date.today
    ) -> ServiceDeskRepository:
        def load(name: str) -> list[dict[str, object]]:
            data: list[dict[str, object]] = json.loads(
                (seed_dir / f"{name}.json").read_text(encoding="utf-8")
            )
            return data

        return cls(
            employees=[Employee.model_validate(r) for r in load("employees")],
            assets=[Asset.model_validate(r) for r in load("assets")],
            tickets=[Ticket.model_validate(r) for r in load("tickets")],
            applications=[Application.model_validate(r) for r in load("applications")],
            access=[AccessRecord.model_validate(r) for r in load("employee_access")],
            access_requests=[AccessRequest.model_validate(r) for r in load("access_requests")],
            today=today,
        )

    # -- reads ------------------------------------------------------------

    def get_employee(self, employee_id: str) -> Employee | None:
        return self._employees.get(employee_id)

    def list_assets_for(self, employee_id: str) -> list[Asset]:
        return sorted(
            (a for a in self._assets.values() if a.assigned_to == employee_id),
            key=lambda a: a.asset_tag,
        )

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def list_tickets_for(self, employee_id: str) -> list[Ticket]:
        return sorted(
            (t for t in self._tickets.values() if t.requester_id == employee_id),
            key=lambda t: t.ticket_id,
        )

    # -- writes -----------------------------------------------------------

    def create_ticket(
        self,
        *,
        requester_id: str,
        title: str,
        description: str,
        category: TicketCategory,
        priority: TicketPriority,
        idempotency_key: str,
    ) -> tuple[Ticket, bool]:
        """Create a ticket, or return the one already created with this key.

        Returns `(ticket, created)`; `created` is False for a duplicate request.
        """
        existing_id = self._ticket_idempotency.get(idempotency_key)
        if existing_id is not None:
            return self._tickets[existing_id], False

        ticket = Ticket(
            ticket_id=self._next_ticket_id(),
            requester_id=requester_id,
            title=title,
            description=description,
            category=category,
            priority=priority,
            status=TicketStatus.OPEN,
            created_at=datetime.now(UTC),
        )
        self._tickets[ticket.ticket_id] = ticket
        self._ticket_idempotency[idempotency_key] = ticket.ticket_id
        return ticket, True

    def comments_for(self, ticket_id: str) -> list[TicketComment]:
        return [c for c in self._comments.values() if c.ticket_id == ticket_id]

    def add_ticket_comment(
        self, *, ticket_id: str, author_id: str, body: str, idempotency_key: str
    ) -> tuple[TicketComment, bool]:
        existing_id = self._comment_idempotency.get(idempotency_key)
        if existing_id is not None:
            return self._comments[existing_id], False
        comment = TicketComment(
            comment_id=f"CMT-{len(self._comments) + 1:04d}",
            ticket_id=ticket_id,
            author_id=author_id,
            body=body,
            created_at=datetime.now(UTC),
        )
        self._comments[comment.comment_id] = comment
        self._comment_idempotency[idempotency_key] = comment.comment_id
        return comment, True

    def _next_ticket_id(self) -> str:
        highest = max((int(t.split("-")[1]) for t in self._tickets), default=1000)
        return f"INC-{highest + 1}"

    # -- application access -------------------------------------------------

    def today(self) -> date:
        return self._today()

    def list_applications(self) -> list[Application]:
        return sorted(self._applications.values(), key=lambda a: a.name)

    def find_application(self, text: str) -> Application | None:
        """Resolve an application from an ID, a name, or text that mentions exactly one name."""
        wanted = text.strip().lower()
        for app in self._applications.values():
            if wanted in (app.application_id.lower(), app.name.lower()):
                return app
        mentioned = [a for a in self._applications.values() if a.name.lower() in wanted]
        return mentioned[0] if len(mentioned) == 1 else None

    def access_for(self, employee_id: str) -> list[AccessRecord]:
        return [a for a in self._access if a.employee_id == employee_id]

    def access_requests_for(self, employee_id: str) -> list[AccessRequest]:
        return sorted(
            (r for r in self._requests.values() if r.employee_id == employee_id),
            key=lambda r: r.request_id,
        )

    def access_request_for_key(self, idempotency_key: str) -> AccessRequest | None:
        request_id = self._request_idempotency.get(idempotency_key)
        return self._requests[request_id] if request_id else None

    def create_access_request(
        self,
        *,
        employee_id: str,
        application_id: str,
        approvals_required: list[Approval],
        justification: str,
        idempotency_key: str,
    ) -> tuple[AccessRequest, bool]:
        """Record a request (never grants access). Returns (request, created)."""
        existing_id = self._request_idempotency.get(idempotency_key)
        if existing_id is not None:
            return self._requests[existing_id], False

        highest = max((int(r.split("-")[1]) for r in self._requests), default=1000)
        request = AccessRequest(
            request_id=f"AR-{highest + 1}",
            employee_id=employee_id,
            application_id=application_id,
            status=(
                AccessRequestStatus.AWAITING_APPROVAL
                if approvals_required
                else AccessRequestStatus.AUTO_APPROVED
            ),
            approvals_required=approvals_required,
            created_at=datetime.now(UTC),
            justification=justification,
        )
        self._requests[request.request_id] = request
        self._request_idempotency[idempotency_key] = request.request_id
        return request, True

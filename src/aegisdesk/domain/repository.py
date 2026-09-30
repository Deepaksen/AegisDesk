"""Service-desk data, seeded from `data/seed/*.json`.

Employees, assets, tickets and applications are in memory (Milestone 1 keeps
the lesson on tool calling, not persistence). The access domain (access
records, requests, approvals) lives in an `AccessStore` from Milestone 7,
in memory or in PostgreSQL, because an approval must survive a restart.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from aegisdesk.domain.access import (
    AccessRecord,
    AccessRequest,
    Application,
    Approval,
    approval_steps,
)
from aegisdesk.domain.access_store import AccessStore, InMemoryAccessStore, guarded
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
        access_store: AccessStore | None = None,
        approval_ttl: timedelta = timedelta(days=7),
    ):
        self._employees = {e.employee_id: e for e in employees}
        self._assets = {a.asset_tag: a for a in assets}
        self._tickets = {t.ticket_id: t for t in tickets}
        self._applications = {a.application_id: a for a in applications or []}
        # Guarded (M11): a database outage surfaces as StoreUnavailableError.
        self.access_store: AccessStore = guarded(
            access_store or InMemoryAccessStore(access, access_requests)
        )
        self._approval_ttl = approval_ttl
        self._today = today
        # idempotency key -> record id, so a repeated write returns the first result.
        self._ticket_idempotency: dict[str, str] = {}
        self._comments: dict[str, TicketComment] = {}
        self._comment_idempotency: dict[str, str] = {}

    @classmethod
    def from_seed(
        cls,
        seed_dir: Path,
        *,
        today: Callable[[], date] = date.today,
        access_store: AccessStore | None = None,
        approval_ttl: timedelta = timedelta(days=7),
    ) -> ServiceDeskRepository:
        """Seed everything; with `access_store`, the access domain comes from that store."""

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
            access_store=access_store,
            approval_ttl=approval_ttl,
        )

    # -- reads ------------------------------------------------------------

    def list_employee_ids(self) -> list[str]:
        return sorted(self._employees)

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
        return self.access_store.access_for(employee_id)

    def access_requests_for(self, employee_id: str) -> list[AccessRequest]:
        return self.access_store.access_requests_for(employee_id)

    def access_request_for_key(self, idempotency_key: str) -> AccessRequest | None:
        return self.access_store.access_request_for_key(idempotency_key)

    def create_access_request(
        self,
        *,
        employee_id: str,
        application_id: str,
        approvals_required: list[Approval],
        justification: str,
        idempotency_key: str,
        thread_id: str | None = None,
    ) -> tuple[AccessRequest, bool]:
        """Record a request and its approval steps (never grants access)."""
        employee = self._employees[employee_id]
        return self.access_store.create_access_request(
            employee_id=employee_id,
            application_id=application_id,
            approvals_required=approvals_required,
            steps=approval_steps(employee, approvals_required),
            justification=justification,
            idempotency_key=idempotency_key,
            thread_id=thread_id,
            approval_ttl=self._approval_ttl,
        )

    def get_application(self, application_id: str) -> Application | None:
        return self._applications.get(application_id)

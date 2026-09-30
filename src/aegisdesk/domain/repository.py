"""In-memory service-desk data store, seeded from `data/seed/*.json`.

Milestone 1 keeps data in memory on purpose: the lesson is tool calling, not
persistence. Writes live only for the lifetime of the process. PostgreSQL,
migrations and the full synthetic dataset replace this later, behind the same
methods.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aegisdesk.domain.models import (
    Asset,
    Employee,
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)


class ServiceDeskRepository:
    def __init__(self, employees: list[Employee], assets: list[Asset], tickets: list[Ticket]):
        self._employees = {e.employee_id: e for e in employees}
        self._assets = {a.asset_tag: a for a in assets}
        self._tickets = {t.ticket_id: t for t in tickets}
        # idempotency key -> ticket_id, so a repeated write returns the first result.
        self._ticket_idempotency: dict[str, str] = {}

    @classmethod
    def from_seed(cls, seed_dir: Path) -> ServiceDeskRepository:
        def load(name: str) -> list[dict[str, object]]:
            data: list[dict[str, object]] = json.loads(
                (seed_dir / f"{name}.json").read_text(encoding="utf-8")
            )
            return data

        return cls(
            employees=[Employee.model_validate(r) for r in load("employees")],
            assets=[Asset.model_validate(r) for r in load("assets")],
            tickets=[Ticket.model_validate(r) for r in load("tickets")],
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

    def _next_ticket_id(self) -> str:
        highest = max((int(t.split("-")[1]) for t in self._tickets), default=1000)
        return f"INC-{highest + 1}"

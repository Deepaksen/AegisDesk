from __future__ import annotations

import pytest

from aegisdesk.domain.models import TicketCategory, TicketPriority, TicketStatus
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import AuthenticationError, authenticate


def test_seed_data_loads(repository: ServiceDeskRepository) -> None:
    aisha = repository.get_employee("E1004")
    assert aisha is not None
    assert aisha.department == "finance"
    assert aisha.manager_id == "E1010"
    assert [a.asset_tag for a in repository.list_assets_for("E1004")] == [
        "NS-LT-0101",
        "NS-MN-0102",
    ]
    assert [t.ticket_id for t in repository.list_tickets_for("E1004")] == ["INC-1001", "INC-1002"]


def test_create_ticket_assigns_next_id(repository: ServiceDeskRepository) -> None:
    ticket, created = repository.create_ticket(
        requester_id="E1004",
        title="Printer offline",
        description="The 3rd floor printer shows offline.",
        category=TicketCategory.HARDWARE,
        priority=TicketPriority.LOW,
        idempotency_key="k1",
    )
    assert created
    assert ticket.ticket_id == "INC-1008"
    assert ticket.status is TicketStatus.OPEN
    assert repository.get_ticket("INC-1008") == ticket


def test_same_idempotency_key_returns_first_ticket(repository: ServiceDeskRepository) -> None:
    kwargs = {
        "requester_id": "E1004",
        "title": "Printer offline",
        "description": "The 3rd floor printer shows offline.",
        "category": TicketCategory.HARDWARE,
        "priority": TicketPriority.LOW,
        "idempotency_key": "same-key",
    }
    first, created_first = repository.create_ticket(**kwargs)  # type: ignore[arg-type]
    second, created_second = repository.create_ticket(**kwargs)  # type: ignore[arg-type]

    assert (created_first, created_second) == (True, False)
    assert second.ticket_id == first.ticket_id
    assert len(repository.list_tickets_for("E1004")) == 3


def test_authenticate_builds_trusted_context(repository: ServiceDeskRepository) -> None:
    user = authenticate(repository, "E1010")
    assert user.employee_id == "E1010"
    assert "manager" in user.roles
    assert user.department == "finance"


@pytest.mark.parametrize(
    ("employee_id", "reason"), [("E9999", "Unknown employee"), ("E1007", "not active")]
)
def test_authenticate_rejects_unknown_and_terminated(
    repository: ServiceDeskRepository, employee_id: str, reason: str
) -> None:
    with pytest.raises(AuthenticationError, match=reason):
        authenticate(repository, employee_id)


def test_contractor_can_sign_in(repository: ServiceDeskRepository) -> None:
    assert authenticate(repository, "E1005").roles == ("contractor",)

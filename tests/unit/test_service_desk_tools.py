"""The Service Desk tools, exercised through the executor exactly as the agent uses them."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import ToolAccess, ToolRisk
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor
from aegisdesk.tools.service_desk import build_service_desk_tools

VPN_TICKET = {
    "title": "VPN keeps disconnecting",
    "description": "GlobalProtect drops every 10 minutes; restarted client and laptop already.",
    "category": "vpn",
    "priority": "medium",
}


@pytest.fixture
def executor(repository: ServiceDeskRepository) -> ToolExecutor:
    return ToolExecutor(build_service_desk_tools(repository))


def _run(
    executor: ToolExecutor,
    user: UserContext,
    name: str,
    args: dict[str, Any],
    request_id: str = "r1",
) -> tuple[OutcomeStatus, dict[str, Any]]:
    outcome = executor.execute(name, args, user=user, request_id=request_id)
    return outcome.status, json.loads(outcome.content)


def test_tool_catalogue_and_metadata(executor: ToolExecutor) -> None:
    assert executor.tool_names == [
        "get_my_assets",
        "list_my_tickets",
        "get_ticket",
        "create_ticket",
    ]
    create = executor.get("create_ticket")
    assert create is not None
    assert (create.risk, create.access) == (ToolRisk.MEDIUM, ToolAccess.WRITE)
    for name in ("get_my_assets", "list_my_tickets", "get_ticket"):
        spec = executor.get(name)
        assert spec is not None
        assert (spec.risk, spec.access) == (ToolRisk.LOW, ToolAccess.READ)


def test_model_facing_schemas_have_no_identity_parameters(executor: ToolExecutor) -> None:
    for definition in executor.model_definitions():
        parameters = definition["function"]["parameters"]
        assert parameters["additionalProperties"] is False
        assert "$defs" not in parameters  # refs are inlined for every provider
        for field in parameters["properties"]:
            assert "employee" not in field and "user" not in field and "requester" not in field


def test_get_my_assets_returns_only_own_assets(executor: ToolExecutor, aisha: UserContext) -> None:
    status, body = _run(executor, aisha, "get_my_assets", {})
    assert status is OutcomeStatus.OK
    assert [a["asset_tag"] for a in body["assets"]] == ["NS-LT-0101", "NS-MN-0102"]
    # Views do not expose internal fields such as assigned_to.
    assert "assigned_to" not in body["assets"][0]


def test_list_my_tickets_filters_closed_by_default(
    executor: ToolExecutor, aisha: UserContext
) -> None:
    _, open_only = _run(executor, aisha, "list_my_tickets", {})
    _, everything = _run(executor, aisha, "list_my_tickets", {"include_closed": True})
    assert [t["ticket_id"] for t in open_only["tickets"]] == ["INC-1001"]
    assert [t["ticket_id"] for t in everything["tickets"]] == ["INC-1001", "INC-1002"]


def test_get_ticket_normalises_the_id(executor: ToolExecutor, aisha: UserContext) -> None:
    status, body = _run(executor, aisha, "get_ticket", {"ticket_id": " inc-1001 "})
    assert status is OutcomeStatus.OK
    assert body["ticket"]["ticket_id"] == "INC-1001"


def test_someone_elses_ticket_looks_like_a_missing_one(
    executor: ToolExecutor, aisha: UserContext
) -> None:
    _, others = _run(executor, aisha, "get_ticket", {"ticket_id": "INC-1003"})  # E1001's
    _, missing = _run(executor, aisha, "get_ticket", {"ticket_id": "INC-9999"})
    assert others["error"]["category"] == missing["error"]["category"] == "not_found"
    assert "VPN" not in json.dumps(others) and "GitHub" not in json.dumps(others)


def test_create_ticket_uses_the_signed_in_user(
    executor: ToolExecutor, aisha: UserContext, repository: ServiceDeskRepository
) -> None:
    status, body = _run(executor, aisha, "create_ticket", VPN_TICKET)

    assert status is OutcomeStatus.OK
    assert body["created"] is True
    ticket = repository.get_ticket(body["ticket"]["ticket_id"])
    assert ticket is not None
    assert ticket.requester_id == "E1004"


def test_create_ticket_is_idempotent_within_a_request(
    executor: ToolExecutor, aisha: UserContext, repository: ServiceDeskRepository
) -> None:
    _, first = _run(executor, aisha, "create_ticket", VPN_TICKET, request_id="req-A")
    _, retry = _run(executor, aisha, "create_ticket", VPN_TICKET, request_id="req-A")

    assert retry["created"] is False
    assert retry["ticket"]["ticket_id"] == first["ticket"]["ticket_id"]
    assert len(repository.list_tickets_for("E1004")) == 3  # 2 seeded + 1


def test_a_new_request_can_create_another_ticket(
    executor: ToolExecutor, aisha: UserContext, repository: ServiceDeskRepository
) -> None:
    _run(executor, aisha, "create_ticket", VPN_TICKET, request_id="req-A")
    _, second = _run(executor, aisha, "create_ticket", VPN_TICKET, request_id="req-B")
    assert second["created"] is True
    assert len(repository.list_tickets_for("E1004")) == 4


@pytest.mark.parametrize(
    "bad_args",
    [
        {**VPN_TICKET, "title": "hi"},  # too short
        {**VPN_TICKET, "category": "printer"},  # not an allowed category
        {**VPN_TICKET, "priority": "urgent"},
        {k: v for k, v in VPN_TICKET.items() if k != "description"},
    ],
)
def test_create_ticket_rejects_invalid_arguments(
    executor: ToolExecutor,
    aisha: UserContext,
    repository: ServiceDeskRepository,
    bad_args: dict[str, Any],
) -> None:
    status, body = _run(executor, aisha, "create_ticket", bad_args)
    assert status is OutcomeStatus.ERROR
    assert body["error"]["category"] == "invalid_arguments"
    assert len(repository.list_tickets_for("E1004")) == 2  # nothing written

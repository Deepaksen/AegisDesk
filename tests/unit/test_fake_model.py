"""The offline fake's demo mode, which the CLI uses without a real model."""

from __future__ import annotations

import pytest

from aegisdesk.agents.service_desk import build_service_desk_agent
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext


@pytest.mark.parametrize(
    ("message", "expected_tool"),
    [
        ("What laptop is assigned to me?", "get_my_assets"),
        ("What tickets do I have open?", "list_my_tickets"),
        ("Show me ticket inc-1001", "get_ticket"),
        ("My VPN keeps dropping and I tried the guide, please create a ticket", "create_ticket"),
    ],
)
def test_demo_mode_routes_to_a_plausible_tool(
    repository: ServiceDeskRepository, aisha: UserContext, message: str, expected_tool: str
) -> None:
    agent = build_service_desk_agent(Settings(), repository)

    run = agent.run(message, user=aisha)

    assert [s.tool_name for s in run.tool_steps] == [expected_tool]
    assert run.answer.startswith("[fake model] Tool results:")


def test_demo_mode_without_a_matching_tool_just_replies(
    repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    run = build_service_desk_agent(Settings(), repository).run("hello", user=aisha)
    assert run.tool_steps == []
    assert run.answer == "[fake model] You said: hello"


def test_demo_ticket_arguments_come_from_the_text(
    repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    run = build_service_desk_agent(Settings(), repository).run(
        "Create a ticket: my VPN keeps dropping every few minutes", user=aisha
    )
    args = run.tool_steps[0].args
    assert args["category"] == "vpn"
    assert args["title"].startswith("Create a ticket")

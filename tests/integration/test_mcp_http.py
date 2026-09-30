"""The MCP servers over real Streamable HTTP, as `aegisdesk mcp serve` runs them."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from aegisdesk.agents.supervisor import build_supervisor_agent, specialist_identity
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext
from aegisdesk.mcp_servers.catalogue import McpServerName
from aegisdesk.mcp_servers.http import build_http_app
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.handoff import AgentName
from aegisdesk.tools.remote import McpGateway, ToolTransportError
from aegisdesk.tools.service_desk import build_service_desk_tools
from aegisdesk.tools.transport import ToolFactory

SECRET = "http-secret-" + "h" * 32


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def base_url(repository: ServiceDeskRepository) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        build_http_app(repository, SECRET), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "MCP HTTP server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _settings(base_url: str) -> Settings:
    return Settings(
        tool_transport=ToolTransport.MCP_HTTP,
        mcp_token_secret=SecretStr(SECRET),
        mcp_read_url=f"{base_url}/read/mcp",
        mcp_action_url=f"{base_url}/action/mcp",
    )


def test_health_endpoint(base_url: str) -> None:
    assert httpx.get(f"{base_url}/healthz").json() == {
        "status": "ok",
        "servers": ["read", "action"],
    }


def test_supervisor_creates_access_request_over_http(
    base_url: str,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    aisha: UserContext,
) -> None:
    settings = _settings(base_url)
    with ToolFactory.from_settings(settings, repository) as factory:
        agent = build_supervisor_agent(
            settings,
            repository,
            checkpointer=InMemorySaver(),
            retriever=retriever,
            tool_factory=factory,
        )
        run = agent.run(
            "Please create an access request for FinanceERP for month-end reporting", user=aisha
        )

    created = json.loads(run.tool_steps[-1].result)
    assert (created["application"], created["status"]) == ("FinanceERP", "awaiting_approval")
    # The server (same process here) recorded it for the token's user.
    assert [r.application_id for r in repository.access_requests_for("E1004")][-1] == "APP-FIN"


def test_host_with_wrong_secret_is_refused(
    base_url: str, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    settings = _settings(base_url).model_copy(
        update={"mcp_token_secret": SecretStr("wrong-" + "w" * 32)}
    )
    with ToolFactory.from_settings(settings, repository) as factory:
        runner = factory.runner(
            specialist_identity(AgentName.SERVICE_DESK, settings),
            build_service_desk_tools(repository),
        )
        outcome = runner.execute("get_my_assets", {}, user=aisha, request_id="r")

    assert outcome.error_category == "unauthenticated"


def test_unreachable_server_is_reported_as_unavailable() -> None:
    url = f"http://127.0.0.1:{_free_port()}/read/mcp"
    with (
        McpGateway({McpServerName.READ: url}, timeout_seconds=2) as gateway,
        pytest.raises(ToolTransportError) as exc_info,
    ):
        gateway.list_tools(McpServerName.READ)

    assert exc_info.value.category == "unavailable"

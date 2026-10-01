"""Serve both MCP servers over Streamable HTTP from one process.

    POST/GET /read/mcp     aegisdesk-read
    POST/GET /action/mcp   aegisdesk-action
    GET      /healthz      liveness

One process keeps local setup simple and lets both servers share the
in-memory repository. They remain two MCP servers with two audiences: a
token for one is refused by the other. Running them as separate services is
a deployment change once the repository is backed by a shared database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.mcp_servers.catalogue import build_servers


def build_http_app(
    repository: ServiceDeskRepository,
    token_secret: str,
    *,
    gateway: ActionGateway,
    host: str = "127.0.0.1",
) -> Starlette:
    servers = build_servers(repository, token_secret, gateway=gateway)
    # `host` enables the SDK's DNS-rebinding protection for localhost binds.
    mounts = [
        Mount(f"/{name.value}", app=server.streamable_http_app(host=host))
        for name, server in servers.items()
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Mounted apps' own lifespans do not run, so start each session manager here.
        async with AsyncExitStack() as stack:
            for server in servers.values():
                await stack.enter_async_context(server.session_manager.run())
            yield

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "servers": [n.value for n in servers]})

    return Starlette(routes=[Route("/healthz", healthz), *mounts], lifespan=lifespan)

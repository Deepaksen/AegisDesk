"""Who is calling: the single place identity enters the API.

Milestone 10 uses a **trusted header**, `X-Employee-Id`, as if an authenticating
gateway (SSO proxy, API gateway) had verified the user and forwarded the ID.
The API still checks it against the employee directory (unknown or inactive
employees are refused), but it cannot tell a gateway from anyone else who can
reach the port. So:

* deploy the API only behind such a gateway, on a network nothing else can reach;
* never trust the same header from the public internet.

Routes depend on `current_user` and nothing else, so replacing this with OIDC
token verification (spec stretch goal) changes this file only. ADR 0015.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from aegisdesk.identity.context import AuthenticationError, UserContext, authenticate
from aegisdesk.observability import tracing
from aegisdesk.observability.redaction import pseudonym
from aegisdesk.runtime import AegisRuntime

EMPLOYEE_HEADER = "X-Employee-Id"


class AuthenticationRequiredError(Exception):
    pass


def get_runtime(request: Request) -> AegisRuntime:
    runtime: AegisRuntime = request.app.state.runtime
    return runtime


def current_user(
    runtime: Annotated[AegisRuntime, Depends(get_runtime)],
    employee_id: Annotated[
        str | None,
        Header(
            alias=EMPLOYEE_HEADER,
            description="Set by the authenticating gateway (development: a synthetic ID).",
        ),
    ] = None,
) -> UserContext:
    if not employee_id:
        raise AuthenticationRequiredError(f"missing {EMPLOYEE_HEADER} header")
    with tracing.span("aegisdesk.authenticate", **{tracing.USER: pseudonym(employee_id)}):
        try:
            return authenticate(runtime.repository, employee_id.strip())
        except AuthenticationError as exc:
            raise AuthenticationRequiredError("unknown or inactive employee") from exc


CurrentUser = Annotated[UserContext, Depends(current_user)]
Runtime = Annotated[AegisRuntime, Depends(get_runtime)]

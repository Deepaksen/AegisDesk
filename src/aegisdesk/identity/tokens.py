"""Delegation tokens for MCP calls.

When an agent calls a tool on an MCP server, the server must know three
things it cannot take from the tool arguments (which the model writes):

* **who the user is** - the authenticated employee and their claims;
* **which agent is acting** - agent id, version, type, environment;
* **which request this is** - for idempotency, audit and tracing.

The host (our application) puts these in a short-lived signed JWT, minted per
tool call, and sends it in the MCP request's `_meta`. Claims follow standard
names where they exist:

    iss, aud, sub, iat, exp, jti     (RFC 7519)
    act = {sub: agent_id, ...}       (RFC 8693 "actor": who acts on the user's behalf)
    rid                              AegisDesk request id
    tid (optional)                   conversation thread id, for audit
    roles, department, manager_id    user claims from the identity provider

`aud` is the target server, so a token minted for the read server is refused
by the action server. The model never sees the token and cannot create one:
it has no access to the signing key.

HS256 with a shared secret keeps local setup simple. A real deployment would
use an OAuth authorization server and asymmetric keys (RS256/ES256), and
MCP's standard bearer-token auth; the claims would stay the same.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext

ISSUER = "aegisdesk-host"
TOKEN_META_KEY = "aegisdesk/token"  # noqa: S105 - a _meta key name, not a secret
DEFAULT_TTL_SECONDS = 60
_ALGORITHM = "HS256"


class TokenError(PermissionError):
    """The delegation token is missing, malformed, expired, forged or for another audience."""


@dataclass(frozen=True)
class CallerContext:
    """Everything a server may trust about a call, taken from a verified token."""

    user: UserContext
    agent: AgentIdentity
    request_id: str
    token_id: str
    thread_id: str | None = None


class TokenIssuer:
    def __init__(self, secret: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        if len(secret) < 32:
            raise ValueError("token secret must be at least 32 characters")
        self._secret = secret
        self._ttl = ttl_seconds

    def issue(
        self,
        *,
        user: UserContext,
        agent: AgentIdentity,
        request_id: str,
        audience: str,
        thread_id: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": audience,
            "sub": user.employee_id,
            "iat": now,
            "exp": now + self._ttl,
            "jti": str(uuid.uuid4()),
            "rid": request_id,
            "roles": list(user.roles),
            "department": user.department,
            "manager_id": user.manager_id,
            "act": {
                "sub": agent.agent_id,
                "ver": agent.agent_version,
                "typ": agent.agent_type,
                "env": agent.environment,
            },
        }
        if thread_id is not None:
            claims["tid"] = thread_id  # conversation thread, for audit
        return jwt.encode(claims, self._secret, algorithm=_ALGORITHM)


class TokenVerifier:
    def __init__(self, secret: str, *, audience: str, leeway_seconds: int = 5) -> None:
        self._secret = secret
        self._audience = audience
        self._leeway = leeway_seconds

    @property
    def audience(self) -> str:
        return self._audience

    def verify(self, token: str | None) -> CallerContext:
        if not token:
            raise TokenError("missing delegation token")
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],  # never accept "none" or another algorithm
                audience=self._audience,
                issuer=ISSUER,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "aud", "iss", "jti", "rid", "act"]},
            )
            actor = claims["act"]
            return CallerContext(
                user=UserContext(
                    employee_id=str(claims["sub"]),
                    roles=tuple(str(r) for r in claims.get("roles", [])),
                    department=str(claims.get("department", "")),
                    manager_id=claims.get("manager_id"),
                ),
                agent=AgentIdentity(
                    agent_id=str(actor["sub"]),
                    agent_version=str(actor["ver"]),
                    agent_type=str(actor["typ"]),
                    environment=str(actor["env"]),
                ),
                request_id=str(claims["rid"]),
                token_id=str(claims["jti"]),
                thread_id=str(claims["tid"]) if claims.get("tid") is not None else None,
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("delegation token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenError(f"delegation token is not for {self._audience}") from exc
        except (jwt.InvalidTokenError, KeyError, TypeError) as exc:
            raise TokenError(f"invalid delegation token: {type(exc).__name__}") from exc

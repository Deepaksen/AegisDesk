"""Delegation tokens: what a server can trust about a call."""

from __future__ import annotations

import time

import jwt
import pytest

from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.identity.tokens import ISSUER, TokenError, TokenIssuer, TokenVerifier

SECRET = "test-secret-" + "x" * 32
AUDIENCE = "aegisdesk-mcp-read"
AGENT = AgentIdentity("access", "0.1.0", "specialist", "test")


def _token(user: UserContext) -> str:
    return TokenIssuer(SECRET).issue(user=user, agent=AGENT, request_id="req-1", audience=AUDIENCE)


def test_round_trip_carries_user_agent_and_request(aisha: UserContext) -> None:
    caller = TokenVerifier(SECRET, audience=AUDIENCE).verify(_token(aisha))

    assert caller.user == aisha
    assert caller.agent == AGENT
    assert caller.request_id == "req-1"
    assert caller.token_id


def test_standard_claims_and_actor(aisha: UserContext) -> None:
    claims = jwt.decode(_token(aisha), SECRET, algorithms=["HS256"], audience=AUDIENCE)

    assert claims["iss"] == ISSUER and claims["sub"] == "E1004" and claims["aud"] == AUDIENCE
    assert claims["exp"] - claims["iat"] == 60  # short-lived
    assert claims["act"]["sub"] == "access"  # RFC 8693: the agent acts for the user


def test_each_token_is_unique(aisha: UserContext) -> None:
    assert _token(aisha) != _token(aisha)


def test_missing_token_is_refused() -> None:
    with pytest.raises(TokenError, match="missing"):
        TokenVerifier(SECRET, audience=AUDIENCE).verify(None)


def test_token_for_another_server_is_refused(aisha: UserContext) -> None:
    with pytest.raises(TokenError, match="not for"):
        TokenVerifier(SECRET, audience="aegisdesk-mcp-action").verify(_token(aisha))


def test_forged_token_is_refused(aisha: UserContext) -> None:
    forged = TokenIssuer("attacker-secret-" + "y" * 32).issue(
        user=aisha, agent=AGENT, request_id="r", audience=AUDIENCE
    )
    with pytest.raises(TokenError, match="invalid"):
        TokenVerifier(SECRET, audience=AUDIENCE).verify(forged)


def test_tampered_claims_are_refused(aisha: UserContext) -> None:
    header, payload, signature = _token(aisha).split(".")
    other = jwt.encode({"sub": "E1010"}, SECRET, algorithm="HS256").split(".")[1]
    with pytest.raises(TokenError):
        TokenVerifier(SECRET, audience=AUDIENCE).verify(f"{header}.{other}.{signature}")
    assert payload  # the original payload was replaced, the signature no longer matches


def test_expired_token_is_refused(aisha: UserContext) -> None:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "E1004",
        "iat": now - 120,
        "exp": now - 60,
        "jti": "j",
        "rid": "r",
        "act": {"sub": "access", "ver": "1", "typ": "specialist", "env": "test"},
    }
    with pytest.raises(TokenError, match="expired"):
        TokenVerifier(SECRET, audience=AUDIENCE).verify(jwt.encode(claims, SECRET))


def test_unsigned_token_is_refused() -> None:
    unsigned = jwt.encode({"sub": "E1004", "aud": AUDIENCE}, key="", algorithm="none")
    with pytest.raises(TokenError):
        TokenVerifier(SECRET, audience=AUDIENCE).verify(unsigned)


def test_token_without_actor_is_refused() -> None:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "E1004", "iat": now, "exp": now + 60}
    claims |= {"jti": "j", "rid": "r"}
    with pytest.raises(TokenError, match="invalid"):
        TokenVerifier(SECRET, audience=AUDIENCE).verify(jwt.encode(claims, SECRET))


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="32"):
        TokenIssuer("too-short")

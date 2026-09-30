"""Agent identity: which agent is acting (spec section 13).

User identity says *on whose behalf* something happens; agent identity says
*which piece of software* is doing it. Both travel with every MCP call, so a
server (and, from Milestone 6, the policy engine) can answer "may the Access
agent, version 0.3.0, in development, call create_access_request for E1004?".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str  # e.g. "access"
    agent_version: str  # e.g. "0.3.0"
    agent_type: str  # "supervisor" | "specialist" | "single_agent"
    environment: str  # "development" | "test" | "production"

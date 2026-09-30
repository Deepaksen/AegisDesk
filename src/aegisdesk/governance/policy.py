"""The policy engine: may this agent call this tool for this user, here?

Deterministic code over policy *data* (`config/policy.yaml`). The input and
the decision have the shape of an OPA query, so the engine could be replaced
by OPA later without touching its callers:

    input    {agent, user, tool, environment}
    decision {decision: allow | deny | require_approval, reasons: [...], policy_version}

Every deny rule is evaluated and every reason is reported (like a Rego
`deny` set), so an audit record says *all* the ways a call was wrong. Any
deny wins; otherwise HIGH risk needs a human; otherwise the call is allowed.
Anything unexpected inside evaluation is a deny: the engine fails closed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import ToolRisk

logger = logging.getLogger(__name__)


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class DenyReason(StrEnum):
    FORBIDDEN_ACTION = "forbidden_action"
    UNCLASSIFIED_TOOL = "unclassified_tool"
    UNKNOWN_AGENT = "unknown_agent"
    AGENT_NOT_AUTHORIZED_FOR_TOOL = "agent_not_authorized_for_tool"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    NOT_APPROVED_FOR_ENVIRONMENT = "not_approved_for_environment"
    WRITE_NOT_AUTHORIZED = "write_not_authorized"
    MISSING_ROLE = "missing_role"
    POLICY_ERROR = "policy_error"


@dataclass(frozen=True)
class PolicyInput:
    tool: str
    user: UserContext
    agent: AgentIdentity | None
    # Where the enforcing component runs (not what the caller claims).
    environment: str


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reasons: tuple[str, ...]
    policy_version: str
    # The risk the policy assigned, if the tool is classified.
    risk: ToolRisk | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class PolicyError(ValueError):
    """The policy file is missing or invalid. Startup must stop."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolRule(_Strict):
    risk: ToolRisk


class EnvironmentRule(_Strict):
    allowed_tools: Literal["all"] | list[str]


class PolicyData(_Strict):
    version: int
    tools: dict[str, ToolRule]
    authorized_writes: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    agents: dict[str, list[str]]
    environments: dict[str, EnvironmentRule]
    required_roles: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> PolicyData:
        # Catch typos in the policy itself: every tool it names must be classified.
        named = set(self.authorized_writes) | set(self.required_roles)
        for tools in self.agents.values():
            named |= set(tools)
        for env in self.environments.values():
            if env.allowed_tools != "all":
                named |= set(env.allowed_tools)
        unknown = sorted(named - set(self.tools))
        if unknown:
            raise ValueError(f"policy names unclassified tools: {unknown}")
        both = sorted(set(self.forbidden_tools) & set(self.tools))
        if both:
            raise ValueError(f"tools both classified and forbidden: {both}")
        return self


class PolicyEngine:
    def __init__(self, data: PolicyData, *, version: str) -> None:
        self._data = data
        self.version = version

    @classmethod
    def from_file(cls, path: Path) -> PolicyEngine:
        try:
            raw = path.read_bytes()
            data = PolicyData.model_validate(yaml.safe_load(raw))
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise PolicyError(f"cannot load policy {path}: {exc}") from exc
        version = f"v{data.version}-{hashlib.sha256(raw).hexdigest()[:12]}"
        return cls(data, version=version)

    @property
    def data(self) -> PolicyData:
        return self._data

    def agent_tools(self, agent_id: str) -> frozenset[str]:
        return frozenset(self._data.agents.get(agent_id, ()))

    def evaluate(self, request: PolicyInput) -> PolicyDecision:
        try:
            return self._evaluate(request)
        except Exception:
            logger.exception("policy evaluation failed for tool %s", request.tool)
            return PolicyDecision(Decision.DENY, (DenyReason.POLICY_ERROR.value,), self.version)

    def _evaluate(self, request: PolicyInput) -> PolicyDecision:
        data = self._data
        rule = data.tools.get(request.tool)
        risk = rule.risk if rule else None
        reasons: list[DenyReason] = []

        if request.tool in data.forbidden_tools:
            reasons.append(DenyReason.FORBIDDEN_ACTION)
        elif rule is None:
            reasons.append(DenyReason.UNCLASSIFIED_TOOL)

        agent = request.agent
        if agent is None or agent.agent_id not in data.agents:
            reasons.append(DenyReason.UNKNOWN_AGENT)
        elif request.tool not in data.agents[agent.agent_id]:
            reasons.append(DenyReason.AGENT_NOT_AUTHORIZED_FOR_TOOL)
        if agent is not None and agent.environment != request.environment:
            reasons.append(DenyReason.ENVIRONMENT_MISMATCH)

        env = data.environments.get(request.environment)
        if env is None or (env.allowed_tools != "all" and request.tool not in env.allowed_tools):
            reasons.append(DenyReason.NOT_APPROVED_FOR_ENVIRONMENT)

        if risk is ToolRisk.MEDIUM and request.tool not in data.authorized_writes:
            reasons.append(DenyReason.WRITE_NOT_AUTHORIZED)

        needed = data.required_roles.get(request.tool)
        if needed and not set(needed) & set(request.user.roles):
            reasons.append(DenyReason.MISSING_ROLE)

        if reasons:
            return PolicyDecision(
                Decision.DENY, tuple(r.value for r in reasons), self.version, risk
            )
        if risk is ToolRisk.HIGH:
            return PolicyDecision(
                Decision.REQUIRE_APPROVAL, ("high_risk_requires_approval",), self.version, risk
            )
        return PolicyDecision(Decision.ALLOW, (), self.version, risk)

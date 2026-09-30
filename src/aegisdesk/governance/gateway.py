"""The action gateway: every tool call passes here before it runs.

    agent ─ tool request ─► ToolExecutor: lookup · schema validation
                                  │
                                  ▼
                           ActionGateway.authorize
                             1. PolicyEngine.evaluate(agent, user, tool, environment)
                             2. audit "decision" event (before anything runs)
                                  │
                ┌─────────────────┼──────────────────┐
              DENY              ALLOW          REQUIRE_APPROVAL
         policy_denied       handler runs      approval_required
                                  │           (human review, M7)
                                  ▼
                           ActionGateway.record_outcome → audit "outcome" event

The gateway is deterministic code. It does not read the conversation, and
nothing the model writes reaches it except the tool name and the validated
arguments (used only to note resource IDs in the audit record).

Fail closed: no agent identity → deny; policy engine error → deny; decision
event cannot be written for a write tool → deny. For a read tool an audit
failure is logged and the read proceeds (reads change nothing).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aegisdesk.audit.events import AuditError, AuditEvent, AuditLog, AuditPhase, resource_ids
from aegisdesk.governance.policy import Decision, PolicyDecision, PolicyEngine, PolicyInput
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import ToolAccess, ToolRisk

logger = logging.getLogger(__name__)

AUDIT_UNAVAILABLE = "audit_unavailable"


@dataclass(frozen=True)
class Authorization:
    """The gateway's answer for one call, and what the outcome event needs."""

    decision: PolicyDecision
    event: AuditEvent

    @property
    def allowed(self) -> bool:
        return self.decision.allowed


class ActionGateway:
    def __init__(self, policy: PolicyEngine, audit: AuditLog, *, environment: str) -> None:
        self.policy = policy
        self.audit = audit
        self.environment = environment

    def authorize(
        self,
        *,
        tool: str,
        access: ToolAccess,
        args: dict[str, Any],
        user: UserContext,
        agent: AgentIdentity | None,
        request_id: str,
        thread_id: str | None = None,
    ) -> Authorization:
        decision = self.policy.evaluate(
            PolicyInput(tool=tool, user=user, agent=agent, environment=self.environment)
        )
        event = AuditEvent(
            phase=AuditPhase.DECISION,
            call_id=str(uuid.uuid4()),
            request_id=request_id,
            thread_id=thread_id,
            user_id=user.employee_id,
            agent_id=agent.agent_id if agent else None,
            agent_version=agent.agent_version if agent else None,
            environment=self.environment,
            tool=tool,
            risk=decision.risk.value if decision.risk else None,
            resource=resource_ids(args),
            policy_decision=decision.decision.value,
            policy_reasons=decision.reasons,
            policy_version=decision.policy_version,
            outcome=decision.decision.value,
        )
        try:
            self.audit.record(event)
        except AuditError:
            logger.exception("audit decision event not recorded (tool=%s)", tool)
            # Unknown risk counts as a write: never act without a record.
            writes = access is ToolAccess.WRITE or decision.risk not in (None, ToolRisk.LOW)
            if writes and decision.decision is not Decision.DENY:
                decision = PolicyDecision(
                    Decision.DENY, (AUDIT_UNAVAILABLE,), decision.policy_version, decision.risk
                )
        if not decision.allowed:
            logger.info(
                "policy %s: tool=%s user=%s agent=%s reasons=%s request_id=%s",
                decision.decision.value,
                tool,
                user.employee_id,
                event.agent_id,
                ",".join(decision.reasons),
                request_id,
            )
        return Authorization(decision, event)

    def record_outcome(
        self, authorization: Authorization, *, outcome: str, latency_ms: float
    ) -> None:
        event = authorization.event.model_copy(
            update={
                "event_id": str(uuid.uuid4()),
                "phase": AuditPhase.OUTCOME,
                "occurred_at": datetime.now(UTC),
                "policy_decision": authorization.decision.decision.value,
                "policy_reasons": authorization.decision.reasons,
                "outcome": outcome,
                "latency_ms": latency_ms,
            }
        )
        try:
            self.audit.record(event)
        except AuditError:
            logger.exception("audit outcome event not recorded (tool=%s)", event.tool)

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
from typing import Any, Protocol

from aegisdesk.audit.events import AuditError, AuditEvent, AuditLog, AuditPhase, resource_ids
from aegisdesk.governance.policy import (
    ApprovalEvidence,
    Decision,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
)
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.observability import tracing
from aegisdesk.observability.metrics import instruments
from aegisdesk.tools.base import ToolAccess, ToolRisk

logger = logging.getLogger(__name__)

AUDIT_UNAVAILABLE = "audit_unavailable"


class ApprovalVerifier(Protocol):
    """Finds recorded approval for a call from trusted state, or returns None."""

    def evidence(
        self, *, tool: str, args: dict[str, Any], user: UserContext
    ) -> ApprovalEvidence | None: ...


@dataclass(frozen=True)
class Authorization:
    """The gateway's answer for one call, and what the outcome event needs."""

    decision: PolicyDecision
    event: AuditEvent

    @property
    def allowed(self) -> bool:
        return self.decision.allowed


class ActionGateway:
    def __init__(
        self,
        policy: PolicyEngine,
        audit: AuditLog,
        *,
        environment: str,
        approvals: ApprovalVerifier | None = None,
    ) -> None:
        self.policy = policy
        self.audit = audit
        self.environment = environment
        self.approvals = approvals

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
        with tracing.span("policy.evaluate", **{tracing.TOOL_NAME: tool}) as current:
            writes_so_far = 0
            if access is ToolAccess.WRITE:
                try:
                    writes_so_far = self._writes_in_request(request_id)
                except AuditError:
                    logger.exception("cannot count writes for request %s", request_id)
                    writes_so_far = 10**6  # fail closed: the budget check denies
            evidence = None
            if self.approvals is not None:
                # From the store, keyed by the validated arguments; nothing the caller asserts.
                evidence = self.approvals.evidence(tool=tool, args=args, user=user)
            decision = self.policy.evaluate(
                PolicyInput(
                    tool=tool,
                    user=user,
                    agent=agent,
                    environment=self.environment,
                    approval=evidence,
                    writes_in_request=writes_so_far,
                )
            )
            current.set_attribute("aegisdesk.policy.decision", decision.decision.value)
            current.set_attribute("aegisdesk.policy.reasons", list(decision.reasons))
            current.set_attribute("aegisdesk.policy.version", decision.policy_version)
            if evidence is not None:
                current.set_attribute("aegisdesk.approval.ids", list(evidence.approval_ids))
        if decision.decision is Decision.DENY:
            for reason in decision.reasons:
                instruments().policy_denials.add(1, {"tool": tool, "reason": reason})
        event = AuditEvent(
            phase=AuditPhase.DECISION,
            call_id=str(uuid.uuid4()),
            request_id=request_id,
            trace_id=tracing.current_trace_id(),
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
            approval_id=",".join(evidence.approval_ids)
            if evidence and evidence.approval_ids
            else None,
            approver_id=",".join(evidence.approver_ids)
            if evidence and evidence.approver_ids
            else None,
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

    def _writes_in_request(self, request_id: str) -> int:
        """Writes this gateway already allowed for the request (idempotent repeats included)."""
        return sum(
            1
            for e in self.audit.query(request_id=request_id, limit=10_000)
            if e.phase is AuditPhase.DECISION
            and e.policy_decision == Decision.ALLOW.value
            and e.risk in (ToolRisk.MEDIUM.value, ToolRisk.HIGH.value)
        )

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

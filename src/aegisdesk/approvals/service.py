"""Human decisions on approval steps.

Deterministic rules, checked in this order for every decision:

1. The approval exists and the person may see it (else "not found", so IDs
   cannot be probed).
2. The person may decide this step: the named approver (the requester's
   manager), or a holder of the step's role (`security_approver`,
   `data_owner`, or any `manager` when the requester has none).
3. Separation of duties: never the requester, and never someone who already
   decided another step of the same request.
4. Not expired. An overdue step is marked expired and the request rejected.
5. Idempotent: repeating the same decision returns the recorded one; a
   different decision on a decided step is refused. The store update is
   conditional ("only if still pending"), so two deciders cannot both win.

After each decision the request is finalised when possible (any rejection ->
rejected; every step approved -> approved), and an audit event is written.
Provisioning is *not* done here: the resumed workflow does it, through the
action gateway, which re-checks the approvals itself.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from aegisdesk.audit.events import AuditError, AuditEvent, AuditLog, AuditPhase
from aegisdesk.domain.access import (
    AccessRequest,
    AccessRequestStatus,
    ApprovalRecord,
    ApprovalStatus,
)
from aegisdesk.domain.access_store import AccessStore, is_expired
from aegisdesk.identity.context import UserContext
from aegisdesk.observability import tracing
from aegisdesk.observability.metrics import instruments
from aegisdesk.observability.redaction import pseudonym

logger = logging.getLogger(__name__)

DECISION_ACTION = "approval_decision"


class ApprovalError(PermissionError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class DecisionResult:
    approval: ApprovalRecord
    request: AccessRequest
    # False when the same decision had already been recorded (idempotent repeat).
    changed: bool


class ApprovalService:
    def __init__(self, store: AccessStore, audit: AuditLog, *, environment: str) -> None:
        self._store = store
        self._audit = audit
        self._environment = environment

    # -- queries ------------------------------------------------------------

    def why_not(self, approval: ApprovalRecord, approver: UserContext) -> str | None:
        """None if `approver` may decide this step, else the reason."""
        if approver.employee_id == approval.requester_id:
            return "cannot_approve_own_request"
        if approval.approver_id is not None and approver.employee_id != approval.approver_id:
            return "not_the_approver"
        if approval.approver_role is not None and approval.approver_role not in approver.roles:
            return "missing_approver_role"
        others = self._store.approvals_for_request(approval.access_request_id)
        if any(
            a.approval_id != approval.approval_id and a.decided_by == approver.employee_id
            for a in others
        ):
            return "already_decided_another_step"
        return None

    def list_pending_for(self, approver: UserContext) -> list[ApprovalRecord]:
        return [
            a
            for a in self._store.pending_approvals()
            if not is_expired(a) and self.why_not(a, approver) is None
        ]

    def get(self, approval_id: str, viewer: UserContext) -> ApprovalRecord:
        approval = self._store.get_approval(approval_id)
        if approval is None or not self._may_view(approval, viewer):
            raise ApprovalError("not_found", f"No approval {approval_id}.")
        return approval

    def _may_view(self, approval: ApprovalRecord, viewer: UserContext) -> bool:
        return viewer.employee_id in (approval.requester_id, approval.decided_by) or self.why_not(
            approval, viewer
        ) in (None, "already_decided_another_step")

    # -- decisions ----------------------------------------------------------

    def decide(
        self,
        approval_id: str,
        approver: UserContext,
        *,
        approve: bool,
        comment: str | None = None,
        request_id: str | None = None,
    ) -> DecisionResult:
        with tracing.span(
            "approval.decide",
            **{
                "aegisdesk.approval.id": approval_id,
                "aegisdesk.approval.decision": "approve" if approve else "reject",
                tracing.USER: pseudonym(approver.employee_id),
            },
        ) as current:
            try:
                result = self._decide(
                    approval_id, approver, approve=approve, comment=comment, request_id=request_id
                )
            except ApprovalError as exc:
                tracing.mark_error(current, exc.category)
                raise
            current.set_attribute("aegisdesk.approval.changed", result.changed)
            current.set_attribute(tracing.THREAD_ID, result.approval.thread_id or "")
            current.set_attribute("aegisdesk.access_request.status", result.request.status.value)
            if result.changed and not approve:
                instruments().approval_rejections.add(1, {"reason": "rejected"})
            return result

    def _decide(
        self,
        approval_id: str,
        approver: UserContext,
        *,
        approve: bool,
        comment: str | None,
        request_id: str | None,
    ) -> DecisionResult:
        wanted = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        approval = self._store.get_approval(approval_id)
        if approval is None or not self._may_view(approval, approver):
            raise ApprovalError("not_found", f"No approval {approval_id}.")

        if approval.status is not ApprovalStatus.PENDING:
            return self._repeat(approval, approver, wanted)

        reason = self.why_not(approval, approver)
        if reason is not None:
            self._record(approval, approver, outcome=f"refused:{reason}", request_id=request_id)
            raise ApprovalError(reason, f"You cannot decide {approval_id} ({reason}).")

        if is_expired(approval):
            self.refresh(approval.access_request_id)
            raise ApprovalError("expired", f"{approval_id} has expired; the request was closed.")

        # Write-ahead audit (M11): the decision is recorded before anything changes, and
        # if it cannot be recorded, nothing changes (AuditError: the API answers 503).
        # Same rule as the tool gateway: never act without a record.
        call_id = str(uuid.uuid4())
        self._record(
            approval,
            approver,
            outcome=wanted.value,
            request_id=request_id,
            phase=AuditPhase.DECISION,
            call_id=call_id,
            strict=True,
        )
        changed = self._store.decide_approval(
            approval_id, status=wanted, decided_by=approver.employee_id, comment=comment
        )
        current = self._store.get_approval(approval_id)
        if current is None:
            raise ApprovalError("not_found", f"No approval {approval_id}.")
        if not changed:  # someone else decided first
            return self._repeat(current, approver, wanted)

        request = self.refresh(approval.access_request_id)
        self._record(
            current, approver, outcome=wanted.value, request_id=request_id, call_id=call_id
        )
        return DecisionResult(current, request, changed=True)

    def _repeat(
        self, approval: ApprovalRecord, approver: UserContext, wanted: ApprovalStatus
    ) -> DecisionResult:
        if approval.decided_by == approver.employee_id and approval.status is wanted:
            request = self.refresh(approval.access_request_id)
            return DecisionResult(approval, request, changed=False)
        raise ApprovalError(
            "already_decided", f"{approval.approval_id} is already {approval.status.value}."
        )

    def refresh(self, request_id: str) -> AccessRequest:
        """Expire overdue steps and settle the request's status. Safe to call repeatedly."""
        for approval in self._store.approvals_for_request(request_id):
            if is_expired(approval) and self._store.decide_approval(
                approval.approval_id,
                status=ApprovalStatus.EXPIRED,
                decided_by=None,
                comment="Expired without a decision.",
            ):
                instruments().approval_rejections.add(1, {"reason": "expired"})
        steps = self._store.approvals_for_request(request_id)
        waiting = AccessRequestStatus.AWAITING_APPROVAL
        if any(a.status in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED) for a in steps):
            self._store.set_request_status(
                request_id, status=AccessRequestStatus.REJECTED, expected=waiting
            )
        elif steps and all(a.status is ApprovalStatus.APPROVED for a in steps):
            self._store.set_request_status(
                request_id, status=AccessRequestStatus.APPROVED, expected=waiting
            )
        request = self._store.get_access_request(request_id)
        if request is None:
            raise ApprovalError("not_found", f"No access request {request_id}.")
        return request

    def _record(
        self,
        approval: ApprovalRecord,
        approver: UserContext,
        *,
        outcome: str,
        request_id: str | None,
        phase: AuditPhase = AuditPhase.OUTCOME,
        call_id: str | None = None,
        strict: bool = False,
    ) -> None:
        event = AuditEvent(
            phase=phase,
            call_id=call_id or str(uuid.uuid4()),
            request_id=request_id or f"approval-{uuid.uuid4()}",
            trace_id=tracing.current_trace_id(),
            thread_id=approval.thread_id,
            user_id=approval.requester_id,
            agent_id=None,  # a human decision, not an agent action
            agent_version=None,
            environment=self._environment,
            action=DECISION_ACTION,
            tool="decide_approval",
            risk=None,
            resource={
                "access_request_id": approval.access_request_id,
                "application": approval.application_id,
                "step": approval.step.value,
            },
            policy_decision="human",
            policy_reasons=(),
            policy_version="approval-service",
            approval_id=approval.approval_id,
            approver_id=approver.employee_id,
            outcome=outcome,
        )
        try:
            self._audit.record(event)
        except AuditError:
            if strict:
                raise
            # After the write-ahead event, the approval row (who, when, comment) is the record.
            logger.exception("audit event for %s not recorded", approval.approval_id)

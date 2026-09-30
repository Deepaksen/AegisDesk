"""Approval evidence for the gateway: is this provisioning call backed by human approval?

The gateway asks this before the policy decides a HIGH-risk call. The answer
comes only from the access store: the request must belong to the calling
user, be fully approved (or auto-approved by application policy), and not be
provisioned yet. The model cannot produce evidence: it can neither call
`provision_access` nor write approval records.
"""

from __future__ import annotations

from typing import Any

from aegisdesk.domain.access import AccessRequestStatus, ApprovalStatus
from aegisdesk.domain.access_store import AccessStore
from aegisdesk.governance.policy import ApprovalEvidence
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.provisioning import PROVISION_TOOL


class AccessApprovalVerifier:
    def __init__(self, store: AccessStore) -> None:
        self._store = store

    def evidence(
        self, *, tool: str, args: dict[str, Any], user: UserContext
    ) -> ApprovalEvidence | None:
        if tool != PROVISION_TOOL:
            return None
        request = self._store.get_access_request(str(args.get("access_request_id", "")))
        if request is None or request.employee_id != user.employee_id:
            return None
        if request.provisioned_at is not None:
            return None
        if request.status is AccessRequestStatus.AUTO_APPROVED:
            return ApprovalEvidence((), (), automatic=True)
        if request.status is not AccessRequestStatus.APPROVED:
            return None
        approvals = self._store.approvals_for_request(request.request_id)
        if not approvals or any(a.status is not ApprovalStatus.APPROVED for a in approvals):
            return None
        return ApprovalEvidence(
            approval_ids=tuple(a.approval_id for a in approvals),
            approver_ids=tuple(a.decided_by or "" for a in approvals),
        )

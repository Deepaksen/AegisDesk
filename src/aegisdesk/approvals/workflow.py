"""The approval workflow the graph runs after an access request (Milestone 7).

Deterministic code, called by graph nodes, never by a model:

* `requests_in(trajectory)` - access requests this turn created (from tool results).
* `waiting(ids)`            - still waiting for a human? (expires overdue steps first)
* `pending(ids)`            - what the pause is waiting for (the interrupt payload).
* `settle(ids, ...)`        - provision approved / auto-approved requests through
                              the governed `provision_access` tool, and report
                              rejected or expired ones.

The workflow reads the store every time. It never trusts what a resume
message says, what the model wrote, or what the state claims: the store's
approval records are the only source of truth.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from aegisdesk.approvals.service import ApprovalService
from aegisdesk.domain.access import AccessRequest, AccessRequestStatus, ApprovalStatus
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.executor import OutcomeStatus, ToolRunner
from aegisdesk.tools.provisioning import PROVISION_TOOL

logger = logging.getLogger(__name__)

WORKFLOW_AGENT_ID = "access_workflow"
WORKFLOW_VERSION = "0.1.0"
CREATE_TOOL = "create_access_request"
_TRACKED = {AccessRequestStatus.AWAITING_APPROVAL.value, AccessRequestStatus.AUTO_APPROVED.value}


def workflow_identity(environment: str) -> AgentIdentity:
    return AgentIdentity(WORKFLOW_AGENT_ID, WORKFLOW_VERSION, "workflow", environment)


@dataclass(frozen=True)
class Settlement:
    messages: list[str]
    trajectory: list[dict[str, Any]]


class AccessApprovalWorkflow:
    def __init__(
        self,
        repository: ServiceDeskRepository,
        service: ApprovalService,
        provisioner: ToolRunner,
    ) -> None:
        self._repository = repository
        self._store = repository.access_store
        self._service = service
        self._provisioner = provisioner

    @staticmethod
    def requests_in(trajectory: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for entry in trajectory:
            if entry.get("kind") != "tool" or entry.get("tool_name") != CREATE_TOOL:
                continue
            if entry.get("status") != OutcomeStatus.OK.value:
                continue
            try:
                result = json.loads(entry["result"])
            except (ValueError, KeyError):
                continue
            if result.get("status") in _TRACKED and result.get("request_id") not in found:
                found.append(str(result["request_id"]))
        return found

    def _requests(self, request_ids: list[str]) -> list[AccessRequest]:
        return [self._service.refresh(r) for r in request_ids]

    def waiting(self, request_ids: list[str]) -> bool:
        return any(r.status.is_open for r in self._requests(request_ids))

    def pending(self, request_ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "approval_id": a.approval_id,
                "access_request_id": a.access_request_id,
                "step": a.step.value,
                "approver": a.approver_id or f"any {a.approver_role}",
            }
            for request_id in request_ids
            for a in self._store.approvals_for_request(request_id)
            if a.status is ApprovalStatus.PENDING
        ]

    def settle(
        self,
        request_ids: list[str],
        *,
        user: UserContext,
        request_id: str,
        thread_id: str | None,
    ) -> Settlement:
        messages: list[str] = []
        trajectory: list[dict[str, Any]] = []
        for request in self._requests(request_ids):
            app = self._repository.get_application(request.application_id)
            label = f"{request.request_id} ({app.name if app else request.application_id})"
            approvals = self._store.approvals_for_request(request.request_id)
            decided = ", ".join(
                f"{a.step.value.replace('_', ' ')}: {self._name(a.decided_by)} ({a.approval_id})"
                for a in approvals
                if a.status is ApprovalStatus.APPROVED
            )
            if request.status is AccessRequestStatus.REJECTED:
                closed = [a for a in approvals if a.status is not ApprovalStatus.APPROVED]
                why = "; ".join(
                    f"{a.approval_id} {a.status.value}"
                    + (f" by {self._name(a.decided_by)}" if a.decided_by else "")
                    + (f": {a.comment}" if a.comment else "")
                    for a in closed
                )
                messages.append(f"Update on {label}: not approved ({why}). No access was granted.")
                continue
            if request.status.is_open:
                continue  # still waiting; the graph pauses again

            outcome = self._provisioner.execute(
                PROVISION_TOOL,
                {"access_request_id": request.request_id},
                user=user,
                request_id=request_id,
                thread_id=thread_id,
            )
            trajectory.append(
                {
                    "kind": "tool",
                    "step": 0,
                    "agent": WORKFLOW_AGENT_ID,
                    "tool_name": PROVISION_TOOL,
                    "args": {"access_request_id": request.request_id},
                    "status": outcome.status.value,
                    "latency_ms": outcome.latency_ms,
                    "error_category": outcome.error_category,
                    "result": outcome.content,
                }
            )
            if outcome.status is OutcomeStatus.OK:
                result = json.loads(outcome.content)
                expiry = (
                    f" It expires on {result['expires_on']}." if result.get("expires_on") else ""
                )
                how = (
                    f"approved by {decided}"
                    if decided
                    else "standard application, no approval needed"
                )
                messages.append(f"Update on {label}: {how}. Access has been granted.{expiry}")
            else:
                logger.error(
                    "provisioning %s failed: %s", request.request_id, outcome.error_category
                )
                messages.append(
                    f"Update on {label}: approved, but access could not be granted automatically "
                    f"({outcome.error_category}). The service desk will follow up."
                )
        return Settlement(messages, trajectory)

    def _name(self, employee_id: str | None) -> str:
        if employee_id is None:
            return "nobody"
        employee = self._repository.get_employee(employee_id)
        return f"{employee.name} ({employee_id})" if employee else employee_id

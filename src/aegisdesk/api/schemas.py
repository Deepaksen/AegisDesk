"""Request and response models of the public API (they are the OpenAPI contract).

Request bodies forbid unknown fields, and none of them has an identity field:
who is calling comes from authentication only, so `{"employee_id": "E1010"}`
in a body is a 422, not an impersonation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aegisdesk.domain.access import ApprovalRecord
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.runtime import ThreadView, TurnResult


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageRequest(_Request):
    text: str = Field(min_length=1, max_length=10_000)


class DecisionRequest(_Request):
    comment: str | None = Field(default=None, max_length=500)


class Me(BaseModel):
    employee_id: str
    name: str
    department: str
    roles: list[str]
    manager_id: str | None


class ThreadCreated(BaseModel):
    thread_id: str


class ActivityOut(BaseModel):
    text: str
    status: str


class CitationOut(BaseModel):
    document_id: str
    title: str


class PendingApprovalOut(BaseModel):
    approval_id: str
    access_request_id: str
    step: str
    approver: str


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int


class MessageResponse(BaseModel):
    thread_id: str
    request_id: str
    trace_id: str | None
    answer: str
    stop_reason: str
    activity: list[ActivityOut]
    citations: list[CitationOut]
    references: list[str]
    pending_approvals: list[PendingApprovalOut]
    usage: UsageOut
    latency_ms: float

    @classmethod
    def of(cls, turn: TurnResult) -> MessageResponse:
        return cls(
            thread_id=turn.thread_id,
            request_id=turn.request_id,
            trace_id=turn.trace_id,
            answer=turn.answer,
            stop_reason=turn.stop_reason,
            activity=[ActivityOut(text=a.text, status=a.status) for a in turn.activity],
            citations=[
                CitationOut(document_id=c.document_id, title=c.title) for c in turn.citations
            ],
            references=turn.references,
            pending_approvals=[PendingApprovalOut(**p) for p in turn.pending_approvals],
            usage=UsageOut(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens),
            latency_ms=turn.latency_ms,
        )


class ThreadMessageOut(BaseModel):
    role: str
    text: str


class ThreadResponse(BaseModel):
    thread_id: str
    messages: list[ThreadMessageOut]
    pending_approvals: list[PendingApprovalOut]

    @classmethod
    def of(cls, view: ThreadView) -> ThreadResponse:
        return cls(
            thread_id=view.thread_id,
            messages=[ThreadMessageOut(role=m.role, text=m.text) for m in view.messages],
            pending_approvals=[PendingApprovalOut(**p) for p in view.pending_approvals],
        )


class ApprovalOut(BaseModel):
    approval_id: str
    access_request_id: str
    requester_id: str
    requester_name: str | None
    application_id: str
    application_name: str | None
    step: str
    status: str
    approver: str
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    comment: str | None

    @classmethod
    def of(cls, a: ApprovalRecord, repository: ServiceDeskRepository) -> ApprovalOut:
        requester = repository.get_employee(a.requester_id)
        app = repository.get_application(a.application_id)
        return cls(
            approval_id=a.approval_id,
            access_request_id=a.access_request_id,
            requester_id=a.requester_id,
            requester_name=requester.name if requester else None,
            application_id=a.application_id,
            application_name=app.name if app else None,
            step=a.step.value,
            status=a.status.value,
            approver=a.approver_id or f"any {a.approver_role}",
            requested_at=a.requested_at,
            expires_at=a.expires_at,
            decided_by=a.decided_by,
            decided_at=a.decided_at,
            comment=a.comment,
        )


class DecisionResponse(BaseModel):
    approval: ApprovalOut
    request_status: str
    changed: bool
    resumed: MessageResponse | None
    note: str | None


class AuditResponse(BaseModel):
    events: list[dict[str, Any]]


class Problem(BaseModel):
    """RFC 9457 problem details."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    category: str | None = None
    request_id: str | None = None
    trace_id: str | None = None

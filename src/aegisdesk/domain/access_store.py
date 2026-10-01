"""Where the mutable access domain lives: access records, requests, approvals.

Milestone 7 needs this state to outlive the process: a request waits for a
manager for hours or days, across restarts. Two implementations share one
protocol and one contract test suite:

* `InMemoryAccessStore` - seeded per process; tests and demos.
* `PgAccessStore` (`access_store_pg.py`) - PostgreSQL (migration 0003).

Every state change is a conditional update ("only if still pending", "only
if not yet provisioned"), so two concurrent deciders, or a retried resume,
cannot apply the same change twice.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from aegisdesk.domain.access import (
    AccessRecord,
    AccessRequest,
    AccessRequestStatus,
    Approval,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalStep,
)
from aegisdesk.observability import faults
from aegisdesk.reliability.errors import StoreUnavailableError, is_database_outage


class AccessStore(Protocol):
    def access_for(self, employee_id: str) -> list[AccessRecord]: ...

    def access_requests_for(self, employee_id: str) -> list[AccessRequest]: ...

    def get_access_request(self, request_id: str) -> AccessRequest | None: ...

    def access_request_for_key(self, idempotency_key: str) -> AccessRequest | None: ...

    def create_access_request(
        self,
        *,
        employee_id: str,
        application_id: str,
        approvals_required: list[Approval],
        steps: list[ApprovalStep],
        justification: str,
        idempotency_key: str,
        thread_id: str | None,
        approval_ttl: timedelta,
    ) -> tuple[AccessRequest, bool]: ...

    def approvals_for_request(self, request_id: str) -> list[ApprovalRecord]: ...

    def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    def pending_approvals(self) -> list[ApprovalRecord]: ...

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        decided_by: str | None,
        comment: str | None,
    ) -> bool:
        """Record a decision only if the approval is still pending. True if it changed."""
        ...

    def set_request_status(
        self, request_id: str, *, status: AccessRequestStatus, expected: AccessRequestStatus
    ) -> bool:
        """Change status only from `expected`. True if it changed."""
        ...

    def provision(self, request_id: str, record: AccessRecord) -> bool:
        """Grant access for a request, once. True if granted now, False if already done."""
        ...


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryAccessStore:
    def __init__(
        self,
        access: list[AccessRecord] | None = None,
        access_requests: list[AccessRequest] | None = None,
        approvals: list[ApprovalRecord] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._access = list(access or [])
        self._requests = {r.request_id: r for r in access_requests or []}
        self._approvals = {a.approval_id: a for a in approvals or []}
        self._request_keys: dict[str, str] = {}

    def access_for(self, employee_id: str) -> list[AccessRecord]:
        with self._lock:
            return [a for a in self._access if a.employee_id == employee_id]

    def access_requests_for(self, employee_id: str) -> list[AccessRequest]:
        with self._lock:
            mine = [r for r in self._requests.values() if r.employee_id == employee_id]
        return sorted(mine, key=lambda r: r.request_id)

    def get_access_request(self, request_id: str) -> AccessRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def access_request_for_key(self, idempotency_key: str) -> AccessRequest | None:
        with self._lock:
            request_id = self._request_keys.get(idempotency_key)
            return self._requests[request_id] if request_id else None

    def create_access_request(
        self,
        *,
        employee_id: str,
        application_id: str,
        approvals_required: list[Approval],
        steps: list[ApprovalStep],
        justification: str,
        idempotency_key: str,
        thread_id: str | None,
        approval_ttl: timedelta,
    ) -> tuple[AccessRequest, bool]:
        with self._lock:
            existing = self._request_keys.get(idempotency_key)
            if existing is not None:
                return self._requests[existing], False
            highest = max((int(r.split("-")[1]) for r in self._requests), default=1000)
            now = _now()
            request = AccessRequest(
                request_id=f"AR-{highest + 1}",
                employee_id=employee_id,
                application_id=application_id,
                status=(
                    AccessRequestStatus.AWAITING_APPROVAL
                    if approvals_required
                    else AccessRequestStatus.AUTO_APPROVED
                ),
                approvals_required=approvals_required,
                created_at=now,
                justification=justification,
                thread_id=thread_id,
            )
            self._requests[request.request_id] = request
            self._request_keys[idempotency_key] = request.request_id
            for step in steps:
                approval_id = f"AP-{len(self._approvals) + 1:04d}"
                self._approvals[approval_id] = ApprovalRecord(
                    approval_id=approval_id,
                    access_request_id=request.request_id,
                    requester_id=employee_id,
                    application_id=application_id,
                    step=step.step,
                    approver_id=step.approver_id,
                    approver_role=step.approver_role,
                    status=ApprovalStatus.PENDING,
                    thread_id=thread_id,
                    requested_at=now,
                    expires_at=now + approval_ttl,
                )
            return request, True

    def approvals_for_request(self, request_id: str) -> list[ApprovalRecord]:
        with self._lock:
            found = [a for a in self._approvals.values() if a.access_request_id == request_id]
        return sorted(found, key=lambda a: a.approval_id)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._approvals.get(approval_id)

    def pending_approvals(self) -> list[ApprovalRecord]:
        with self._lock:
            pending = [a for a in self._approvals.values() if a.status is ApprovalStatus.PENDING]
        return sorted(pending, key=lambda a: a.approval_id)

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        decided_by: str | None,
        comment: str | None,
    ) -> bool:
        with self._lock:
            current = self._approvals.get(approval_id)
            if current is None or current.status is not ApprovalStatus.PENDING:
                return False
            self._approvals[approval_id] = current.model_copy(
                update={
                    "status": status,
                    "decided_by": decided_by,
                    "decided_at": _now(),
                    "comment": comment,
                }
            )
            return True

    def set_request_status(
        self, request_id: str, *, status: AccessRequestStatus, expected: AccessRequestStatus
    ) -> bool:
        with self._lock:
            current = self._requests.get(request_id)
            if current is None or current.status is not expected:
                return False
            self._requests[request_id] = current.model_copy(update={"status": status})
            return True

    def provision(self, request_id: str, record: AccessRecord) -> bool:
        with self._lock:
            current = self._requests.get(request_id)
            if current is None or current.provisioned_at is not None:
                return False
            self._requests[request_id] = current.model_copy(update={"provisioned_at": _now()})
            self._access.append(record)
            return True


def is_expired(approval: ApprovalRecord) -> bool:
    return approval.status is ApprovalStatus.PENDING and approval.expires_at <= _now()


class GuardedAccessStore:
    """Wraps any AccessStore so a database outage is a `StoreUnavailableError` (M11).

    Callers (tools, the approval service) then report "unavailable" instead of
    an unexpected driver exception. `AEGIS_FAULTS=db_error:access` simulates the
    outage for every access-store call, reads and writes alike.
    """

    def __init__(self, inner: AccessStore) -> None:
        self._inner = inner

    @property
    def inner(self) -> AccessStore:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute) or name.startswith("_"):
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if faults.active("db_error", "access"):
                raise StoreUnavailableError("access")
            try:
                return attribute(*args, **kwargs)
            except Exception as exc:
                if is_database_outage(exc):
                    raise StoreUnavailableError("access") from exc
                raise

        return guarded


def guarded(store: AccessStore) -> AccessStore:
    if isinstance(store, GuardedAccessStore):
        return cast(AccessStore, store)
    return cast(AccessStore, GuardedAccessStore(store))

"""Audit events and where they are stored.

One tool call produces two events, linked by `call_id`:

* **decision** - written *before* the tool runs: who (user, agent, version,
  environment), what (tool, resource), and the policy decision with reasons.
* **outcome**  - written after: ok, or the error category, and the latency.

Writing the decision first means an action can never happen without a
record of it being authorized. If that write fails for a write tool, the
gateway refuses the call.

Events hold identifiers, not free text: the resource is the IDs taken from
the validated arguments (ticket, application, document), never a ticket
description or a justification.

Stores are append-only: `record` and `query`, no update or delete. The
PostgreSQL table also rejects UPDATE, DELETE and TRUNCATE with triggers
(migration 0002), so not even a bug in this code can rewrite history.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class AuditPhase(StrEnum):
    DECISION = "decision"
    OUTCOME = "outcome"


def _now() -> datetime:
    return datetime.now(UTC)


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = Field(default_factory=_now)
    phase: AuditPhase
    # Links the decision and outcome events of one tool call.
    call_id: str

    request_id: str
    trace_id: str | None = None  # filled by tracing (Milestone 8)
    thread_id: str | None = None

    user_id: str
    agent_id: str | None
    agent_version: str | None
    environment: str

    action: str = "tool_call"
    tool: str
    risk: str | None
    resource: dict[str, str] = Field(default_factory=dict)

    policy_decision: str
    policy_reasons: tuple[str, ...] = ()
    policy_version: str

    approval_id: str | None = None  # Milestone 7
    approver_id: str | None = None  # Milestone 7

    # decision phase: the decision; outcome phase: "ok" or the error category.
    outcome: str
    latency_ms: float | None = None


class AuditError(RuntimeError):
    """The audit store could not record an event."""


class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> None: ...

    def query(
        self,
        *,
        request_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]: ...


class InMemoryAuditLog:
    """Per-process store for tests and demos. Lost when the process exits."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def query(
        self,
        *,
        request_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        with self._lock:
            events = [
                e
                for e in self._events
                if (request_id is None or e.request_id == request_id)
                and (user_id is None or e.user_id == user_id)
            ]
        return events[-limit:]

    @property
    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)


def resource_ids(args: dict[str, Any]) -> dict[str, str]:
    """The identifiers in a tool's validated arguments; no free text."""
    keys = ("ticket_id", "application", "document_id")
    return {k: str(args[k]) for k in keys if args.get(k) is not None}

"""Milestone 11: approved access that was never provisioned is finished later, once."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.observability import faults
from aegisdesk.runtime import AegisRuntime, Reconciled

FINANCE_ERP = "Please create an access request for FinanceERP for month-end reporting"


@pytest.fixture
def runtime(repository: ServiceDeskRepository) -> Iterator[AegisRuntime]:
    with AegisRuntime.open(Settings(model_retry_backoff_seconds=0.0), repository=repository) as rt:
        yield rt


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def set_faults(spec: str) -> None:
        monkeypatch.setenv(faults.ENV, spec)
        faults.reload()

    yield set_faults
    monkeypatch.delenv(faults.ENV, raising=False)
    faults.reload()


def _granted(repository: ServiceDeskRepository) -> bool:
    return any(a.application_id == "APP-FIN" for a in repository.access_for("E1004"))


def test_nothing_to_do_ignores_legacy_seed_requests(runtime: AegisRuntime) -> None:
    # The seed has approved requests without provisioning records (pre-workflow data).
    assert runtime.reconcile() == []


def test_failed_provisioning_is_completed_by_reconcile(
    runtime: AegisRuntime, repository: ServiceDeskRepository, aisha: UserContext, inject: Any
) -> None:
    turn = runtime.send_message(aisha, "t-1", FINANCE_ERP)
    approval_id = turn.pending_approvals[0]["approval_id"]
    grace = authenticate(repository, "E1010")

    inject("tool_error:provision_access")  # provisioning breaks when it runs
    decision = runtime.decide(grace, approval_id, approve=True, comment="ok")
    assert decision.resumed is not None
    assert "could not be granted automatically" in decision.resumed.answer
    assert not _granted(repository)

    inject("")
    [done] = runtime.reconcile()

    assert (done.action, done.detail) == ("provisioned", None) and _granted(repository)
    assert runtime.reconcile() == []  # idempotent: nothing left


def test_a_decision_without_a_resume_is_resumed_by_reconcile(
    runtime: AegisRuntime, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    turn = runtime.send_message(aisha, "t-2", FINANCE_ERP)
    approval_id = turn.pending_approvals[0]["approval_id"]
    # The process "crashed" after recording the decision, before resuming the thread.
    runtime.approvals.decide(approval_id, authenticate(repository, "E1010"), approve=True)

    [done] = runtime.reconcile()

    assert isinstance(done, Reconciled) and done.action == "resumed"
    assert "Access has been granted" in (done.detail or "") and _granted(repository)
    messages = runtime.thread_view(aisha, "t-2")
    assert messages is not None and "Access has been granted" in messages.messages[-1].text

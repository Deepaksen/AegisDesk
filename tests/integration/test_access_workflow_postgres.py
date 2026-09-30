"""The access store contract (memory and PostgreSQL) and a real multi-process restart.

The PostgreSQL cases need AEGIS_TEST_DATABASE_URL pointing at a migrated
database (CI provides one). They reset the three access-workflow tables and
reseed them, so point it at a database whose access data you can lose.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.access import (
    AccessRecord,
    AccessRequestStatus,
    Approval,
    ApprovalStatus,
    ApprovalStep,
)
from aegisdesk.domain.access_store import AccessStore
from aegisdesk.domain.repository import ServiceDeskRepository

TEST_DB_URL = os.environ.get("AEGIS_TEST_DATABASE_URL")
SEED = PROJECT_ROOT / "data" / "seed"


def _seed_rows() -> tuple[list[AccessRecord], list]:  # type: ignore[type-arg]
    seed = ServiceDeskRepository.from_seed(SEED)
    ids = seed.list_employee_ids()
    return (
        [a for e in ids for a in seed.access_for(e)],
        [r for e in ids for r in seed.access_requests_for(e)],
    )


def _reset_postgres(url: str) -> None:
    from aegisdesk.domain.access_store_pg import PgAccessStore

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE employee_access, approvals, access_requests"))
        conn.execute(text("ALTER SEQUENCE access_request_seq RESTART WITH 1001"))
        conn.execute(text("ALTER SEQUENCE approval_seq RESTART WITH 1"))
    access, requests = _seed_rows()
    PgAccessStore(engine=engine).seed(access, requests)
    engine.dispose()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> AccessStore:
    if request.param == "memory":
        return ServiceDeskRepository.from_seed(SEED).access_store
    if not TEST_DB_URL:
        pytest.skip("AEGIS_TEST_DATABASE_URL not set")
    from aegisdesk.domain.access_store_pg import PgAccessStore

    _reset_postgres(TEST_DB_URL)
    return PgAccessStore(TEST_DB_URL)


STEPS = [
    ApprovalStep(step=Approval.MANAGER, approver_id="E1010", approver_role=None),
    ApprovalStep(step=Approval.SECURITY, approver_id=None, approver_role="security_approver"),
]


def _create(store: AccessStore, key: str = "k-1") -> tuple[str, bool]:
    request, created = store.create_access_request(
        employee_id="E1004",
        application_id="APP-FIN",
        approvals_required=[Approval.MANAGER, Approval.SECURITY],
        steps=STEPS,
        justification="Month-end reporting",
        idempotency_key=key,
        thread_id="t-1",
        approval_ttl=timedelta(days=7),
    )
    return request.request_id, created


def test_create_records_the_request_and_its_steps(store: AccessStore) -> None:
    request_id, created = _create(store)

    request = store.get_access_request(request_id)
    assert created and request_id == "AR-1013"  # continues after the seeded requests
    assert request is not None and request.status is AccessRequestStatus.AWAITING_APPROVAL
    assert request.thread_id == "t-1" and request.provisioned_at is None
    approvals = store.approvals_for_request(request_id)
    assert [(a.approval_id, a.step, a.approver_id, a.approver_role) for a in approvals] == [
        ("AP-0001", Approval.MANAGER, "E1010", None),
        ("AP-0002", Approval.SECURITY, None, "security_approver"),
    ]
    assert all(a.status is ApprovalStatus.PENDING and a.thread_id == "t-1" for a in approvals)
    assert {a.approval_id for a in store.pending_approvals()} >= {"AP-0001", "AP-0002"}


def test_create_is_idempotent(store: AccessStore) -> None:
    first, _ = _create(store)
    again, created = _create(store)

    assert again == first and not created
    assert len(store.approvals_for_request(first)) == 2
    assert store.access_request_for_key("k-1") is not None


def test_decisions_and_status_changes_are_conditional(store: AccessStore) -> None:
    request_id, _ = _create(store)

    assert store.decide_approval(
        "AP-0001", status=ApprovalStatus.APPROVED, decided_by="E1010", comment="ok"
    )
    assert not store.decide_approval(
        "AP-0001", status=ApprovalStatus.REJECTED, decided_by="E1010", comment="changed my mind"
    )
    decided = store.get_approval("AP-0001")
    assert decided is not None and decided.status is ApprovalStatus.APPROVED
    assert decided.comment == "ok" and decided.decided_at is not None

    waiting = AccessRequestStatus.AWAITING_APPROVAL
    approved = AccessRequestStatus.APPROVED
    assert store.set_request_status(request_id, status=approved, expected=waiting)
    assert not store.set_request_status(request_id, status=approved, expected=waiting)


def test_provisioning_happens_once(store: AccessStore) -> None:
    request_id, _ = _create(store)
    record = AccessRecord(
        employee_id="E1004",
        application_id="APP-FIN",
        role="user",
        granted_on=date(2026, 9, 30),
        expires_on=None,
    )

    assert store.provision(request_id, record)
    assert not store.provision(request_id, record)

    request = store.get_access_request(request_id)
    assert request is not None and request.provisioned_at is not None
    assert [a.application_id for a in store.access_for("E1004")].count("APP-FIN") == 1


def test_concurrent_deciders_cannot_both_win(store: AccessStore) -> None:
    _create(store)
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def decide(i: int) -> None:
        barrier.wait()
        results.append(
            store.decide_approval(
                "AP-0001", status=ApprovalStatus.APPROVED, decided_by=f"E{i}", comment=None
            )
        )

    threads = [threading.Thread(target=decide, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1


def test_seed_is_idempotent(store: AccessStore) -> None:
    if not hasattr(store, "seed"):
        pytest.skip("seeding is a PostgreSQL concern")
    access, requests = _seed_rows()
    assert store.seed(access, requests) == 0


# -- a real restart: three separate processes ------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    if not TEST_DB_URL:
        pytest.skip("AEGIS_TEST_DATABASE_URL not set")
    from langgraph.checkpoint.postgres import PostgresSaver

    from aegisdesk.persistence.factory import psycopg_url

    _reset_postgres(TEST_DB_URL)
    with PostgresSaver.from_conn_string(psycopg_url(TEST_DB_URL)) as saver:
        saver.setup()  # idempotent; `aegisdesk db init` does this in real setups
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DB_URL,
        "DATA_STORE": "postgres",
        "CHECKPOINT_STORE": "postgres",
        "AUDIT_STORE": "postgres",
        "CHECKPOINT_DB_PATH": str(tmp_path / "unused.sqlite"),
    }
    yield env


def _cli(env: dict[str, str], *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter + our CLI
        [sys.executable, "-m", "aegisdesk.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_approval_resumes_the_workflow_in_another_process(cli_env: dict[str, str]) -> None:
    # Process 1: the employee asks; the workflow pauses and the process exits.
    first = _cli(
        cli_env,
        "agent",
        "--as",
        "E1004",
        "--quiet",
        "Please create an access request for FinanceERP for month-end reporting",
    )
    approval_id = re.search(r"Waiting for approval: (AP-\d+)", first)
    thread_id = re.search(r"Thread ([0-9a-f-]{36})", first)
    assert approval_id and thread_id, first

    # Process 2: the manager sees it and approves; the paused workflow resumes here.
    listed = _cli(cli_env, "approvals", "list", "--as", "E1010")
    assert approval_id.group(1) in listed
    decided = _cli(
        cli_env, "approvals", "approve", approval_id.group(1), "--as", "E1010", "--comment", "ok"
    )
    assert "Resumed thread" in decided and "Access has been granted" in decided

    # Process 3: the employee's conversation shows the outcome.
    thread = _cli(cli_env, "thread", thread_id.group(1), "--as", "E1004")
    assert "Access has been granted" in thread.splitlines()[-1]

    audit = _cli(cli_env, "audit", "--user", "E1004", "--limit", "20")
    assert re.search(
        r"outcome +provision_access .*decision=allow outcome=ok .*approval=AP-\d+ approver=E1010",
        audit,
    ), audit

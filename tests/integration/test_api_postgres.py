"""Milestone 10: the API on PostgreSQL stores, as in the compose stack.

Two separate app instances (two replicas, or one before and after a restart)
share only the database: the employee's request goes through one, the
manager's approval through the other, and the paused workflow resumes from the
PostgreSQL checkpoint. Needs AEGIS_TEST_DATABASE_URL (a migrated database whose
access data may be reset).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aegisdesk.api.app import create_app
from aegisdesk.config import AuditStoreKind, CheckpointStoreKind, DataStoreKind, Settings
from tests.integration.test_access_workflow_postgres import _reset_postgres

TEST_DB_URL = os.environ.get("AEGIS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="AEGIS_TEST_DATABASE_URL not set")


def _settings() -> Settings:
    assert TEST_DB_URL
    return Settings(
        database_url=TEST_DB_URL,
        data_store=DataStoreKind.POSTGRES,
        audit_store=AuditStoreKind.POSTGRES,
        checkpoint_store=CheckpointStoreKind.POSTGRES,
    )


def test_request_on_one_replica_is_approved_and_resumed_on_another() -> None:
    assert TEST_DB_URL
    _reset_postgres(TEST_DB_URL)
    employee_app = create_app(_settings(), configure_observability=False)
    manager_app = create_app(_settings(), configure_observability=False)

    with TestClient(employee_app) as employee, TestClient(manager_app) as manager:
        assert employee.get("/ready").json()["database"] == "ok"
        aisha = {"X-Employee-Id": "E1004"}
        thread = employee.post("/api/v1/threads", headers=aisha).json()["thread_id"]
        paused = employee.post(
            f"/api/v1/threads/{thread}/messages",
            json={"text": "Please create an access request for FinanceERP for month-end"},
            headers=aisha,
        ).json()
        approval_id = paused["pending_approvals"][0]["approval_id"]

        decided = manager.post(
            f"/api/v1/approvals/{approval_id}/approve",
            json={"comment": "ok"},
            headers={"X-Employee-Id": "E1010"},
        ).json()

        assert "Access has been granted" in decided["resumed"]["answer"]
        view = employee.get(f"/api/v1/threads/{thread}", headers=aisha).json()
        assert "Access has been granted" in view["messages"][-1]["text"]
        events = employee.get(
            "/api/v1/audit", params={"request_id": paused["request_id"]}, headers=aisha
        ).json()["events"]
        assert any(e["tool"] == "provision_access" and e["outcome"] == "ok" for e in events)

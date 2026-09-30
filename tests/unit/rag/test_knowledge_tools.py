"""Knowledge-base tools through the ToolExecutor."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor
from aegisdesk.tools.knowledge import UNTRUSTED_NOTE, build_knowledge_tools


@pytest.fixture
def executor(retriever: Retriever) -> ToolExecutor:
    return ToolExecutor(build_knowledge_tools(retriever))


def _run(
    executor: ToolExecutor, user: UserContext, name: str, args: dict[str, Any]
) -> tuple[OutcomeStatus, dict[str, Any]]:
    outcome = executor.execute(name, args, user=user, request_id="r")
    return outcome.status, json.loads(outcome.content)


def test_search_returns_citable_passages(executor: ToolExecutor, aisha: UserContext) -> None:
    status, body = _run(executor, aisha, "search_knowledge_base", {"query": "error GP-512"})

    assert status is OutcomeStatus.OK
    assert body["sufficient_evidence"] is True
    first = body["passages"][0]
    assert (first["chunk_id"], first["version"]) == ("DOC-VPN-001#05", "2.4")
    assert body["note"] == UNTRUSTED_NOTE


def test_search_reports_insufficient_evidence(executor: ToolExecutor, aisha: UserContext) -> None:
    _, body = _run(executor, aisha, "search_knowledge_base", {"query": "canteen menu friday"})
    assert body == {"sufficient_evidence": False, "passages": [], "note": UNTRUSTED_NOTE}


def test_search_cannot_be_widened_by_the_model(executor: ToolExecutor, aisha: UserContext) -> None:
    status, body = _run(
        executor,
        aisha,
        "search_knowledge_base",
        {"query": "production database", "roles": ["it_admin"], "top_k": 50},
    )
    assert status is OutcomeStatus.ERROR
    assert body["error"]["category"] == "invalid_arguments"


def test_retrieve_document_respects_access(
    executor: ToolExecutor, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    status, body = _run(executor, aisha, "retrieve_document", {"document_id": "DOC-VPN-001"})
    assert status is OutcomeStatus.OK
    assert [s["chunk_id"] for s in body["sections"]][:2] == ["DOC-VPN-001#01", "DOC-VPN-001#02"]

    _, restricted = _run(executor, aisha, "retrieve_document", {"document_id": "DOC-PDB-001"})
    _, missing = _run(executor, aisha, "retrieve_document", {"document_id": "DOC-XYZ-999"})
    assert restricted["error"]["category"] == missing["error"]["category"] == "not_found"

    admin = authenticate(repository, "E1006")
    status, _ = _run(executor, admin, "retrieve_document", {"document_id": "DOC-PDB-001"})
    assert status is OutcomeStatus.OK

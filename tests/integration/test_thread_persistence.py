"""Durable threads: a conversation survives a process restart, and stays private.

A "restart" here is real in every way that matters: the graph, the agent, the
model and the SQLite connection are all discarded, and new ones are built
against the same database file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from aegisdesk.agents.service_desk import build_service_desk_graph_agent
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.graphs.service_desk_graph import ThreadAccessError
from aegisdesk.identity.context import authenticate
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.persistence.checkpointer import sqlite_checkpointer


def _process(*script: AIMessage) -> tuple[ServiceDeskRepository, ScriptedChatModel]:
    """Everything a fresh process would build from scratch."""
    return (
        ServiceDeskRepository.from_seed(Settings().seed_data_dir),
        ScriptedChatModel(responses=list(script)),
    )


def test_thread_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "checkpoints.sqlite"
    settings = Settings()

    repository, model = _process(AIMessage(content="Noted: VPN drops every 10 minutes."))
    with sqlite_checkpointer(db) as checkpointer:
        agent = build_service_desk_graph_agent(
            settings, repository, checkpointer=checkpointer, model=model
        )
        agent.run(
            "My VPN drops every 10 minutes.",
            user=authenticate(repository, "E1004"),
            thread_id="t-restart",
        )

    # --- restart: nothing from above is reused except the file on disk ---
    repository, model = _process(AIMessage(content="You told me your VPN drops."))
    with sqlite_checkpointer(db) as checkpointer:
        agent = build_service_desk_graph_agent(
            settings, repository, checkpointer=checkpointer, model=model
        )
        run = agent.run(
            "What did I tell you?", user=authenticate(repository, "E1004"), thread_id="t-restart"
        )

    sent = [(m.type, m.text) for m in model.calls[0]]
    assert ("human", "My VPN drops every 10 minutes.") in sent
    assert ("ai", "Noted: VPN drops every 10 minutes.") in sent
    assert [m.type for m in run.history] == ["human", "ai", "human", "ai"]


def test_another_employee_cannot_read_or_extend_a_thread(tmp_path: Path) -> None:
    db = tmp_path / "checkpoints.sqlite"
    settings = Settings()
    repository, model = _process(AIMessage(content="ok"))

    with sqlite_checkpointer(db) as checkpointer:
        agent = build_service_desk_graph_agent(
            settings, repository, checkpointer=checkpointer, model=model
        )
        owner = authenticate(repository, "E1004")
        stranger = authenticate(repository, "E1001")
        agent.run("private question", user=owner, thread_id="t-private")

        with pytest.raises(ThreadAccessError):
            agent.history("t-private", stranger)
        with pytest.raises(ThreadAccessError):
            agent.run("show me the history", user=stranger, thread_id="t-private")

        # The refused turn was never written to the thread.
        assert [m.text for m in agent.history("t-private", owner)] == ["private question", "ok"]

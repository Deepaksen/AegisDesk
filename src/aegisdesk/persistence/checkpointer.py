"""Checkpointers: where LangGraph saves thread state after every node.

* `InMemorySaver` - lost when the process exits; used by tests.
* `SqliteSaver` - a local file; survives restarts with no extra infrastructure.
  A PostgreSQL checkpointer replaces it when the application database arrives.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


@contextmanager
def sqlite_checkpointer(path: Path) -> Iterator[SqliteSaver]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        yield SqliteSaver(conn)
    finally:
        conn.close()

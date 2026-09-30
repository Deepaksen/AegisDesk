"""Build the durable parts from settings: the repository's access store and the checkpointer.

For an approval to survive a restart, both must be durable: the approval
records (DATA_STORE=postgres) and the paused conversation (CHECKPOINT_STORE
sqlite or postgres). Schemas come from `aegisdesk db init`, never from
application startup.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from aegisdesk.config import CheckpointStoreKind, DataStoreKind, Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.persistence.checkpointer import sqlite_checkpointer


def psycopg_url(database_url: str) -> str:
    """SQLAlchemy URL (postgresql+psycopg://) -> libpq URL for psycopg/LangGraph."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def build_repository(settings: Settings) -> ServiceDeskRepository:
    store = None
    if settings.data_store is DataStoreKind.POSTGRES:
        from aegisdesk.domain.access_store_pg import PgAccessStore

        store = PgAccessStore(settings.database_url)
    return ServiceDeskRepository.from_seed(
        settings.seed_data_dir,
        access_store=store,
        approval_ttl=timedelta(hours=settings.approval_ttl_hours),
    )


@contextmanager
def open_checkpointer(settings: Settings) -> Iterator[BaseCheckpointSaver[Any]]:
    if settings.checkpoint_store is CheckpointStoreKind.POSTGRES:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(psycopg_url(settings.database_url)) as saver:
            yield saver
    else:
        with sqlite_checkpointer(settings.checkpoint_db_path) as saver:
            yield saver

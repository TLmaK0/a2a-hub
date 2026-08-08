"""Persistent task store (SQLite by default) for the hub mailbox.

The ``Task`` is the mailbox: this is where what one agent leaves for another lives
until the recipient picks it up. SQLite is enough to start; moving to PostgreSQL
is just a connection URL change (``A2A_HUB_DB_URL``), no code changes.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from a2a.server.tasks import DatabaseTaskStore

from a2a_hub.executor import hub_owner_resolver


def create_engine(db_url: str) -> AsyncEngine:
    """Create the SQLAlchemy ``AsyncEngine`` for the given URL."""
    return create_async_engine(db_url)


def create_task_store(engine: AsyncEngine) -> DatabaseTaskStore:
    """Create the task store with the hub's per-recipient routing."""
    return DatabaseTaskStore(engine=engine, owner_resolver=hub_owner_resolver)

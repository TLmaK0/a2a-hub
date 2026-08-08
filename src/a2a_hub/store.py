"""Persistent task store (SQLite by default) for the hub mailbox.

The ``Task`` is the mailbox: this is where what one agent leaves for another lives
until the recipient picks it up. SQLite is enough to start; moving to PostgreSQL is
just a connection URL change (``A2A_HUB_DB_URL``), no code changes.

Two mailboxes per identity
--------------------------
An agent ``p`` authenticated as session ``p/s`` reads **two** owners:

- ``p``   — the agent's shared mailbox: where messages addressed to the agent land
  when the sender does not know (or care about) the session. This is what makes
  store-and-forward work: you can leave a message for an agent that is not awake.
- ``p/s`` — the session's own mailbox: direct messages to that specific process.

Writes always target exactly one owner (the recipient the executor resolved).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from a2a.server.context import ServerCallContext
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import a2a_pb2
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE

from a2a_hub.auth import principal_of
from a2a_hub.executor import OWNER_OVERRIDE_KEY, hub_owner_resolver


def create_engine(db_url: str) -> AsyncEngine:
    """Create the SQLAlchemy ``AsyncEngine`` for the given URL."""
    return create_async_engine(db_url)


class HubTaskStore(DatabaseTaskStore):
    """Task store where a session reads its own mailbox *and* its principal's."""

    def _read_owners(self, context: ServerCallContext) -> list[str]:
        """Owners to read for this call, most specific first.

        While delivering a message the executor pins the owner (``owner_override``),
        and that must win: the framework reads back the task it is writing. Outside
        that flow, a session reads its own mailbox plus the agent's shared one.
        """
        override = context.state.get(OWNER_OVERRIDE_KEY)
        if isinstance(override, str) and override:
            return [override]

        identity = context.user.user_name
        principal = principal_of(identity)
        return [identity] if identity == principal else [identity, principal]

    @staticmethod
    def _context_for(context: ServerCallContext, owner: str) -> ServerCallContext:
        """Copy of ``context`` that resolves to exactly ``owner``."""
        return context.model_copy(
            update={"state": {**context.state, OWNER_OVERRIDE_KEY: owner}}
        )

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> a2a_pb2.Task | None:
        """Fetch a task from any mailbox this caller may read."""
        for owner in self._read_owners(context):
            task = await super().get(task_id, self._context_for(context, owner))
            if task is not None:
                return task
        return None

    async def list(
        self, params: a2a_pb2.ListTasksRequest, context: ServerCallContext
    ) -> a2a_pb2.ListTasksResponse:
        """List tasks across every mailbox this caller may read.

        Note: when two mailboxes are merged the result is not paginated (agents poll
        with ``status_timestamp_after``); ``page_size`` still caps the batch.
        """
        owners = self._read_owners(context)
        responses = [
            await super().list(params, self._context_for(context, owner))
            for owner in owners
        ]
        if len(responses) == 1:
            return responses[0]

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        tasks = [task for response in responses for task in response.tasks]
        # Newest first, matching the store's own ordering (unset timestamps last).
        tasks.sort(
            key=lambda task: (
                task.status.timestamp.seconds,
                task.status.timestamp.nanos,
            ),
            reverse=True,
        )
        return a2a_pb2.ListTasksResponse(
            tasks=tasks[:page_size],
            total_size=sum(response.total_size for response in responses),
            next_page_token="",
            page_size=page_size,
        )


def create_task_store(engine: AsyncEngine) -> HubTaskStore:
    """Create the task store with the hub's per-recipient routing."""
    return HubTaskStore(engine=engine, owner_resolver=hub_owner_resolver)

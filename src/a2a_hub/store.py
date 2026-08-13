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

import base64
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from a2a.server.context import ServerCallContext
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import a2a_pb2
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE

from a2a_hub.auth import principal_of
from a2a_hub.executor import OWNER_OVERRIDE_KEY, hub_owner_resolver


@dataclass(frozen=True)
class _Cursor:
    """Where a merged listing is: each mailbox's token, plus offset in this batch."""

    tokens: dict[str, str]
    skip: int
    #: Per-mailbox totals, carried so an exhausted mailbox still counts towards
    #: total_size. Fixed on the first page: a listing reports the size it started
    #: with, which is more useful than a number that shifts under the reader.
    totals: dict[str, int]


#: Marks a mailbox that has already been read to the end. Without it, a mailbox with
#: no further token looks like one that was never started, and the next page serves
#: its first tasks again — measured as duplicates while writing this.
EXHAUSTED = "-"


def _encode_cursor(tokens: dict[str, str], skip: int, totals: dict[str, int]) -> str:
    """Opaque token. Opaque on purpose: its shape is ours to change."""
    raw = json.dumps({"t": tokens, "s": skip, "n": totals}, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(token: str) -> _Cursor:
    """Decode a cursor, treating anything unreadable as "start from the beginning".

    A malformed token must not fail the request: the caller would lose access to its
    own mailbox over a value it never constructed.
    """
    if not token:
        return _Cursor({}, 0, {})
    try:
        raw = json.loads(base64.urlsafe_b64decode(token.encode()))
        tokens = {str(k): str(v) for k, v in dict(raw["t"]).items()}
        totals = {str(k): int(v) for k, v in dict(raw.get("n") or {}).items()}
        return _Cursor(tokens, max(0, int(raw["s"])), totals)
    except Exception:  # noqa: BLE001 - any malformed token restarts the listing
        return _Cursor({}, 0, {})


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
        """List tasks across every mailbox this caller may read, paginated.

        The merge used to return an **empty** page token whatever was left, on the
        assumption that agents poll by timestamp rather than paginate. That made the
        response contradict itself — `total_size` said 115 while 50 came back and the
        token said "no more" — and since `page_size` is capped at 100 by the protocol,
        anything past the first page of a mailbox over 100 became **unreachable**:
        measured on four real mailboxes on 2026-08-13, with no client-side workaround
        (`status_timestamp_before` is rejected and every message has its own context).

        Now the token carries the per-mailbox tokens the SDK itself produces, plus how
        far into the current merged batch we already are. An empty token means what it
        says: nothing is left.
        """
        owners = self._read_owners(context)
        if len(owners) == 1:
            return await super().list(params, self._context_for(context, owners[0]))

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        cursor = _decode_cursor(params.page_token)

        responses: dict[str, a2a_pb2.ListTasksResponse] = {}
        for owner in owners:
            sub = a2a_pb2.ListTasksRequest()
            sub.CopyFrom(params)
            sub.page_size = page_size
            token = cursor.tokens.get(owner, "")
            if token == EXHAUSTED:
                responses[owner] = a2a_pb2.ListTasksResponse(page_size=page_size)
                continue
            sub.page_token = token
            responses[owner] = await super().list(
                sub, self._context_for(context, owner)
            )

        tasks = [task for response in responses.values() for task in response.tasks]
        # Newest first, matching the store's own ordering (unset timestamps last).
        tasks.sort(
            key=lambda task: (
                task.status.timestamp.seconds,
                task.status.timestamp.nanos,
            ),
            reverse=True,
        )

        # An exhausted mailbox reports nothing, so its size comes from the cursor.
        totals = {
            owner: (
                response.total_size
                if owner not in cursor.totals
                else cursor.totals[owner]
            )
            for owner, response in responses.items()
        }

        window = tasks[cursor.skip : cursor.skip + page_size]

        if cursor.skip + page_size < len(tasks):
            # More of this batch to hand out before fetching the next one.
            next_token = _encode_cursor(
                cursor.tokens, cursor.skip + page_size, totals
            )
        else:
            # Batch exhausted: carry each mailbox's own token forward.
            forward = {
                owner: response.next_page_token or EXHAUSTED
                for owner, response in responses.items()
            }
            more = any(token != EXHAUSTED for token in forward.values())
            next_token = _encode_cursor(forward, 0, totals) if more else ""

        return a2a_pb2.ListTasksResponse(
            tasks=window,
            total_size=sum(totals.values()),
            next_page_token=next_token,
            page_size=page_size,
        )


def create_task_store(engine: AsyncEngine) -> HubTaskStore:
    """Create the task store with the hub's per-recipient routing."""
    return HubTaskStore(engine=engine, owner_resolver=hub_owner_resolver)

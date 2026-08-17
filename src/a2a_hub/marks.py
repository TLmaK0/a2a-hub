"""Message marks: what happened to a message after it was read.

Three states looked identical before this existed — a message nobody read, one read
and acted on, and one read and dismissed. ``ListTasks`` returned all three the same
way, so a sender could not tell *refused* from *not yet read* from *read and
ignored*, and work was lost in silence rather than refused out loud.

Two things shape the design, and both come from the owner:

- **A mark that costs nothing proves nothing.** *"Si acaba siendo un botón que todos
  pulsan sin leer, hemos construido otro verde que no prueba nada."* So a message is
  never marked on read, and every close costs something a loop that did not read
  cannot fabricate: a **ref** for processed, a **reason** for discarded, and the
  **question** for one waiting on a decision.
- **The sender may read the mark of a message they sent.** The original complaint was
  made from the sender's side, so marks visible only to their writer would have been
  a private to-do list. Reading is all the sender gets: the mark is always *written*
  by the recipient.

Storage is deliberately separate from the ``Task``. Reusing an SDK task state to mean
"handled" would corrupt protocol semantics for every existing client, and this repo
does not extend the schema the SDK implements. One row per ``(task_id, identity)``,
so on a broadcast one session closing a message does not discharge another's.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, and_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from a2a_hub.schema import add_missing_columns


#: A message is closed by acting on it or by dismissing it; ``awaiting`` is neither.
#: It is a waiting room — the agent cannot resolve this alone and it is with the owner
#: — and it becomes ``processed`` when the decision arrives. Keeping it out of the
#: "closed" set is the whole point: parked work must not read as finished work.
PROCESSED = "processed"
DISCARDED = "discarded"
AWAITING = "awaiting"

STATES: frozenset[str] = frozenset({PROCESSED, DISCARDED, AWAITING})

#: States that take a message out of the unprocessed queue.
CLOSED_STATES: frozenset[str] = frozenset({PROCESSED, DISCARDED})

#: The detail each state requires, and why it is not optional. A minimum length is a
#: crude gate and still the difference between "ok" and a sentence somebody had to
#: read the message to write.
DETAIL_LABELS = {
    PROCESSED: "a ref: the issue comment, commit sha or reply id where you acted",
    DISCARDED: "a reason: why this needed nothing",
    AWAITING: "the question that is waiting for a decision",
}

MIN_DETAIL = 8
MAX_DETAIL = 2000

_metadata = MetaData()

#: One row per (message, whoever marked it).
marks = Table(
    "message_marks",
    _metadata,
    Column("task_id", String(255), primary_key=True),
    Column("identity", String(255), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("detail", String(MAX_DETAIL), nullable=False, server_default=""),
    # Recorded when the mark is written, read from the message itself rather than
    # from the caller: it is what lets a sender ask about their own message without
    # any access to someone else's mailbox.
    Column("sender", String(255), nullable=False, server_default=""),
    Column("marked_at", DateTime(timezone=True), nullable=True),
)


class MarkError(ValueError):
    """The mark as asked for is not one that can be stored."""


def clean_mark(payload: dict) -> dict:
    """Validate a requested mark, or refuse it with a reason the caller can act on."""
    state = str(payload.get("state") or "").strip().lower()
    if state not in STATES:
        raise MarkError(
            f"state must be one of {sorted(STATES)}; got {state!r}"
        )

    detail = str(payload.get("detail") or "").strip()
    if len(detail) < MIN_DETAIL:
        raise MarkError(
            f"{state} needs {DETAIL_LABELS[state]} "
            f"(at least {MIN_DETAIL} characters); got {len(detail)}"
        )
    return {"state": state, "detail": detail[:MAX_DETAIL]}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class MessageMarks:
    """Storage for message marks."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._ready = False

    async def create_schema(self) -> None:
        """Create the table if absent. Additive: nothing existing is touched."""
        if self._ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(_metadata.create_all)
            await conn.run_sync(add_missing_columns, _metadata)
        self._ready = True

    async def set_mark(
        self, task_id: str, identity: str, mark: dict, sender: str = ""
    ) -> dict:
        """Write (or replace) this identity's mark for a message.

        Replacing is intended, not a loophole: ``awaiting`` is meant to become
        ``processed`` once the decision lands, and a mistaken mark should be
        correctable by whoever wrote it. What cannot happen is one identity
        overwriting another's, which the composite key prevents.
        """
        await self.create_schema()
        values = {
            "task_id": task_id,
            "identity": identity,
            "state": mark["state"],
            "detail": mark["detail"],
            "sender": sender,
            "marked_at": _now(),
        }
        statement = sqlite_insert(marks).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["task_id", "identity"],
            set_={
                "state": statement.excluded.state,
                "detail": statement.excluded.detail,
                "sender": statement.excluded.sender,
                "marked_at": statement.excluded.marked_at,
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(statement)
        return {
            "task_id": task_id,
            "identity": identity,
            "state": values["state"],
            "detail": values["detail"],
            "marked_at": _isoformat(values["marked_at"]),
        }

    async def get_marks(self, task_id: str) -> list[dict]:
        """Every mark on one message, oldest first.

        Plural because a broadcast has one row per session that closed it, and
        "two of the four sessions dealt with it" is the honest answer there.
        """
        await self.create_schema()
        query = (
            select(marks)
            .where(marks.c.task_id == task_id)
            .order_by(marks.c.marked_at)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_as_dict(row) for row in rows]

    async def marked_by(self, identity: str) -> dict[str, str]:
        """``task_id -> state`` for everything this identity has marked.

        This is what makes an unprocessed-only mailbox possible without changing
        what ``ListTasks`` returns: the caller asks the hub what it has already
        closed and leaves those out. An old client never calls it and is unaffected.
        """
        await self.create_schema()
        query = select(marks.c.task_id, marks.c.state).where(
            marks.c.identity == identity
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return {task_id: state for task_id, state in rows}

    async def sent_by(self, sender: str) -> list[dict]:
        """Marks on messages this identity *sent*, newest first.

        The sender's half of the contract. Scoped to their own messages by the
        stored sender, so it is not a view of anyone's mailbox — it answers "what
        happened to what I sent", which is the question the whole feature exists
        for.
        """
        await self.create_schema()
        query = (
            select(marks)
            .where(and_(marks.c.sender == sender, marks.c.sender != ""))
            .order_by(marks.c.marked_at.desc())
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_as_dict(row) for row in rows]


def _as_dict(row) -> dict:
    return {
        "task_id": row["task_id"],
        "identity": row["identity"],
        "state": row["state"],
        "detail": row["detail"],
        "sender": row["sender"],
        "marked_at": _isoformat(row["marked_at"]),
    }


def _isoformat(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat()

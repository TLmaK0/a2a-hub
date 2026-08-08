"""Store-level tests: which mailboxes a call reads.

A session reads its own mailbox plus its agent's shared one — except while a message
is being delivered, when the executor pins the owner and that must win (the framework
reads back the task it is writing).
"""

from __future__ import annotations

import pytest

from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2

from a2a_hub.executor import OWNER_OVERRIDE_KEY
from a2a_hub.store import create_engine, create_task_store
from test_executor_unit import FakeUser


@pytest.fixture
async def store(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    task_store = create_task_store(engine)
    yield task_store
    await engine.dispose()


def _context(identity: str, owner_override: str | None = None) -> ServerCallContext:
    state = {OWNER_OVERRIDE_KEY: owner_override} if owner_override else {}
    return ServerCallContext(user=FakeUser(identity), state=state)


def test_read_owners_session_reads_own_and_shared(store):
    assert store._read_owners(_context("agent/sess")) == ["agent/sess", "agent"]


def test_read_owners_bare_principal_reads_once(store):
    assert store._read_owners(_context("agent")) == ["agent"]


def test_read_owners_override_wins(store):
    # While delivering, the pinned owner is the only one consulted.
    owners = store._read_owners(_context("sender/sess", owner_override="dest/other"))
    assert owners == ["dest/other"]


async def test_list_with_pinned_owner_reads_only_that_mailbox(store):
    """Single-owner path: used by the delivery flow, must not merge mailboxes."""
    task = a2a_pb2.Task(id="t1", context_id="c1")
    task.status.state = a2a_pb2.TaskState.TASK_STATE_COMPLETED
    await store.save(task, _context("sender/s", owner_override="dest/one"))

    pinned = await store.list(
        a2a_pb2.ListTasksRequest(), _context("sender/s", owner_override="dest/one")
    )
    assert [t.id for t in pinned.tasks] == ["t1"]
    assert pinned.total_size == 1

    # The sender's own mailboxes are untouched.
    empty = await store.list(a2a_pb2.ListTasksRequest(), _context("sender/s"))
    assert empty.total_size == 0


async def test_list_merges_shared_and_session_mailboxes(store):
    shared = a2a_pb2.Task(id="shared", context_id="c")
    shared.status.state = a2a_pb2.TaskState.TASK_STATE_COMPLETED
    shared.status.timestamp.FromSeconds(100)
    await store.save(shared, _context("x/s", owner_override="agent"))

    direct = a2a_pb2.Task(id="direct", context_id="c")
    direct.status.state = a2a_pb2.TaskState.TASK_STATE_COMPLETED
    direct.status.timestamp.FromSeconds(200)
    await store.save(direct, _context("x/s", owner_override="agent/sess"))

    merged = await store.list(a2a_pb2.ListTasksRequest(), _context("agent/sess"))
    assert merged.total_size == 2
    # Newest first.
    assert [task.id for task in merged.tasks] == ["direct", "shared"]


async def test_get_falls_back_to_shared_mailbox(store):
    task = a2a_pb2.Task(id="in-shared", context_id="c")
    task.status.state = a2a_pb2.TaskState.TASK_STATE_COMPLETED
    await store.save(task, _context("x/s", owner_override="agent"))

    found = await store.get("in-shared", _context("agent/whatever"))
    assert found is not None and found.id == "in-shared"

    # Another agent cannot reach it.
    assert await store.get("in-shared", _context("other/s")) is None

"""Unit tests for the executor and the owner resolver.

Cover branches that are hard to force over HTTP (cancel of a live task, missing
message) using a fake event queue that captures what the executor would emit.
"""

from __future__ import annotations

from a2a.auth.user import User
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest, TaskState

from a2a_hub.executor import (
    HubAgentExecutor,
    _extract_recipient,
    _struct_to_dict,
    hub_owner_resolver,
)


class FakeUser(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


class FakeQueue:
    """Captures the events enqueued by the executor/TaskUpdater."""

    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)

    def states(self) -> list:
        return [
            e.status.state for e in self.events if hasattr(e, "status") and e.status.state
        ]


def _ctx(message: Message | None, user: str = "agent-a") -> RequestContext:
    request = SendMessageRequest(message=message) if message is not None else None
    return RequestContext(
        call_context=ServerCallContext(user=FakeUser(user)),
        request=request,
        task_id="task-1",
        context_id="ctx-1",
    )


def _msg(recipient: str | None, text: str = "hello") -> Message:
    m = Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text=text)])
    if recipient is not None:
        m.metadata.update({"recipient": recipient})
    return m


# --- resolver -------------------------------------------------------------

def test_owner_resolver_uses_override():
    ctx = ServerCallContext(user=FakeUser("caller"), state={"owner_override": "dest"})
    assert hub_owner_resolver(ctx) == "dest"


def test_owner_resolver_falls_back_to_caller():
    ctx = ServerCallContext(user=FakeUser("caller"))
    assert hub_owner_resolver(ctx) == "caller"


def test_owner_resolver_empty_override_falls_back():
    ctx = ServerCallContext(user=FakeUser("caller"), state={"owner_override": ""})
    assert hub_owner_resolver(ctx) == "caller"


# --- extraction helpers ---------------------------------------------------

def test_struct_to_dict_none():
    assert _struct_to_dict(None) == {}


def test_extract_recipient_from_message():
    assert _extract_recipient(_ctx(_msg("agent-b/s1"))) == "agent-b/s1"


def test_extract_recipient_missing():
    assert _extract_recipient(_ctx(_msg(None))) is None


def test_extract_recipient_from_request_metadata():
    req = SendMessageRequest(message=_msg(None))
    req.metadata.update({"recipient": "agent-c"})
    ctx = RequestContext(
        call_context=ServerCallContext(user=FakeUser("a")),
        request=req,
        task_id="t",
        context_id="c",
    )
    assert _extract_recipient(ctx) == "agent-c"


# --- execute / cancel -----------------------------------------------------

async def test_execute_without_message_rejects():
    q = FakeQueue()
    await HubAgentExecutor().execute(_ctx(None), q)
    assert TaskState.TASK_STATE_REJECTED in q.states()


async def test_execute_delivery_marks_completed():
    q = FakeQueue()
    ctx = _ctx(_msg("agent-b/s1"))
    await HubAgentExecutor(known_agents={"agent-b"}).execute(ctx, q)
    assert TaskState.TASK_STATE_COMPLETED in q.states()
    assert ctx.call_context.state["owner_override"] == "agent-b/s1"


async def test_cancel_emits_canceled():
    q = FakeQueue()
    await HubAgentExecutor().cancel(_ctx(_msg("agent-b/s1")), q)
    assert TaskState.TASK_STATE_CANCELED in q.states()

"""App factory tests: fail-closed startup, lifecycle and non-HTTP scope passthrough."""

from __future__ import annotations

import dataclasses

import pytest

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import Task

from a2a_hub.app import create_app
from a2a_hub.auth import BearerAuthMiddleware, TokenRegistry
from a2a_hub.executor import HubAgentExecutor
from test_executor_unit import FakeQueue, FakeUser


def test_create_app_without_tokens_raises(settings):
    # Fail closed and loud: no tokens => the process must abort (pod crashes)
    # instead of coming up unusable.
    no_tokens = dataclasses.replace(settings, tokens={})
    with pytest.raises(RuntimeError, match="No agent tokens"):
        create_app(no_tokens)


async def test_lifespan_starts_and_closes(settings):
    # The lifecycle must enter and exit releasing resources without errors.
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.engine is not None


async def test_middleware_passes_non_http_scopes():
    seen = {}

    async def downstream(scope, receive, send):
        seen["type"] = scope["type"]

    mw = BearerAuthMiddleware(downstream, TokenRegistry())
    await mw({"type": "lifespan"}, None, None)
    assert seen["type"] == "lifespan"


async def test_ensure_task_does_not_recreate_if_exists():
    q = FakeQueue()
    ctx = RequestContext(
        call_context=ServerCallContext(user=FakeUser("a")),
        task=Task(id="task-1", context_id="ctx-1"),
        task_id="task-1",
        context_id="ctx-1",
    )
    await HubAgentExecutor._ensure_task(ctx, q)
    assert q.events == []

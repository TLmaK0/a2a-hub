"""Security hardening tests: body-size limit and token redaction from context."""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from starlette.requests import Request

from a2a_hub.app import create_app
from a2a_hub.auth import RedactingContextBuilder
from conftest import TOKEN_A, rpc


async def test_oversized_body_rejected_413(settings):
    # A small limit makes a normal request oversized.
    tiny = dataclasses.replace(settings, max_body_bytes=10)
    app = create_app(tiny)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        r = await rpc(c, "ListTasks", {}, token=TOKEN_A)
        assert r.status_code == 413
        assert r.json()["error"] == "payload_too_large"
    await app.state.engine.dispose()


async def test_body_limit_disabled_allows_large(settings):
    disabled = dataclasses.replace(settings, max_body_bytes=0)
    app = create_app(disabled)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        r = await rpc(c, "ListTasks", {}, token=TOKEN_A)
        assert r.status_code == 200
    await app.state.engine.dispose()


def test_context_builder_redacts_authorization():
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer super-secret"),
            (b"cookie", b"session=abc"),
            (b"a2a-version", b"1.0"),
        ],
    }
    context = RedactingContextBuilder().build(Request(scope))
    stored = {k.lower() for k in context.state["headers"]}
    assert "authorization" not in stored
    assert "cookie" not in stored
    assert "a2a-version" in stored


async def test_max_body_middleware_ignores_bad_content_length():
    # A malformed Content-Length must not block the request (treated as not-too-large).
    from a2a_hub.auth import MaxBodySizeMiddleware

    called = {}

    async def downstream(scope, receive, send):
        called["ok"] = True

    mw = MaxBodySizeMiddleware(downstream, max_bytes=10)
    scope = {"type": "http", "headers": [(b"content-length", b"not-a-number")]}
    await mw(scope, None, None)
    assert called["ok"] is True


async def test_max_body_middleware_passes_non_http():
    from a2a_hub.auth import MaxBodySizeMiddleware

    seen = {}

    async def downstream(scope, receive, send):
        seen["type"] = scope["type"]

    mw = MaxBodySizeMiddleware(downstream, max_bytes=10)
    await mw({"type": "lifespan"}, None, None)
    assert seen["type"] == "lifespan"

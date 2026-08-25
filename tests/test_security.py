"""Security hardening tests: body-size limit and token redaction from context."""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from starlette.requests import Request

from a2a_hub.app import create_app
from a2a_hub.auth import RedactingContextBuilder
from conftest import TOKEN_A, auth, rpc


async def test_oversized_body_rejected_413(settings):
    # A small limit makes a normal request oversized.
    tiny = dataclasses.replace(settings, max_body_bytes=10)
    app = create_app(tiny)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        r = await rpc(c, "ListTasks", {}, token=TOKEN_A)
        assert r.status_code == 413
        # On the JSON-RPC endpoint the refusal is a JSON-RPC error object, because a
        # conformant client parses this body as a JSON-RPC response (#48 item 5).
        # The guard itself is unchanged: this is the shape, not the check.
        error = r.json()["error"]
        assert r.json()["jsonrpc"] == "2.0"
        assert r.json()["id"] is None
        assert error["code"] == -32600
        assert error["data"]["error"] == "payload_too_large"
    await app.state.engine.dispose()


async def test_a_refusal_off_the_jsonrpc_endpoint_keeps_the_plain_shape(settings):
    """The other half of #48 item 5, and the half that is easy to lose.

    A JSON-RPC envelope is the right shape *because* the caller is speaking JSON-RPC.
    On `/agents/*` — plain REST, and off the A2A surface entirely — it would be the
    identical mistake mirrored: a body shaped for a protocol the caller is not using.

    Without this, "make the errors conformant" reads as "wrap every error", which is
    how a conformance fix turns into a second non-conformance.
    """
    tiny = dataclasses.replace(settings, max_body_bytes=10)
    app = create_app(tiny)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        r = await c.post(
            "/agents/register",
            json={"role": "project", "projects": ["a2a-hub"], "doing": "x" * 100},
            headers=auth(TOKEN_A),
        )
        assert r.status_code == 413
        assert r.json()["error"] == "payload_too_large"
        assert "jsonrpc" not in r.json()

        # And a session refusal on the same non-JSON-RPC path.
        r = await c.get("/agents", headers=auth(TOKEN_A, session="bad session"))
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_session"
        assert "jsonrpc" not in r.json()
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

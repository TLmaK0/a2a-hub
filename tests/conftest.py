"""Shared fixtures: hub app + HTTP client against the ASGI app.

Each test uses its own SQLite file in a temp dir (full isolation). The client
speaks the real A2A protocol over JSON-RPC against the in-memory app (no network).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from a2a_hub.app import create_app
from a2a_hub.config import Settings


# Two test agents: token -> name.
TOKENS = {"tok-a": "agent-a", "tok-b": "agent-b"}
TOKEN_A = "tok-a"
TOKEN_B = "tok-b"
AGENT_A = "agent-a"
AGENT_B = "agent-b"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings with two agents and a temp SQLite DB per test."""
    db_file = tmp_path / "hub.db"
    return Settings(
        tokens=dict(TOKENS),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        public_url="https://a2a.example.test/",
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[Any]:
    """Hub app; disposes the engine on teardown."""
    application = create_app(settings)
    yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client routed to the ASGI app (no port, no network)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://a2a.example.test"
    ) as c:
        yield c
    await app.state.engine.dispose()


#: A2A protocol version negotiated by the SDK handler.
A2A_VERSION = "1.0"


def auth(token: str | None) -> dict[str, str]:
    """Headers: A2A version + Authorization bearer (if a token is given)."""
    headers = {"A2A-Version": A2A_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def rpc(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any],
    *,
    token: str | None = None,
    request_id: int | str = 1,
) -> httpx.Response:
    """Send an A2A JSON-RPC request to the root endpoint."""
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return await client.post("/", json=body, headers=auth(token))


def send_message_params(
    recipient: str | None,
    text: str = "hello",
    *,
    with_recipient: bool = True,
) -> dict[str, Any]:
    """Build ``SendMessage`` params for a given recipient."""
    message: dict[str, Any] = {
        "messageId": uuid.uuid4().hex,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if with_recipient and recipient is not None:
        message["metadata"] = {"recipient": recipient}
    return {"message": message}

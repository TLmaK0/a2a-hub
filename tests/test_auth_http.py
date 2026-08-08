"""Functional auth tests: the server does not answer without a valid bearer."""

from __future__ import annotations

from conftest import IDENT_B, TOKEN_A, rpc, send_message_params


async def test_no_token_401(client):
    r = await rpc(client, "ListTasks", {})
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == "Bearer"


async def test_invalid_token_401(client):
    r = await rpc(client, "ListTasks", {}, token="does-not-exist")
    assert r.status_code == 401


async def test_wrong_scheme_401(client):
    r = await client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}},
        headers={"Authorization": "Basic dXNlcjpwYXNz", "A2A-Version": "1.0"},
    )
    assert r.status_code == 401


async def test_valid_token_passes(client):
    r = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    assert r.status_code == 200
    assert "result" in r.json()


async def test_send_without_token_does_not_persist(client):
    # An unauthenticated attempt must not create anything in the recipient mailbox.
    unauth = await rpc(client, "SendMessage", send_message_params(IDENT_B))
    assert unauth.status_code == 401
    listed = await rpc(client, "ListTasks", {}, token="tok-b")
    assert listed.json()["result"]["totalSize"] == 0


async def test_agent_card_public(client):
    r = await client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    assert r.json()["name"] == "a2a-hub"


async def test_healthz_public(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_public_paths_open_others_protected(client):
    # Sanity: the public path answers, a protected one without a token does not.
    assert (await client.get("/healthz")).status_code == 200
    assert (await rpc(client, "GetTask", {"id": "x"})).status_code == 401

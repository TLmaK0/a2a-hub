"""Per-session identity tests.

The token identifies the machine (principal); a session claims its own mailbox under
that principal (``machine/session``). This lets several sessions on the same machine
talk to each other while staying isolated from other machines.
"""

from __future__ import annotations

import pytest

from a2a_hub.auth import is_valid_identity, principal_of
from conftest import AGENT_A, AGENT_B, TOKEN_A, TOKEN_B, auth, rpc, send_message_params


def session_headers(token: str, session: str | None) -> dict[str, str]:
    headers = auth(token)
    if session is not None:
        headers["A2A-Session"] = session
    return headers


async def send_as(client, token, session, recipient, text="hi"):
    """SendMessage as ``token``'s principal, optionally under ``session``."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": send_message_params(recipient, text),
    }
    return await client.post("/", json=body, headers=session_headers(token, session))


async def inbox_of(client, token, session):
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "ListTasks",
        "params": {"includeArtifacts": True},
    }
    response = await client.post(
        "/", json=body, headers=session_headers(token, session)
    )
    return response.json()["result"]


# --- functional: sessions on the same machine ------------------------------

async def test_sessions_on_same_machine_can_talk(client):
    # session "one" writes to session "two" of the SAME machine (same token).
    sent = await send_as(client, TOKEN_A, "one", f"{AGENT_A}/two", "hello sibling")
    assert sent.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    two = await inbox_of(client, TOKEN_A, "two")
    assert two["totalSize"] == 1
    artifact = two["tasks"][0]["artifacts"][0]
    assert artifact["parts"][0]["text"] == "hello sibling"
    # The sender is the full session identity, not just the machine.
    assert artifact["metadata"]["sender"] == f"{AGENT_A}/one"


async def test_each_session_has_its_own_mailbox(client):
    await send_as(client, TOKEN_A, "one", f"{AGENT_A}/two", "for two")

    # The sending session sees nothing; only the addressed one does.
    assert (await inbox_of(client, TOKEN_A, "one"))["totalSize"] == 0
    assert (await inbox_of(client, TOKEN_A, "two"))["totalSize"] == 1
    # A third session of the same machine is unaffected.
    assert (await inbox_of(client, TOKEN_A, "three"))["totalSize"] == 0


async def test_bare_machine_is_not_addressable(client):
    # With sessions mandatory nobody could ever read a bare-principal mailbox,
    # so addressing one is rejected instead of silently black-holing the message.
    sent = await send_as(client, TOKEN_B, "s1", AGENT_A, "to the machine")
    assert sent.json()["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"


async def test_cross_machine_to_session(client):
    # Another machine can address a session of this one.
    await send_as(client, TOKEN_B, "other", f"{AGENT_A}/one", "from the other machine")
    inbox = await inbox_of(client, TOKEN_A, "one")
    assert inbox["totalSize"] == 1
    sender = inbox["tasks"][0]["artifacts"][0]["metadata"]["sender"]
    assert sender == f"{AGENT_B}/other"


async def test_session_cannot_read_another_machines_mailbox(client):
    # B leaves a message for machine B's session; A must not see it.
    await send_as(client, TOKEN_B, "x", f"{AGENT_B}/x", "b private")
    assert (await inbox_of(client, TOKEN_A, "x"))["totalSize"] == 0
    assert (await inbox_of(client, TOKEN_B, "x"))["totalSize"] == 1


async def test_session_identity_is_bound_to_own_token(client):
    """A client cannot claim a session under another machine's principal.

    Sending the session header with A's token yields ``agent-a/<session>`` — there is
    no way to become ``agent-b/<session>``; the principal always comes from the token.
    """
    await send_as(client, TOKEN_A, AGENT_B, f"{AGENT_A}/dest", "probe")
    inbox = await inbox_of(client, TOKEN_A, "dest")
    sender = inbox["tasks"][0]["artifacts"][0]["metadata"]["sender"]
    assert sender == f"{AGENT_A}/{AGENT_B}"
    assert not sender.startswith(f"{AGENT_B}/")


async def test_unknown_principal_with_session_rejected(client):
    sent = await send_as(client, TOKEN_A, "one", "ghost/session", "nobody")
    task = sent.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"


@pytest.mark.parametrize("bad", ["with space", "sla/sh", "-startshyphen", "x" * 65, ""])
async def test_invalid_session_header_rejected(client, bad):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
    response = await client.post("/", json=body, headers=session_headers(TOKEN_A, bad))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_session"


async def test_session_header_is_mandatory(client):
    """Without a session there is no identity: two processes could share a mailbox."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
    response = await client.post("/", json=body, headers=auth(TOKEN_A, session=None))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_session"
    assert "required" in response.json()["detail"]


async def test_missing_session_still_401_without_token(client):
    """Auth comes first: no token is 401, not a session complaint."""
    response = await rpc(client, "ListTasks", {}, token=None)
    assert response.status_code == 401


async def test_session_header_is_trimmed(client):
    await send_as(client, TOKEN_A, "  spaced  ", f"{AGENT_A}/dest", "trim me")
    inbox = await inbox_of(client, TOKEN_A, "dest")
    assert inbox["tasks"][0]["artifacts"][0]["metadata"]["sender"] == f"{AGENT_A}/spaced"


# --- identity helpers -----------------------------------------------------

@pytest.mark.parametrize(
    "identity,expected",
    [("machine", "machine"), ("machine/session", "machine"), ("m/s/extra", "m")],
)
def test_principal_of(identity, expected):
    assert principal_of(identity) == expected


@pytest.mark.parametrize(
    "identity,known,valid",
    [
        ("a/s1", {"a"}, True),
        ("a", {"a"}, False),  # a session is mandatory
        ("b/s1", {"a"}, False),  # unknown principal
        ("a/bad session", {"a"}, False),
        ("a/", {"a"}, False),  # empty session
        ("/s1", {"a"}, False),  # empty principal
        ("", {"a"}, False),
        ("anything/s", None, True),  # principal validation disabled
    ],
)
def test_is_valid_identity(identity, known, valid):
    assert is_valid_identity(identity, known) is valid

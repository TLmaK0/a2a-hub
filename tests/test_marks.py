"""Message marks: processed, discarded, awaiting — and who may read them.

Functional throughout: every test drives the real flow over the protocol
(``SendMessage`` to deliver, then the extension routes), because a test that only
exercises the module would have passed for the session change that shipped at 100%
coverage and still broke store-and-forward.
"""

from __future__ import annotations

import pytest

from conftest import AGENT_A, AGENT_B, IDENT_A, IDENT_B, TOKEN_A, TOKEN_B, auth


async def deliver(client, token, recipient, text="please handle this"):
    """Send a message and return the delivered task id."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": f"m-{text}",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
                "metadata": {"recipient": recipient},
            }
        },
    }
    response = await client.post("/", json=body, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()["result"]["task"]["id"]


async def mark(client, token, task_id, state, detail):
    return await client.post(
        f"/messages/{task_id}/mark",
        json={"state": state, "detail": detail},
        headers=auth(token),
    )


async def inbox(client, token):
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "ListTasks",
        "params": {"includeArtifacts": True},
    }
    response = await client.post("/", json=body, headers=auth(token))
    return response.json()["result"]


# --- the three states -------------------------------------------------------

@pytest.mark.parametrize(
    "state,detail",
    [
        ("processed", "https://github.com/o/r/issues/1#issuecomment-1"),
        ("discarded", "not for me, it is lexboe's runner"),
        ("awaiting", "does the window include the docs PR?"),
    ],
)
async def test_recipient_can_mark_a_received_message(client, state, detail):
    task_id = await deliver(client, TOKEN_A, AGENT_B)

    response = await mark(client, TOKEN_B, task_id, state, detail)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == state
    assert body["detail"] == detail
    assert body["identity"] == IDENT_B


async def test_a_mark_can_be_corrected_and_awaiting_becomes_processed(client):
    """`awaiting` is a waiting room, so it has to be able to leave it."""
    task_id = await deliver(client, TOKEN_A, AGENT_B)

    await mark(client, TOKEN_B, task_id, "awaiting", "needs a decision from the owner")
    await mark(client, TOKEN_B, task_id, "processed", "decided in comment-99, applied")

    read = await client.get(f"/messages/{task_id}/mark", headers=auth(TOKEN_B))
    marks = read.json()["marks"]
    assert len(marks) == 1, "replacing a mark must not leave the old one behind"
    assert marks[0]["state"] == "processed"


# --- what a close costs -----------------------------------------------------

@pytest.mark.parametrize("state", ["processed", "discarded", "awaiting"])
async def test_a_mark_without_detail_is_refused(client, state):
    """The cost is the feature: a mark that costs nothing proves nothing."""
    task_id = await deliver(client, TOKEN_A, AGENT_B)

    response = await mark(client, TOKEN_B, task_id, state, "")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_mark"
    # The refusal has to say what would satisfy it, or it is a wall.
    assert state in response.json()["detail"]


async def test_an_unknown_state_is_refused_not_stored(client):
    task_id = await deliver(client, TOKEN_A, AGENT_B)

    response = await mark(client, TOKEN_B, task_id, "prosessed", "a typo of processed")

    assert response.status_code == 400
    assert "state must be one of" in response.json()["detail"]
    read = await client.get(f"/messages/{task_id}/mark", headers=auth(TOKEN_B))
    assert read.json()["marks"] == [], "a refused mark must not be stored"


async def test_a_body_that_is_not_an_object_is_refused(client):
    task_id = await deliver(client, TOKEN_A, AGENT_B)
    response = await client.post(
        f"/messages/{task_id}/mark", json=["processed"], headers=auth(TOKEN_B)
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_body"


async def test_invalid_json_is_refused(client):
    task_id = await deliver(client, TOKEN_A, AGENT_B)
    response = await client.post(
        f"/messages/{task_id}/mark",
        content=b"{not json",
        headers={**auth(TOKEN_B), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


# --- only the recipient writes ---------------------------------------------

async def test_a_stranger_cannot_mark_someone_elses_message(client):
    """The mark is written by the recipient, and by nobody else."""
    task_id = await deliver(client, TOKEN_A, AGENT_B)

    # The sender is not the recipient: they may read, never write.
    response = await mark(client, TOKEN_A, task_id, "processed", "marking my own send")

    assert response.status_code == 404
    assert response.json()["error"] == "not_in_your_mailbox"
    read = await client.get(f"/messages/{task_id}/mark", headers=auth(TOKEN_B))
    assert read.json()["marks"] == []


async def test_marking_a_message_that_does_not_exist_is_refused(client):
    response = await mark(client, TOKEN_B, "no-such-task", "processed", "ref: nothing")
    assert response.status_code == 404


# --- the sender's half of the contract -------------------------------------

async def test_the_sender_can_read_what_happened_to_their_message(client):
    """The original complaint was made from the sender's side, so this is the point."""
    task_id = await deliver(client, TOKEN_A, AGENT_B, "do the thing")
    await mark(client, TOKEN_B, task_id, "discarded", "already done in comment-7")

    read = await client.get(f"/messages/{task_id}/mark", headers=auth(TOKEN_A))

    assert read.status_code == 200
    assert read.json()["marks"][0]["state"] == "discarded"
    assert read.json()["marks"][0]["detail"] == "already done in comment-7"


async def test_the_sender_sees_a_listing_of_only_their_own_sends(client):
    mine = await deliver(client, TOKEN_A, AGENT_B, "from a")
    theirs = await deliver(client, TOKEN_B, AGENT_A, "from b")
    await mark(client, TOKEN_B, mine, "processed", "acted in sha deadbeef")
    await mark(client, TOKEN_A, theirs, "processed", "acted in sha cafe1234")

    listing = await client.get("/messages/marks?sent=true", headers=auth(TOKEN_A))

    sent = listing.json()["sent"]
    assert [row["task_id"] for row in sent] == [mine]
    assert sent[0]["identity"] == IDENT_B


async def test_an_unrelated_agent_cannot_read_the_mark(client):
    """Not the recipient and not the sender means no answer, and no hint either."""
    task_id = await deliver(client, TOKEN_A, AGENT_B)
    await mark(client, TOKEN_B, task_id, "processed", "acted in sha 1234567")

    # agent-a sent it, so use a third identity: a different session of agent-a is
    # trusted (same token), so the honest stranger here is agent-b's own send read
    # by nobody. Use a task agent-a never sent and never received.
    other = await deliver(client, TOKEN_B, AGENT_B, "b to itself")
    await mark(client, TOKEN_B, other, "processed", "acted in sha 7654321")

    read = await client.get(f"/messages/{other}/mark", headers=auth(TOKEN_A))

    assert read.status_code == 404
    assert read.json()["error"] == "no_mark_for_you"


async def test_reading_a_message_with_no_mark_says_so_without_leaking(client):
    task_id = await deliver(client, TOKEN_A, AGENT_B)
    read = await client.get(f"/messages/{task_id}/mark", headers=auth(TOKEN_A))
    # Sender, but nothing marked yet: no row names them, so there is nothing to show.
    assert read.status_code == 404


# --- the unprocessed mailbox ------------------------------------------------

async def test_marks_listing_separates_closed_from_awaiting(client):
    done = await deliver(client, TOKEN_A, AGENT_B, "one")
    dropped = await deliver(client, TOKEN_A, AGENT_B, "two")
    parked = await deliver(client, TOKEN_A, AGENT_B, "three")
    await mark(client, TOKEN_B, done, "processed", "acted in sha abcdef1")
    await mark(client, TOKEN_B, dropped, "discarded", "not applicable here")
    await mark(client, TOKEN_B, parked, "awaiting", "which window does this ride?")

    listing = (await client.get("/messages/marks", headers=auth(TOKEN_B))).json()

    assert listing["identity"] == IDENT_B
    assert listing["marked"][parked] == "awaiting"
    # Closed means out of the queue. Awaiting is NOT closed: something is still owed.
    assert sorted(listing["closed"]) == sorted([done, dropped])
    assert parked not in listing["closed"]


# --- compatibility: the promise made to eleven live clients ----------------

async def test_listtasks_returns_everything_even_after_marking(client):
    """An unfiltered mailbox read must be byte-for-byte the behaviour of before.

    This is the whole compatibility claim of the feature. Filtering is the caller
    leaving things out, never the server returning less — an old client polls
    ``ListTasks`` and must not lose a message because somebody marked it.
    """
    first = await deliver(client, TOKEN_A, AGENT_B, "one")
    second = await deliver(client, TOKEN_A, AGENT_B, "two")
    before = await inbox(client, TOKEN_B)

    await mark(client, TOKEN_B, first, "processed", "acted in sha abcdef1")
    await mark(client, TOKEN_B, second, "discarded", "nothing needed here")

    after = await inbox(client, TOKEN_B)
    assert after["totalSize"] == before["totalSize"] == 2
    assert {t["id"] for t in after["tasks"]} == {first, second}


async def test_marks_are_not_task_states(client):
    """Marking must not touch the Task, or protocol semantics change under clients."""
    task_id = await deliver(client, TOKEN_A, AGENT_B)
    await mark(client, TOKEN_B, task_id, "discarded", "not mine to do")

    body = {"jsonrpc": "2.0", "id": 3, "method": "GetTask", "params": {"id": task_id}}
    task = (await client.post("/", json=body, headers=auth(TOKEN_B))).json()["result"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


# --- auth and discovery -----------------------------------------------------

async def test_marks_routes_require_auth(client):
    task_id = await deliver(client, TOKEN_A, AGENT_B)
    assert (await client.get("/messages/marks")).status_code == 401
    assert (await client.get(f"/messages/{task_id}/mark")).status_code == 401
    unauth = await client.post(
        f"/messages/{task_id}/mark", json={"state": "processed", "detail": "x" * 20}
    )
    assert unauth.status_code == 401


async def test_the_card_announces_the_extension(client):
    card = (await client.get("/.well-known/agent-card.json")).json()
    uris = [e["uri"] for e in card["capabilities"]["extensions"]]
    assert "https://github.com/TLmaK0/a2a-hub/ext/message-marks/v1" in uris
    # Not required: a client that ignores it keeps working unchanged.
    assert all(e.get("required", False) is False for e in card["capabilities"]["extensions"])


# --- per-recipient keying ---------------------------------------------------

async def test_two_sessions_of_one_agent_mark_independently(client):
    """A broadcast is owed an answer by each session, not by whoever gets there first."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "m-broadcast",
                "role": "ROLE_USER",
                "parts": [{"text": "everyone read this"}],
                "metadata": {"recipient": AGENT_B},
            }
        },
    }
    response = await client.post("/", json=body, headers=auth(TOKEN_A))
    task_id = response.json()["result"]["task"]["id"]

    one = auth(TOKEN_B)
    one["A2A-Session"] = "one"
    two = auth(TOKEN_B)
    two["A2A-Session"] = "two"

    await client.post(
        f"/messages/{task_id}/mark",
        json={"state": "processed", "detail": "acted in sha 1111111"},
        headers=one,
    )

    # Session "two" has NOT closed it: one session must not discharge another's.
    listing = (await client.get("/messages/marks", headers=two)).json()
    assert listing["closed"] == []

    both = (await client.get(f"/messages/{task_id}/mark", headers=two)).json()
    assert len(both["marks"]) == 1
    assert both["marks"][0]["identity"] == f"{AGENT_B}/one"

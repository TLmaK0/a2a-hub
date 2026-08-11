"""Functional mailbox tests: SendMessage -> ListTasks/GetTask flow.

Covers the hub's core feature: route each message to its recipient's mailbox and
guarantee per-agent isolation.
"""

from __future__ import annotations

import logging

from conftest import (
    AGENT_B,
    IDENT_A,
    IDENT_B,
    TOKEN_A,
    TOKEN_B,
    rpc,
    send_message_params,
)


async def _send(client, sender_token, recipient, text="hello"):
    r = await rpc(
        client, "SendMessage", send_message_params(recipient, text), token=sender_token
    )
    assert r.status_code == 200
    return r.json()


async def test_delivery_to_recipient(client):
    body = await _send(client, TOKEN_A, IDENT_B, "hello B")
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    # The artifact carries the content and who sent it.
    art = task["artifacts"][0]
    assert art["parts"][0]["text"] == "hello B"
    assert art["metadata"]["sender"] == IDENT_A
    assert art["metadata"]["recipient"] == IDENT_B


async def test_recipient_sees_message_sender_does_not(client):
    await _send(client, TOKEN_A, IDENT_B, "for B")

    # B sees a task in its mailbox.
    lb = await rpc(client, "ListTasks", {}, token=TOKEN_B)
    assert lb.json()["result"]["totalSize"] == 1

    # A (the sender) sees nothing in its own: the mailbox belongs to the recipient.
    la = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    assert la.json()["result"]["totalSize"] == 0


async def test_gettask_by_owner_returns_content(client):
    body = await _send(client, TOKEN_A, IDENT_B, "secret")
    tid = body["result"]["task"]["id"]

    got = await rpc(client, "GetTask", {"id": tid}, token=TOKEN_B)
    task = got.json()["result"]
    assert task["id"] == tid
    assert task["artifacts"][0]["parts"][0]["text"] == "secret"


async def test_gettask_by_non_owner_not_found(client):
    body = await _send(client, TOKEN_A, IDENT_B)
    tid = body["result"]["task"]["id"]

    # Sender A cannot read B's mailbox.
    got = await rpc(client, "GetTask", {"id": tid}, token=TOKEN_A)
    assert got.json()["error"]["message"] == "Task not found"


async def test_list_with_artifacts(client):
    await _send(client, TOKEN_A, IDENT_B, "with attachment")
    lb = await rpc(client, "ListTasks", {"includeArtifacts": True}, token=TOKEN_B)
    art = lb.json()["result"]["tasks"][0]["artifacts"][0]
    assert art["parts"][0]["text"] == "with attachment"


async def test_missing_recipient_rejected(client):
    r = await rpc(
        client,
        "SendMessage",
        send_message_params(None, with_recipient=False),
        token=TOKEN_A,
    )
    task = r.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    # Nothing lands in any mailbox.
    assert (
        await rpc(client, "ListTasks", {}, token=TOKEN_B)
    ).json()["result"]["totalSize"] == 0


async def test_unknown_recipient_rejected(client):
    r = await rpc(
        client, "SendMessage", send_message_params("ghost"), token=TOKEN_A
    )
    task = r.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "unknown" in task["status"]["message"]["parts"][0]["text"]


async def test_self_message(client):
    # An agent can leave a note to itself.
    await _send(client, TOKEN_A, IDENT_A, "note to self")
    la = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    assert la.json()["result"]["totalSize"] == 1


async def test_multiple_messages_accumulate(client):
    for i in range(3):
        await _send(client, TOKEN_A, IDENT_B, f"m{i}")
    lb = await rpc(client, "ListTasks", {}, token=TOKEN_B)
    assert lb.json()["result"]["totalSize"] == 3


async def test_bidirectional_isolation(client):
    await _send(client, TOKEN_A, IDENT_B, "A->B")
    await _send(client, TOKEN_B, IDENT_A, "B->A")

    la = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    lb = await rpc(client, "ListTasks", {}, token=TOKEN_B)
    assert la.json()["result"]["totalSize"] == 1
    assert lb.json()["result"]["totalSize"] == 1


async def test_cancel_completed_task_not_reopened(client):
    """A delivered message cannot be un-delivered by cancelling its task.

    This asserted the opposite for a while, and passed by accident of timing. With
    no delay before the handler, `CancelTask` reached the task *before* delivery
    finished, cancelled it, and the test then read the returned task and saw
    COMPLETED — so it looked like proof that cancel left a completed task alone.
    Adding any latency ahead of the handler (measured with a bare `asyncio.sleep`,
    no database involved) flipped it to TASK_NOT_CANCELABLE, which is the behaviour
    the comment always claimed to be testing.

    Now it waits for the task to actually reach COMPLETED before cancelling, so the
    thing being asserted is the thing the name describes.
    """
    body = await _send(client, TOKEN_A, IDENT_B)
    tid = body["result"]["task"]["id"]

    # Delivery is what makes the task COMPLETED; assert it rather than assume it.
    got = await rpc(client, "GetTask", {"id": tid}, token=TOKEN_B)
    assert got.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"

    r = await rpc(client, "CancelTask", {"id": tid}, token=TOKEN_B)
    error = r.json()["error"]
    assert error["data"][0]["reason"] == "TASK_NOT_CANCELABLE"

    # And the message is still there to be read: refusing to cancel is not a no-op
    # that lost it, it is the mailbox keeping what was delivered.
    still = await rpc(client, "GetTask", {"id": tid}, token=TOKEN_B)
    assert still.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


# --- a merged mailbox must be readable to the end ------------------------------


async def _drain(client, token: str, *, page_size: int) -> tuple[list, int, int]:
    """Follow the page tokens the way a correct client does: until empty."""
    ids, pages, total = [], 0, 0
    params: dict = {"pageSize": page_size}
    while True:
        body = (await rpc(client, "ListTasks", params, token=token)).json()["result"]
        pages += 1
        total = int(body.get("totalSize", 0))
        ids.extend(t["id"] for t in body.get("tasks", []))
        next_token = body.get("nextPageToken") or ""
        if not next_token:
            return ids, total, pages
        params = {"pageSize": page_size, "pageToken": next_token}
        assert pages < 50, "pagination did not terminate"


async def test_an_empty_page_token_means_there_is_nothing_left(client):
    """The invariant, checkable without knowing anything about mailbox merging.

    This is what failed in production on 2026-08-13: totalSize said 115, fifty tasks
    came back, and the token said "no more". Two fields of one response cannot
    disagree — either there is a token, or total_size equals what was returned.
    """
    for _ in range(7):
        await _send(client, TOKEN_A, IDENT_B)

    body = (await rpc(client, "ListTasks", {"pageSize": 3}, token=TOKEN_B)).json()[
        "result"
    ]

    if not (body.get("nextPageToken") or ""):
        assert body["totalSize"] == len(body["tasks"])


async def test_every_task_is_reachable_by_following_the_tokens(client):
    """A mailbox over one page must be readable in full, not just its newest page."""
    sent = set()
    for _ in range(7):
        body = await _send(client, TOKEN_A, IDENT_B)
        sent.add(body["result"]["task"]["id"])
    # ...and some to the session's own mailbox, so both merged mailboxes are in play.
    for _ in range(5):
        body = await _send(client, TOKEN_A, AGENT_B)
        sent.add(body["result"]["task"]["id"])

    seen, total, pages = await _drain(client, TOKEN_B, page_size=3)

    assert pages > 1, "the test is pointless if it all fits in one page"
    assert sent <= set(seen), "tasks that were delivered could not be read back"
    assert len(seen) == total


async def test_a_malformed_page_token_restarts_instead_of_failing(client):
    """Losing access to your own mailbox over a value you never built is worse."""
    await _send(client, TOKEN_A, IDENT_B)

    response = await rpc(
        client, "ListTasks", {"pageSize": 2, "pageToken": "not-a-token"}, token=TOKEN_B
    )

    assert response.status_code == 200
    assert response.json()["result"]["tasks"]


# --- rejections must leave a trace for whoever operates the hub ---------------


async def test_an_unknown_recipient_is_logged_with_sender_and_recipient(client, caplog):
    """The check that failed on 2026-08-11: grepping the log found nothing.

    The transport succeeds, so the access log says 200, and the REJECTED task is
    stored under the sender — right, but it leaves the operator unable to tell
    "never sent" from "refused" from "delivered but unread".
    """
    with caplog.at_level(logging.WARNING, logger="a2a_hub.executor"):
        response = await rpc(
            client,
            "SendMessage",
            send_message_params("nobody-here/at-all"),
            token=TOKEN_A,
        )

    assert response.status_code == 200  # the transport really did succeed
    assert response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"

    # What the issue asks for: sender and attempted recipient, at WARNING.
    rejections = [r for r in caplog.records if "rejected" in r.message]
    assert len(rejections) == 1
    logged = rejections[0].getMessage()
    assert "nobody-here/at-all" in logged
    assert IDENT_A in logged
    assert rejections[0].levelname == "WARNING"


async def test_a_missing_recipient_is_logged_too(client, caplog):
    """Misaddressing includes forgetting the address entirely."""
    with caplog.at_level(logging.WARNING, logger="a2a_hub.executor"):
        await rpc(
            client,
            "SendMessage",
            send_message_params(None, with_recipient=False),
            token=TOKEN_A,
        )

    assert any("no recipient metadata" in r.getMessage() for r in caplog.records)


async def test_a_successful_delivery_logs_no_warning(client, caplog):
    """Otherwise the signal drowns: a warning per delivery is a warning per nothing."""
    with caplog.at_level(logging.WARNING, logger="a2a_hub.executor"):
        await rpc(client, "SendMessage", send_message_params(IDENT_B), token=TOKEN_A)

    assert [r for r in caplog.records if "rejected" in r.message] == []


async def test_the_log_never_carries_the_message_body(client, caplog):
    """Bodies are other agents' content; the addresses are enough to diagnose."""
    with caplog.at_level(logging.WARNING, logger="a2a_hub.executor"):
        await rpc(
            client,
            "SendMessage",
            send_message_params("nobody-here/at-all", text="secret payload"),
            token=TOKEN_A,
        )

    assert all("secret payload" not in r.getMessage() for r in caplog.records)

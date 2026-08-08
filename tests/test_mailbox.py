"""Functional mailbox tests: SendMessage -> ListTasks/GetTask flow.

Covers the hub's core feature: route each message to its recipient's mailbox and
guarantee per-agent isolation.
"""

from __future__ import annotations

from conftest import (
    AGENT_A,
    AGENT_B,
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
    body = await _send(client, TOKEN_A, AGENT_B, "hello B")
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    # The artifact carries the content and who sent it.
    art = task["artifacts"][0]
    assert art["parts"][0]["text"] == "hello B"
    assert art["metadata"]["sender"] == AGENT_A
    assert art["metadata"]["recipient"] == AGENT_B


async def test_recipient_sees_message_sender_does_not(client):
    await _send(client, TOKEN_A, AGENT_B, "for B")

    # B sees a task in its mailbox.
    lb = await rpc(client, "ListTasks", {}, token=TOKEN_B)
    assert lb.json()["result"]["totalSize"] == 1

    # A (the sender) sees nothing in its own: the mailbox belongs to the recipient.
    la = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    assert la.json()["result"]["totalSize"] == 0


async def test_gettask_by_owner_returns_content(client):
    body = await _send(client, TOKEN_A, AGENT_B, "secret")
    tid = body["result"]["task"]["id"]

    got = await rpc(client, "GetTask", {"id": tid}, token=TOKEN_B)
    task = got.json()["result"]
    assert task["id"] == tid
    assert task["artifacts"][0]["parts"][0]["text"] == "secret"


async def test_gettask_by_non_owner_not_found(client):
    body = await _send(client, TOKEN_A, AGENT_B)
    tid = body["result"]["task"]["id"]

    # Sender A cannot read B's mailbox.
    got = await rpc(client, "GetTask", {"id": tid}, token=TOKEN_A)
    assert got.json()["error"]["message"] == "Task not found"


async def test_list_with_artifacts(client):
    await _send(client, TOKEN_A, AGENT_B, "with attachment")
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
    await _send(client, TOKEN_A, AGENT_A, "note to self")
    la = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    assert la.json()["result"]["totalSize"] == 1


async def test_multiple_messages_accumulate(client):
    for i in range(3):
        await _send(client, TOKEN_A, AGENT_B, f"m{i}")
    lb = await rpc(client, "ListTasks", {}, token=TOKEN_B)
    assert lb.json()["result"]["totalSize"] == 3


async def test_bidirectional_isolation(client):
    await _send(client, TOKEN_A, AGENT_B, "A->B")
    await _send(client, TOKEN_B, AGENT_A, "B->A")

    la = await rpc(client, "ListTasks", {}, token=TOKEN_A)
    lb = await rpc(client, "ListTasks", {}, token=TOKEN_B)
    assert la.json()["result"]["totalSize"] == 1
    assert lb.json()["result"]["totalSize"] == 1


async def test_cancel_completed_task_not_reopened(client):
    body = await _send(client, TOKEN_A, AGENT_B)
    tid = body["result"]["task"]["id"]
    # Delivery is immediate: the task is already COMPLETED. Cancel leaves it as is.
    r = await rpc(client, "CancelTask", {"id": tid}, token=TOKEN_B)
    task = r.json()["result"]
    assert task["id"] == tid
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"

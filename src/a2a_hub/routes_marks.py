"""HTTP routes for message marks (declared via an A2A extension).

Outside JSON-RPC on purpose, like the register: A2A's core methods are about messages
and tasks, and "what happened to this message afterwards" is an extra capability. The
protocol's own way to offer one is an extension declared in the Agent Card, not new
methods bolted onto the schema the SDK implements.

The compatibility rule that governs this whole file: **an unfiltered ``ListTasks``
returns exactly what it returned before any of this existed.** Eleven live clients
poll this hub; none of them knows these routes, and none of them has to. Filtering a
mailbox is the caller asking what it has already closed and leaving those out — never
the server quietly returning less.
"""

from __future__ import annotations

import json

from google.protobuf.json_format import MessageToDict
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.server.routes import DefaultServerCallContextBuilder

from a2a_hub.marks import (
    CLOSED_STATES,
    MarkError,
    MessageMarks,
    clean_mark,
)


#: ``/messages/marks`` lists what the caller has marked; the per-message path marks
#: one and reads it back.
MARKS_PATH = "/messages/marks"
MARK_PATH = "/messages/{task_id}/mark"

#: URI announced in the Agent Card so clients discover the capability.
MARKS_EXTENSION_URI = "https://github.com/TLmaK0/a2a-hub/ext/message-marks/v1"

_context_builder = DefaultServerCallContextBuilder()


def _sender_of(task) -> str:
    """The sender recorded on the delivered message, or ``""``.

    Read from the artifact the executor wrote, which is the only place the sender
    survives: the task itself is owned by the *recipient*.
    """
    for artifact in getattr(task, "artifacts", []) or []:
        metadata = getattr(artifact, "metadata", None)
        if metadata is None:
            continue
        as_dict = MessageToDict(metadata) if metadata else {}
        sender = as_dict.get("sender")
        if isinstance(sender, str) and sender:
            return sender
    return ""


def build_marks_routes(store, message_marks: MessageMarks) -> list[Route]:
    """Routes for marking a message and for reading marks back.

    Args:
        store: the task store, used **as the authorisation oracle**. If a caller
            cannot read a task from their own mailbox they may not mark it, and that
            check is exactly the isolation the store already enforces — reimplementing
            it here would be a second rule that could disagree with the first.
        message_marks: mark storage.
    """

    async def _readable_task(request: Request, task_id: str):
        context = _context_builder.build(request)
        return await store.get(task_id, context)

    async def set_mark(request: Request) -> JSONResponse:
        """Mark a message. Only its recipient may, and the identity is not theirs to choose."""
        task_id = request.path_params["task_id"]
        try:
            payload = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as error:
            return JSONResponse(
                {"error": "invalid_json", "detail": str(error)}, status_code=400
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "invalid_body", "detail": "expected a JSON object"},
                status_code=400,
            )

        try:
            mark = clean_mark(payload)
        except MarkError as error:
            return JSONResponse(
                {"error": "invalid_mark", "detail": str(error)}, status_code=400
            )

        identity = request.user.username
        task = await _readable_task(request, task_id)
        if task is None:
            # Deliberately the same answer for "no such message" and "not in your
            # mailbox": distinguishing them would tell a caller what exists in
            # someone else's.
            return JSONResponse(
                {
                    "error": "not_in_your_mailbox",
                    "detail": (
                        f"{task_id} is not a message you received; only the "
                        "recipient marks a message"
                    ),
                },
                status_code=404,
            )

        stored = await message_marks.set_mark(
            task_id, identity, mark, sender=_sender_of(task)
        )
        return JSONResponse(stored, status_code=200)

    async def get_mark(request: Request) -> JSONResponse:
        """Read the marks on one message.

        Two callers may: the **recipient**, from their own mailbox, and the
        **sender**, about a message they sent. The sender's path answers only from
        rows that name them as sender — so it cannot be used to probe what exists in
        a mailbox they do not own.
        """
        task_id = request.path_params["task_id"]
        identity = request.user.username

        task = await _readable_task(request, task_id)
        entries = await message_marks.get_marks(task_id)
        if task is not None:
            return JSONResponse({"task_id": task_id, "marks": entries})

        mine_as_sender = [e for e in entries if e["sender"] == identity]
        if mine_as_sender:
            return JSONResponse({"task_id": task_id, "marks": mine_as_sender})

        return JSONResponse(
            {
                "error": "no_mark_for_you",
                "detail": (
                    f"no mark on {task_id} that you can read: it is not a message "
                    "you received, and no mark on it records you as the sender"
                ),
            },
            status_code=404,
        )

    async def list_marks(request: Request) -> JSONResponse:
        """What the caller has marked, or what happened to what they sent.

        ``?sent=true`` switches sides: instead of "what I have closed", it answers
        "what did the recipients do with my messages", which is the question the
        owner's original complaint was made from.
        """
        identity = request.user.username
        if request.query_params.get("sent", "").lower() in {"1", "true", "yes"}:
            return JSONResponse(
                {"identity": identity, "sent": await message_marks.sent_by(identity)}
            )

        marked = await message_marks.marked_by(identity)
        return JSONResponse(
            {
                "identity": identity,
                "marked": marked,
                # Spelled out rather than left to the client to infer: `awaiting` is
                # NOT closed, and a client that guessed otherwise would hide exactly
                # the messages someone is still waiting on.
                "closed": sorted(
                    task_id
                    for task_id, state in marked.items()
                    if state in CLOSED_STATES
                ),
            }
        )

    return [
        Route(MARKS_PATH, list_marks, methods=["GET"]),
        Route(MARK_PATH, set_mark, methods=["POST"]),
        Route(MARK_PATH, get_mark, methods=["GET"]),
    ]

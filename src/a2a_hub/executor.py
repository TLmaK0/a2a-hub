"""Hub AgentExecutor: per-recipient *store-and-forward* mailbox.

The hub does not "reason": it receives a ``SendMessage``, tags it with its
recipient and persists it as a ``Task`` **owned by the recipient**, so that agent
picks it up on its next ``ListTasks``. This is the standard A2A Task model; there
is no home-grown protocol (see AGENTS.md § Architecture decisions).

Owner-based routing
-------------------
``DatabaseTaskStore`` scopes each task to an *owner* resolved from the context.
By default that would be the caller, but in a mailbox the owner must be the
**recipient**:

- On ``SendMessage`` the executor sets ``owner_override = recipient`` in the
  context state **before** emitting any event; the store uses it when saving.
- On ``ListTasks`` / ``GetTask`` there is no executor, no override, and the
  resolver falls back to the calling agent: each agent only sees its own mailbox.
"""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Part, Role, Task, TaskState, TaskStatus


#: Metadata key (on the message or the request) holding the recipient agent.
RECIPIENT_KEY = "recipient"

#: Key in ``ServerCallContext.state`` holding the forced owner when saving.
OWNER_OVERRIDE_KEY = "owner_override"


def hub_owner_resolver(context: ServerCallContext) -> str:
    """Resolve the owner of a task.

    Prefers the ``owner_override`` set by the executor during ``SendMessage``
    (the recipient); otherwise uses the authenticated agent name (the caller),
    so ``ListTasks``/``GetTask`` return only its own mailbox.
    """
    override = context.state.get(OWNER_OVERRIDE_KEY)
    if isinstance(override, str) and override:
        return override
    return context.user.user_name


def _struct_to_dict(struct: object) -> dict:
    """Convert a ``google.protobuf.Struct`` into a plain dict (or ``{}``)."""
    if struct is None:
        return {}
    return MessageToDict(struct)


def _extract_recipient(context: RequestContext) -> str | None:
    """Get the recipient from the message metadata (or the request metadata)."""
    sources: list[dict] = []
    if context.message is not None:
        sources.append(_struct_to_dict(context.message.metadata))
    sources.append(context.metadata or {})

    for meta in sources:
        value = meta.get(RECIPIENT_KEY)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class HubAgentExecutor(AgentExecutor):
    """Mailbox executor: delivers the message to the recipient as a Task."""

    def __init__(self, known_agents: set[str] | None = None) -> None:
        """Args:
        known_agents: if set, recipients outside the set are rejected (avoids
            messages to non-existent mailboxes). ``None`` = no validation.
        """
        self._known_agents = known_agents

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Process a ``SendMessage``: validate, route and persist the delivery."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        sender = context.call_context.user.user_name

        # The framework requires a Task to be enqueued before any status update.
        await self._ensure_task(context, event_queue)

        if context.message is None:
            await updater.reject(
                message=self._notice(updater, "no message to deliver"),
            )
            return

        recipient = _extract_recipient(context)
        if not recipient:
            await updater.reject(
                message=self._notice(
                    updater,
                    f"missing '{RECIPIENT_KEY}' metadata with the recipient agent",
                ),
            )
            return

        if self._known_agents is not None and recipient not in self._known_agents:
            await updater.reject(
                message=self._notice(
                    updater, f"unknown recipient: {recipient!r}"
                ),
            )
            return

        # Route: from here on the task is owned by the recipient.
        context.call_context.state[OWNER_OVERRIDE_KEY] = recipient

        await updater.add_artifact(
            list(context.message.parts),
            name="message",
            metadata={
                "sender": sender,
                "recipient": recipient,
                "source_message_id": context.message.message_id,
            },
        )
        await updater.complete(
            message=self._notice(updater, f"delivered to {recipient}"),
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Cancel the task (set CANCELED state)."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(
            message=self._notice(updater, "canceled"),
        )

    @staticmethod
    async def _ensure_task(
        context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Enqueue the initial ``Task`` (SUBMITTED state) if it does not exist."""
        if context.current_task is not None:
            return
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )

    @staticmethod
    def _notice(updater: TaskUpdater, text: str):
        """Create an agent message with a textual note for the status."""
        msg = updater.new_agent_message([Part(text=text)])
        msg.role = Role.ROLE_AGENT
        return msg

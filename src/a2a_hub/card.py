"""Hub Agent Card, served at ``/.well-known/agent-card.json``.

This is the A2A discovery point: it advertises capabilities, transport, the auth
scheme (bearer) and the mailbox skill. Public (no auth), by protocol design.
"""

from __future__ import annotations

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils import TransportProtocol

from a2a_hub import __version__
from a2a_hub.auth import SESSION_EXTENSION_URI, SESSION_HEADER
from a2a_hub.executor import RECIPIENT_EXTENSION_URI, RECIPIENT_KEY
from a2a_hub.routes_marks import MARKS_EXTENSION_URI
from a2a_hub.routes_registry import REGISTRY_EXTENSION_URI


#: Name of the security scheme declared in the card.
SECURITY_SCHEME_NAME = "bearer"


def build_agent_card(public_url: str, rpc_url: str = "/") -> AgentCard:
    """Build the hub's Agent Card.

    Args:
        public_url: public base URL of the server (e.g. ``https://a2a.example.com``).
        rpc_url: path of the A2A JSON-RPC endpoint.
    """
    endpoint = public_url.rstrip("/") + "/" + rpc_url.lstrip("/")

    card = AgentCard(
        name="a2a-hub",
        description=(
            "A2A store-and-forward hub: a mailbox between agents. Routes each "
            "message to its recipient's mailbox (Task), which is picked up with "
            "ListTasks/GetTask."
        ),
        version=__version__,
        supported_interfaces=[
            AgentInterface(
                url=endpoint,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extensions=[
                # Two conventions this hub has always relied on but never declared:
                # a plain A2A client that only read this card could not tell they
                # exist. `required=True` because, unlike the two below, these are
                # not opt-in — using the hub's core methods at all depends on them.
                AgentExtension(
                    uri=RECIPIENT_EXTENSION_URI,
                    description=(
                        f"SendMessage routes by the {RECIPIENT_KEY!r} key in the "
                        "message's metadata (e.g. metadata: "
                        '{"recipient": "agent-b"}); not part of A2A core, which has '
                        "no addressing field of its own. A message with none is "
                        "rejected."
                    ),
                    required=True,
                ),
                AgentExtension(
                    uri=SESSION_EXTENSION_URI,
                    description=(
                        f"Every authenticated request must carry the "
                        f"{SESSION_HEADER!r} header, giving the token's principal a "
                        "sub-identity (principal/session) with its own mailbox. "
                        "Missing or malformed, the request is refused (400)."
                    ),
                    required=True,
                ),
                # The register and the marks are discoverable, opt-in capabilities
                # beyond the core methods, so clients find them instead of being
                # told: a client that ignores either keeps working unchanged.
                AgentExtension(
                    uri=REGISTRY_EXTENSION_URI,
                    description=(
                        "Agents declare identity, role, host, projects and status; "
                        "the hub stamps last-seen itself, so a dead agent cannot "
                        "claim to be alive. POST /agents/register, GET /agents."
                    ),
                    required=False,
                ),
                AgentExtension(
                    uri=MARKS_EXTENSION_URI,
                    description=(
                        "Recipients mark a message processed, discarded or awaiting "
                        "a decision, each with the detail that close required; the "
                        "sender may read what happened to what they sent. "
                        "POST/GET /messages/{task_id}/mark, GET /messages/marks."
                    ),
                    required=False,
                ),
            ],
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="mailbox",
                name="Store-and-forward mailbox",
                description=(
                    "Delivers a message to the mailbox of the agent named in the "
                    "'recipient' metadata. The recipient picks it up with ListTasks."
                ),
                tags=["mailbox", "relay", "a2a"],
                examples=[
                    'SendMessage with metadata {"recipient": "agent-b"}',
                ],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )

    scheme = SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(
            scheme="bearer",
            description="One token per agent (Authorization: Bearer <token>).",
        )
    )
    card.security_schemes[SECURITY_SCHEME_NAME].CopyFrom(scheme)

    requirement = SecurityRequirement()
    requirement.schemes[SECURITY_SCHEME_NAME].CopyFrom(StringList())
    card.security_requirements.append(requirement)

    return card

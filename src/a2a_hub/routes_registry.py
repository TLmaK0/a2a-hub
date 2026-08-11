"""HTTP routes for the agent register (declared via an A2A extension).

These live outside JSON-RPC on purpose. A2A's core methods are about messages and
tasks; announcing *who is connected* is an extra capability, and the protocol's own
way to offer one is to declare an extension in the Agent Card rather than to add
methods to the schema the SDK implements. `/healthz` already sets the precedent for
a non-JSON-RPC route on this server.

Both routes sit behind the same bearer auth as everything else.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_hub.registry import (
    AgentRegistry,
    NotRegisteredError,
    RegistrationError,
    clean_declaration,
)


#: Path of the register. `.../register` declares, the base path lists.
REGISTRY_PATH = "/agents"
REGISTER_PATH = "/agents/register"
STATUS_PATH = "/agents/status"

#: URI announced in the Agent Card so clients can discover the capability.
REGISTRY_EXTENSION_URI = "https://github.com/TLmaK0/a2a-hub/ext/agent-registry/v1"


def build_registry_routes(agents: AgentRegistry) -> list[Route]:
    """Routes for declaring an identity and for listing what is registered."""

    async def register(request: Request) -> JSONResponse:
        """Declare who the caller is. The identity comes from the token."""
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
            declaration = clean_declaration(payload)
        except RegistrationError as error:
            return JSONResponse(
                {"error": "invalid_declaration", "detail": str(error)}, status_code=400
            )

        # Never from the body: an agent describes itself, it does not choose who it is.
        # Starlette exposes the authenticated name as `username` (the SDK context uses
        # `user_name`); the middleware put the full `principal/session` identity there.
        identity = request.user.username
        await agents.declare(identity, declaration)
        return JSONResponse({"identity": identity, **declaration}, status_code=200)

    async def list_agents(_request: Request) -> JSONResponse:
        """Everyone the hub knows about, for any authenticated caller.

        No filtering by role or principal: the question this answers is "is there
        another manager?", which cannot be answered from a filtered view.
        """
        return JSONResponse({"agents": await agents.list_agents()})

    async def update_status(request: Request) -> JSONResponse:
        """Move only the "what I am doing" line of an existing introduction."""
        try:
            payload = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as error:
            return JSONResponse(
                {"error": "invalid_json", "detail": str(error)}, status_code=400
            )
        if not isinstance(payload, dict) or "status" not in payload:
            return JSONResponse(
                {"error": "invalid_body", "detail": "expected {\"status\": ...}"},
                status_code=400,
            )

        identity = request.user.username
        try:
            status = await agents.update_status(identity, payload["status"])
        except NotRegisteredError as error:
            return JSONResponse(
                {"error": "not_registered", "detail": str(error)}, status_code=409
            )
        return JSONResponse({"identity": identity, "status": status})

    return [
        Route(REGISTER_PATH, register, methods=["POST"]),
        Route(STATUS_PATH, update_status, methods=["POST"]),
        Route(REGISTRY_PATH, list_agents, methods=["GET"]),
    ]

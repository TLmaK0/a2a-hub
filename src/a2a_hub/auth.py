"""Bearer token authentication (one token = one agent).

Hard rules (see AGENTS.md § Security):

- The server is **never exposed without auth**: anything that is not public
  discovery (Agent Card) or the healthcheck requires a valid
  ``Authorization: Bearer <token>`` header, or it returns ``401``.
- Tokens **can be rotated** without rebuilding the image: they live in a
  hot-swappable ``TokenRegistry`` (in production, reloaded from the Secret).
- Token comparison is **constant time** to avoid leaking via timing.
"""

from __future__ import annotations

import hmac

from starlette.authentication import AuthCredentials, SimpleUser
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from a2a.server.context import ServerCallContext
from a2a.server.routes import DefaultServerCallContextBuilder
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH


HEALTH_PATH = "/healthz"

# Public paths: A2A discovery and the healthcheck. Nothing else is exposed.
PUBLIC_PATHS: frozenset[str] = frozenset({AGENT_CARD_WELL_KNOWN_PATH, HEALTH_PATH})

# Request headers that must never end up stored in the call context (and thus never
# in any context/state dump or log).
SENSITIVE_HEADERS: frozenset[str] = frozenset({"authorization", "cookie"})


class TokenRegistry:
    """token->agent map with constant-time lookup and hot rotation."""

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self._tokens: dict[str, str] = dict(tokens or {})

    def resolve(self, token: str) -> str | None:
        """Return the agent that owns the token, or ``None`` if unknown.

        Iterates over every entry comparing with ``hmac.compare_digest`` so that
        timing does not depend on how much of the token prefix matches.
        """
        match: str | None = None
        for known, agent in self._tokens.items():
            if hmac.compare_digest(known, token):
                match = agent
        return match

    def replace(self, tokens: dict[str, str]) -> None:
        """Replace the token set (rotation without restarting the process)."""
        self._tokens = dict(tokens)

    def __len__(self) -> int:
        return len(self._tokens)


def _extract_bearer(header: str | None) -> str | None:
    """Extract ``<token>`` from an ``Authorization: Bearer <token>`` header."""
    if not header:
        return None
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


class BearerAuthMiddleware:
    """ASGI middleware that requires a bearer token except on public paths.

    On success it injects ``scope['user']`` (a ``SimpleUser`` with the agent
    name) and ``scope['auth']``, which the SDK's ``DefaultServerCallContextBuilder``
    picks up to build the ``ServerCallContext`` with the agent identity.
    """

    def __init__(self, app: ASGIApp, registry: TokenRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        token = _extract_bearer(conn.headers.get("authorization"))
        agent = self.registry.resolve(token) if token else None

        if agent is None:
            response = JSONResponse(
                {"error": "unauthorized", "detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        scope["user"] = SimpleUser(agent)
        scope["auth"] = AuthCredentials(["authenticated"])
        await self.app(scope, receive, send)


class RedactingContextBuilder(DefaultServerCallContextBuilder):
    """Context builder that keeps the bearer token out of ``ServerCallContext``.

    The SDK default copies **all** request headers (including ``Authorization``)
    into ``context.state['headers']``. We drop the sensitive ones so a token can
    never leak through a context/state dump (e.g. if DEBUG logging is enabled).
    """

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        headers = context.state.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in SENSITIVE_HEADERS:
                    headers.pop(name, None)
        return context


class MaxBodySizeMiddleware:
    """Reject requests whose declared body exceeds ``max_bytes`` with ``413``.

    Application-level DoS guard so it protects every deployment (Docker-only too),
    not just those behind a proxy. ``max_bytes <= 0`` disables the check.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.max_bytes > 0:
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    try:
                        too_large = int(value) > self.max_bytes
                    except ValueError:
                        too_large = False
                    if too_large:
                        response = JSONResponse(
                            {
                                "error": "payload_too_large",
                                "detail": f"request body exceeds {self.max_bytes} bytes",
                            },
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
                    break
        await self.app(scope, receive, send)

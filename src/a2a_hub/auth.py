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
import re

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

#: Header a client uses to claim its per-session identity under its own token.
#: **Mandatory**: every authenticated request must declare a session, so two
#: processes can never end up sharing one mailbox by accident.
SESSION_HEADER = "a2a-session"

#: Separator between the principal (from the token) and the session name.
IDENTITY_SEPARATOR = "/"

#: Accepted session names: keeps identities readable and safe as storage keys.
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def principal_of(identity: str) -> str:
    """Return the token-backed principal of an identity (``machine/session``)."""
    return identity.split(IDENTITY_SEPARATOR, 1)[0]


def is_valid_identity(identity: str, known_principals: set[str] | None) -> bool:
    """Whether ``identity`` names a reachable mailbox.

    Identities are always ``principal/session``: the principal must be a known agent
    (someone holds its token) and the session must be well formed. A bare principal
    is **not** addressable — nobody could read it, since every client must declare a
    session to authenticate.
    """
    principal, separator, session = identity.partition(IDENTITY_SEPARATOR)
    if not principal or not separator or not session:
        return False
    if known_principals is not None and principal not in known_principals:
        return False
    return bool(SESSION_PATTERN.match(session))


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
    identity) and ``scope['auth']``, which the SDK's
    ``DefaultServerCallContextBuilder`` picks up to build the ``ServerCallContext``.

    Per-session identities: the token identifies the **principal** (one token = one
    machine/agent). A client may claim a sub-identity for its session with the
    ``A2A-Session`` header, yielding ``principal/session`` — its own mailbox. A
    session is always claimed **under its own token's principal**, so sessions of one
    machine can talk to each other while staying isolated from every other machine.
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

        # A session is mandatory: it is what keeps two processes holding the same
        # token from sharing one mailbox.
        session = (conn.headers.get(SESSION_HEADER) or "").strip()
        if not SESSION_PATTERN.match(session):
            response = JSONResponse(
                {
                    "error": "invalid_session",
                    "detail": (
                        f"the {SESSION_HEADER} header is required and must match "
                        f"{SESSION_PATTERN.pattern}"
                    ),
                },
                status_code=400,
            )
            await response(scope, receive, send)
            return

        # The session always hangs off this token's principal: a client cannot claim
        # an identity belonging to another machine.
        identity = f"{agent}{IDENTITY_SEPARATOR}{session}"

        scope["user"] = SimpleUser(identity)
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

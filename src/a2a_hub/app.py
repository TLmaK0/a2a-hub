"""Starlette application factory for the A2A hub.

Wires the pieces together: persistent task store, ``HubAgentExecutor``, A2A routes
(JSON-RPC + Agent Card), healthcheck and the bearer-token auth middleware.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes

from a2a_hub.auth import (
    HEALTH_PATH,
    BearerAuthMiddleware,
    MaxBodySizeMiddleware,
    RedactingContextBuilder,
    TokenRegistry,
)
from a2a_hub.card import build_agent_card
from a2a_hub.config import Settings
from a2a_hub.executor import HubAgentExecutor
from a2a_hub.store import create_engine, create_task_store


async def _healthz(_request: Request) -> JSONResponse:
    """Public healthcheck (for k8s probes)."""
    return JSONResponse({"status": "ok"})


def create_app(settings: Settings) -> Starlette:
    """Build the Starlette application from the configuration.

    Fails closed and loud: if no tokens are configured the process aborts at
    startup (the container crashes) instead of coming up unusable. The
    ``AsyncEngine`` is disposed on the app's ``shutdown`` lifecycle event.
    """
    if not settings.tokens:
        raise RuntimeError(
            "No agent tokens configured (A2A_HUB_TOKENS is empty). Refusing to "
            "start: without tokens no agent could authenticate. See .env.example."
        )

    registry = TokenRegistry(settings.tokens)
    known_agents = set(settings.tokens.values())

    engine = create_engine(settings.db_url)
    task_store = create_task_store(engine)

    agent_card = build_agent_card(settings.public_url, settings.rpc_url)

    handler = DefaultRequestHandler(
        agent_executor=HubAgentExecutor(known_agents=known_agents),
        task_store=task_store,
        agent_card=agent_card,
    )

    routes: list[Route] = [
        Route(HEALTH_PATH, _healthz, methods=["GET"]),
        *create_agent_card_routes(agent_card),
        # RedactingContextBuilder keeps the bearer token out of the call context.
        *create_jsonrpc_routes(
            handler, settings.rpc_url, context_builder=RedactingContextBuilder()
        ),
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            await handler.aclose()
            await engine.dispose()

    app = Starlette(
        routes=routes,
        middleware=[
            # Outermost: reject oversized bodies before anything reads them.
            Middleware(MaxBodySizeMiddleware, max_bytes=settings.max_body_bytes),
            Middleware(BearerAuthMiddleware, registry=registry),
        ],
        lifespan=lifespan,
    )
    # Exposed for tests and introspection.
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.task_store = task_store
    app.state.agent_card = agent_card
    return app

"""Server startup with uvicorn.

Local use:  ``uv run a2a-hub``  (or ``python -m a2a_hub``).
Configured via the environment (see ``.env.example`` and ``config.py``).
"""

from __future__ import annotations

import uvicorn

from a2a_hub.app import create_app
from a2a_hub.config import Settings


def run(settings: Settings | None = None) -> None:
    """Start uvicorn with the hub app.

    Serves HTTPS directly when ``tls_certfile``/``tls_keyfile`` are set, so the
    service is self-contained (secure token auth without a TLS-terminating proxy).
    """
    settings = settings or Settings.from_env()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.tls_certfile,
        ssl_keyfile=settings.tls_keyfile,
    )


def main() -> None:
    """Console entry point (``a2a-hub``)."""
    run()

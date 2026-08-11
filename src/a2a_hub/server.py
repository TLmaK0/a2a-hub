"""Server startup with uvicorn.

Local use:  ``uv run a2a-hub``  (or ``python -m a2a_hub``).
Configured via the environment (see ``.env.example`` and ``config.py``).
"""

from __future__ import annotations

import copy

import uvicorn

from a2a_hub.app import create_app
from a2a_hub.config import Settings


def _log_config() -> dict:
    """uvicorn's logging config, extended so this package's logs look like the rest.

    Without this, a warning from ``a2a_hub`` propagates to a root logger that has no
    handler and comes out through Python's last-resort one: no level, no timestamp,
    a bare line among uvicorn's ``INFO:`` access lines. It would be *present* and
    practically unfindable — grepping for ``WARNING`` would miss it — which is the
    same failure as not logging it at all. Measured, not assumed: the probe printed
    ``delivery rejected: PROBE`` with no prefix.
    """
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["loggers"]["a2a_hub"] = {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    return config


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
        log_config=_log_config(),
    )


def main() -> None:
    """Console entry point (``a2a-hub``)."""
    run()

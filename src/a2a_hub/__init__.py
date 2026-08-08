"""a2a-hub: A2A store-and-forward server on top of the official ``a2a-sdk``.

A minimal layer (executor + task store + auth + deployment) so agents can talk over
HTTPS with authentication. It does not reimplement the A2A protocol.
"""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> None:
    """Console entry point: start the server."""
    from a2a_hub.server import main as _main

    _main()


__all__ = ["__version__", "main"]

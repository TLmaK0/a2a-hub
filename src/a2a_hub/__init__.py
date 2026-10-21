"""a2a-hub: A2A store-and-forward server on top of the official ``a2a-sdk``.

A minimal layer (executor + task store + auth + deployment) so agents can talk over
HTTPS with authentication. It does not reimplement the A2A protocol.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

#: Read from the installed package's metadata rather than hand-copied from
#: ``pyproject.toml``: a copy is a second place to remember to update, and the one in
#: this file had already drifted to "0.1.0" while ``pyproject.toml`` said "0.2.0" — the
#: Agent Card was serving a version number nobody had touched since the file was written.
#:
#: Today's Dockerfile always runs ``uv sync`` before import, so distribution metadata is
#: always present in practice — but the contract here is "import never raises", not
#: "the Dockerfile happens to be fine", because the failure mode of getting this wrong
#: is the hub not starting at all, on whatever future packaging path hits it.
try:
    __version__ = _installed_version("a2a-hub")
except PackageNotFoundError:
    __version__ = "unknown"


def main() -> None:
    """Console entry point: start the server."""
    from a2a_hub.server import main as _main

    _main()


__all__ = ["__version__", "main"]

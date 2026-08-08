"""a2a-hub server configuration, loaded from the environment.

No secrets in the code or in the repo: tokens arrive via environment variables
(in production, from a k8s Secret). See `.env.example`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_DB_PATH = "a2a-hub.db"
DEFAULT_PUBLIC_URL = "http://localhost:8000/"


def _parse_tokens(raw: str) -> dict[str, str]:
    """Turn ``token1:agentA,token2:agentB`` into ``{token: agent}``.

    Empty entries and surrounding whitespace are ignored. A duplicate token
    overrides the previous one (last wins). Invalid format (no ``:``) raises
    ``ValueError`` to fail early and loud rather than silently.
    """
    tokens: dict[str, str] = {}
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"Invalid token entry (missing ':' in token:agent): {entry!r}"
            )
        token, _, agent = entry.partition(":")
        token, agent = token.strip(), agent.strip()
        if not token or not agent:
            raise ValueError(
                f"Invalid token entry (empty token or agent): {entry!r}"
            )
        tokens[token] = agent
    return tokens


@dataclass(frozen=True)
class Settings:
    """Server startup parameters.

    Attributes:
        tokens: ``token -> agent name`` map for bearer authentication.
        db_url: async SQLAlchemy URL of the task store (SQLite by default).
        public_url: public URL advertised in the Agent Card.
        rpc_url: path of the JSON-RPC endpoint (A2A uses ``/`` by default).
        host: server listen interface.
        port: server listen port.
        tls_certfile: optional PEM cert path to serve HTTPS directly (self-contained).
        tls_keyfile: optional PEM private key path paired with ``tls_certfile``.
    """

    tokens: dict[str, str] = field(default_factory=dict)
    db_url: str = f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}"
    public_url: str = DEFAULT_PUBLIC_URL
    rpc_url: str = "/"
    host: str = "0.0.0.0"  # noqa: S104 — bind all; front with TLS/reverse proxy.
    port: int = 8000
    tls_certfile: str | None = None
    tls_keyfile: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """Build Settings from environment variables.

        Recognized variables:
            A2A_HUB_TOKENS      token1:agentA,token2:agentB
            A2A_HUB_DB_URL      async SQLAlchemy URL (or A2A_HUB_DB_PATH)
            A2A_HUB_DB_PATH     path to a SQLite file (shortcut for DB_URL)
            A2A_HUB_PUBLIC_URL  public URL for the Agent Card
            A2A_HUB_RPC_URL     path of the JSON-RPC endpoint
            A2A_HUB_HOST        listen interface
            A2A_HUB_PORT        listen port
            A2A_HUB_TLS_CERTFILE  PEM cert path to serve HTTPS directly (optional)
            A2A_HUB_TLS_KEYFILE   PEM key path paired with the cert (optional)
        """
        env = environ if environ is not None else dict(os.environ)

        tokens = _parse_tokens(env.get("A2A_HUB_TOKENS", ""))

        db_url = env.get("A2A_HUB_DB_URL")
        if not db_url:
            db_path = env.get("A2A_HUB_DB_PATH", DEFAULT_DB_PATH)
            db_url = f"sqlite+aiosqlite:///{db_path}"

        return cls(
            tokens=tokens,
            db_url=db_url,
            public_url=env.get("A2A_HUB_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            rpc_url=env.get("A2A_HUB_RPC_URL", "/"),
            host=env.get("A2A_HUB_HOST", "0.0.0.0"),  # noqa: S104
            port=int(env.get("A2A_HUB_PORT", "8000")),
            tls_certfile=env.get("A2A_HUB_TLS_CERTFILE") or None,
            tls_keyfile=env.get("A2A_HUB_TLS_KEYFILE") or None,
        )

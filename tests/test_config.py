"""Configuration tests: token parsing and loading from the environment."""

from __future__ import annotations

import pytest

from a2a_hub.config import DEFAULT_DB_PATH, Settings, _parse_tokens


def test_parse_tokens_basic():
    assert _parse_tokens("t1:a,t2:b") == {"t1": "a", "t2": "b"}


def test_parse_tokens_whitespace_and_empties():
    assert _parse_tokens("  t1 : a , , t2:b  ") == {"t1": "a", "t2": "b"}


def test_parse_tokens_empty_string():
    assert _parse_tokens("") == {}


def test_parse_tokens_duplicate_last_wins():
    assert _parse_tokens("t:a,t:b") == {"t": "b"}


@pytest.mark.parametrize("bad", ["no_colon", ":agent", "token:"])
def test_parse_tokens_invalid_raises(bad):
    with pytest.raises(ValueError):
        _parse_tokens(bad)


def test_from_env_defaults():
    s = Settings.from_env({})
    assert s.tokens == {}
    assert s.db_url == f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}"
    assert s.rpc_url == "/"
    assert s.port == 8000
    assert s.tls_certfile is None
    assert s.tls_keyfile is None


def test_from_env_tls():
    s = Settings.from_env(
        {"A2A_HUB_TLS_CERTFILE": "/c/tls.crt", "A2A_HUB_TLS_KEYFILE": "/c/tls.key"}
    )
    assert s.tls_certfile == "/c/tls.crt"
    assert s.tls_keyfile == "/c/tls.key"


def test_from_env_full():
    s = Settings.from_env(
        {
            "A2A_HUB_TOKENS": "tok:agent",
            "A2A_HUB_DB_URL": "postgresql+asyncpg://x/y",
            "A2A_HUB_PUBLIC_URL": "https://a2a.example.com/",
            "A2A_HUB_RPC_URL": "/rpc",
            "A2A_HUB_HOST": "127.0.0.1",
            "A2A_HUB_PORT": "9000",
        }
    )
    assert s.tokens == {"tok": "agent"}
    assert s.db_url == "postgresql+asyncpg://x/y"
    assert s.public_url == "https://a2a.example.com/"
    assert s.rpc_url == "/rpc"
    assert s.host == "127.0.0.1"
    assert s.port == 9000


def test_from_env_db_path_shortcut():
    s = Settings.from_env({"A2A_HUB_DB_PATH": "/data/hub.db"})
    assert s.db_url == "sqlite+aiosqlite:////data/hub.db"


def test_from_env_reads_os_environ(monkeypatch):
    monkeypatch.setenv("A2A_HUB_TOKENS", "z:zeta")
    s = Settings.from_env()
    assert s.tokens == {"z": "zeta"}

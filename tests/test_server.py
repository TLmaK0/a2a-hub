"""Startup tests: run()/main() invoke uvicorn with the built app."""

from __future__ import annotations

import a2a_hub
import a2a_hub.server as server
from a2a_hub.config import Settings


def test_run_invokes_uvicorn(monkeypatch):
    captured = {}

    def fake_run(app, host, port, ssl_certfile=None, ssl_keyfile=None, log_config=None):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["ssl_certfile"] = ssl_certfile
        captured["ssl_keyfile"] = ssl_keyfile
        captured["log_config"] = log_config

    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    settings = Settings(
        tokens={"t": "a"},
        db_url="sqlite+aiosqlite:///:memory:",
        host="127.0.0.1",
        port=1234,
        tls_certfile="/certs/tls.crt",
        tls_keyfile="/certs/tls.key",
    )
    server.run(settings)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 1234
    assert captured["ssl_certfile"] == "/certs/tls.crt"
    assert captured["ssl_keyfile"] == "/certs/tls.key"
    assert captured["app"].state.settings is settings
    # Passed explicitly, so this package's warnings share uvicorn's format instead of
    # falling through to Python's last-resort handler.
    assert captured["log_config"]["loggers"]["a2a_hub"]["handlers"] == ["default"]


def test_run_without_settings_uses_env(monkeypatch):
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        server.Settings, "from_env", classmethod(lambda cls: Settings(tokens={"t": "a"}))
    )
    server.run()  # must not raise


def test_main_calls_run(monkeypatch):
    called = {}
    monkeypatch.setattr(server, "run", lambda: called.setdefault("ok", True))
    server.main()
    assert called["ok"]


def test_package_main(monkeypatch):
    monkeypatch.setattr("a2a_hub.server.main", lambda: None)
    a2a_hub.main()


def test_our_warnings_are_formatted_like_uvicorns(capsys):
    """A log line that exists but cannot be found is the same as no log line.

    Without wiring this package into uvicorn's logging config, a warning propagates
    to a handler-less root logger and comes out through Python's last-resort handler:
    no level, no timestamp, a bare line among the access logs. Grepping for WARNING
    would miss the very rejections #13 added.
    """
    import logging.config

    from a2a_hub.server import _log_config

    logging.config.dictConfig(_log_config())
    logging.getLogger("a2a_hub.executor").warning("delivery rejected: probe")

    assert "WARNING" in capsys.readouterr().err


def test_the_log_config_keeps_uvicorns_own_loggers():
    """Extend, never replace: losing the access log to gain ours is a bad trade."""
    from a2a_hub.server import _log_config

    loggers = _log_config()["loggers"]

    assert "a2a_hub" in loggers
    assert "uvicorn" in loggers and "uvicorn.access" in loggers

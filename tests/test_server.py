"""Startup tests: run()/main() invoke uvicorn with the built app."""

from __future__ import annotations

import a2a_hub
import a2a_hub.server as server
from a2a_hub.config import Settings


def test_run_invokes_uvicorn(monkeypatch):
    captured = {}

    def fake_run(app, host, port, ssl_certfile=None, ssl_keyfile=None):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["ssl_certfile"] = ssl_certfile
        captured["ssl_keyfile"] = ssl_keyfile

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

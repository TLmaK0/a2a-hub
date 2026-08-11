"""Client tests: real client against the real server, plus config/CLI units.

The functional tests wire ``HubClient`` to the in-process ASGI app, so they exercise
the actual A2A protocol end to end (client -> JSON-RPC -> hub -> mailbox).
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from a2a_hub.client import (
    ClientConfig,
    ClientError,
    HubClient,
    format_agent,
    format_task,
    main,
)


def _config() -> ClientConfig:
    return ClientConfig(url="https://hub.test/", agent="a", token="t", session="s1")
from conftest import AGENT_A, AGENT_B, IDENT_A, IDENT_B, TOKEN_A, TOKEN_B


@pytest.fixture
async def make_client(app):
    """Build a HubClient talking to the in-process app as the given agent.

    The client is synchronous while the ASGI app is async, so its transport hands
    the request back to the running test loop and blocks for the result. Tests then
    call the client through ``run()`` (a worker thread) to keep the loop free.
    """
    loop = asyncio.get_running_loop()
    asgi = httpx.ASGITransport(app=app)

    async def _post(url: str, body: bytes, headers: dict) -> dict:
        async with httpx.AsyncClient(
            transport=asgi, base_url="https://hub.test"
        ) as http:
            response = await http.post(url, content=body, headers=headers)
            return response.json()

    def _factory(agent: str, token: str, session: str = "s1") -> HubClient:
        def _sync_transport(url, body, headers):
            future = asyncio.run_coroutine_threadsafe(
                _post(url, body, headers), loop
            )
            return future.result(timeout=30)

        config = ClientConfig(url="/", agent=agent, token=token, session=session)
        return HubClient(config, transport=_sync_transport)

    yield _factory
    await app.state.engine.dispose()


async def run(func, *args, **kwargs):
    """Run blocking client/CLI code off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


# --- functional: real client over the real protocol ------------------------

async def test_client_send_and_receive(make_client):
    sender = make_client(AGENT_A, TOKEN_A)
    recipient = make_client(AGENT_B, TOKEN_B)

    result = await run(sender.send_message, IDENT_B, "hello over the wire")
    assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    inbox = await run(recipient.list_tasks)
    assert inbox["totalSize"] == 1
    artifact = inbox["tasks"][0]["artifacts"][0]
    assert artifact["parts"][0]["text"] == "hello over the wire"
    assert artifact["metadata"]["sender"] == IDENT_A


async def test_client_mailboxes_are_isolated(make_client):
    sender = make_client(AGENT_A, TOKEN_A)
    await run(sender.send_message, IDENT_B, "for B only")
    # The sender sees nothing in its own mailbox.
    assert (await run(sender.list_tasks))["totalSize"] == 0


async def test_client_get_task(make_client):
    sender = make_client(AGENT_A, TOKEN_A)
    recipient = make_client(AGENT_B, TOKEN_B)
    sent = await run(sender.send_message, IDENT_B, "fetch me")
    task_id = sent["task"]["id"]

    task = await run(recipient.get_task, task_id)
    assert task["id"] == task_id
    assert task["artifacts"][0]["parts"][0]["text"] == "fetch me"


async def test_client_reports_hub_error(make_client):
    recipient = make_client(AGENT_B, TOKEN_B)
    with pytest.raises(ClientError, match="hub error"):
        await run(recipient.get_task, "does-not-exist")


async def test_client_rejects_unknown_recipient(make_client):
    sender = make_client(AGENT_A, TOKEN_A)
    result = await run(sender.send_message, "ghost", "nobody home")
    assert result["task"]["status"]["state"] == "TASK_STATE_REJECTED"


# --- CLI over the real server ---------------------------------------------

async def test_cli_send_then_inbox(make_client, capsys):
    sender = make_client(AGENT_A, TOKEN_A)
    recipient = make_client(AGENT_B, TOKEN_B)

    assert await run(main, ["send", IDENT_B, "cli", "message"], client=sender) == 0
    assert "COMPLETED" in capsys.readouterr().out

    assert await run(main, ["inbox"], client=recipient) == 0
    out = capsys.readouterr().out
    assert f"mailbox of {IDENT_B}" in out
    assert "cli message" in out
    assert f"from {IDENT_A}" in out


async def test_cli_inbox_json_and_read(make_client, capsys):
    sender = make_client(AGENT_A, TOKEN_A)
    recipient = make_client(AGENT_B, TOKEN_B)
    sent = await run(sender.send_message, IDENT_B, "json please")
    task_id = sent["task"]["id"]

    assert await run(main, ["inbox", "--json"], client=recipient) == 0
    assert json.loads(capsys.readouterr().out)["totalSize"] == 1

    assert await run(main, ["read", task_id], client=recipient) == 0
    assert json.loads(capsys.readouterr().out)["id"] == task_id


def test_cli_whoami_hides_token(make_client, capsys):
    client = make_client(AGENT_A, TOKEN_A)
    assert main(["whoami"], client=client) == 0
    out = capsys.readouterr().out
    assert AGENT_A in out
    assert TOKEN_A not in out


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["read"], "usage: a2a-client read"),
        (["send", "only-recipient"], "usage: a2a-client send"),
        (["nonsense"], "unknown command"),
    ],
)
def test_cli_usage_errors(make_client, capsys, argv, expected):
    client = make_client(AGENT_A, TOKEN_A)
    assert main(argv, client=client) == 1
    assert expected in capsys.readouterr().err


def test_cli_help(capsys):
    assert main([]) == 0
    assert "a2a-client" in capsys.readouterr().out
    assert main(["--help"]) == 0
    assert "inbox" in capsys.readouterr().out


# --- config ---------------------------------------------------------------

def test_config_from_env(tmp_path):
    cfg = ClientConfig.load(
        environ={
            "A2A_HUB_URL": "https://h/",
            "A2A_HUB_AGENT": "me",
            "A2A_HUB_TOKEN": "t",
            "A2A_HUB_SESSION": "s1",
        },
        config_path=tmp_path / "missing.env",
    )
    assert (cfg.url, cfg.agent, cfg.token, cfg.session) == ("https://h/", "me", "t", "s1")
    assert cfg.identity == "me/s1"


def test_config_from_file(tmp_path):
    path = tmp_path / "agent.env"
    path.write_text(
        "# comment\n\nA2A_HUB_URL=https://f/\nA2A_HUB_AGENT=file-agent\n"
        "A2A_HUB_TOKEN=file-token\nA2A_HUB_SESSION=file-session\nBROKEN LINE\n"
    )
    cfg = ClientConfig.load(environ={}, config_path=path)
    assert cfg.agent == "file-agent"
    assert cfg.token == "file-token"


def test_config_env_overrides_file(tmp_path):
    path = tmp_path / "agent.env"
    path.write_text(
        "A2A_HUB_URL=https://f/\nA2A_HUB_AGENT=file\nA2A_HUB_TOKEN=ft\n"
        "A2A_HUB_SESSION=fs\n"
    )
    cfg = ClientConfig.load(environ={"A2A_HUB_AGENT": "env"}, config_path=path)
    assert cfg.agent == "env"
    assert cfg.token == "ft"


def test_config_missing_values_raise(tmp_path):
    with pytest.raises(ClientError, match="A2A_HUB_URL"):
        ClientConfig.load(environ={}, config_path=tmp_path / "nope.env")


def test_config_requires_session(tmp_path):
    """A session is mandatory, so two processes never share one mailbox."""
    with pytest.raises(ClientError, match="A2A_HUB_SESSION"):
        ClientConfig.load(
            environ={
                "A2A_HUB_URL": "https://h/",
                "A2A_HUB_AGENT": "me",
                "A2A_HUB_TOKEN": "t",
            },
            config_path=tmp_path / "nope.env",
        )


def test_client_sends_session_header():
    captured = {}

    def transport(url, body, headers):
        captured.update(headers)
        return {"result": {}}

    config = ClientConfig(url="/", agent="me", token="t", session="sess-1")
    HubClient(config, transport=transport).list_tasks()
    assert captured["A2A-Session"] == "sess-1"


def test_cli_session_flag_overrides(tmp_path, monkeypatch, capsys):
    path = tmp_path / "agent.env"
    path.write_text(
        "A2A_HUB_URL=https://h/\nA2A_HUB_AGENT=me\nA2A_HUB_TOKEN=t\n"
        "A2A_HUB_SESSION=from-file\n"
    )
    monkeypatch.setattr("a2a_hub.client.DEFAULT_CONFIG_PATH", path)
    for var in ("A2A_HUB_URL", "A2A_HUB_AGENT", "A2A_HUB_TOKEN", "A2A_HUB_SESSION"):
        monkeypatch.delenv(var, raising=False)

    assert main(["--session", "from-flag", "whoami"]) == 0
    assert "me/from-flag" in capsys.readouterr().out


def test_cli_session_flag_needs_value(capsys):
    assert main(["--session"]) == 1
    assert "usage: a2a-client --session" in capsys.readouterr().err


def test_cli_reports_config_error(tmp_path, monkeypatch, capsys):
    # No env, no file -> the CLI must fail cleanly (exit 1), not traceback.
    monkeypatch.setattr("a2a_hub.client.DEFAULT_CONFIG_PATH", tmp_path / "nope.env")
    for var in ("A2A_HUB_URL", "A2A_HUB_AGENT", "A2A_HUB_TOKEN", "A2A_HUB_SESSION"):
        monkeypatch.delenv(var, raising=False)
    assert main(["whoami"]) == 1
    assert "missing" in capsys.readouterr().err


# --- default transport ----------------------------------------------------

def test_urllib_transport_posts_and_decodes(monkeypatch):
    """The stdlib transport must send the request and decode the JSON reply."""
    import contextlib
    import io

    from a2a_hub import client as client_module

    captured = {}

    @contextlib.contextmanager
    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["headers"] = request.headers
        captured["timeout"] = timeout
        yield io.BytesIO(b'{"result": {"ok": true}}')

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    result = client_module._urllib_transport(
        "https://hub.test/", b'{"jsonrpc":"2.0"}', {"Authorization": "Bearer t"}
    )
    assert result == {"result": {"ok": True}}
    assert captured["url"] == "https://hub.test/"
    assert captured["body"] == b'{"jsonrpc":"2.0"}'
    assert captured["headers"]["Authorization"] == "Bearer t"


def test_client_uses_urllib_transport_by_default():
    from a2a_hub import client as client_module

    hub = HubClient(ClientConfig(url="https://h/", agent="a", token="t", session="s1"))
    assert hub._transport is client_module._urllib_transport


# --- formatting -----------------------------------------------------------

def test_format_task_without_artifacts():
    line = format_task({"id": "abcdef1234", "status": {"state": "TASK_STATE_WORKING"}})
    assert "[abcdef12]" in line
    assert "WORKING" in line


def test_format_task_with_artifact():
    text = format_task(
        {
            "id": "abcdef1234",
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "timestamp": "2026-08-08T10:51:11Z",
            },
            "artifacts": [
                {"metadata": {"sender": "a"}, "parts": [{"text": "hi"}]}
            ],
        }
    )
    assert "from a: hi" in text
    assert "2026-08-08 10:51:11" in text


# --- register commands -------------------------------------------------------


def test_introduce_sends_role_projects_and_status(monkeypatch, capsys):
    """The host is taken from the machine: one less field to get wrong by hand."""
    seen = {}

    def transport(url, body, headers):
        seen["url"] = url
        seen["body"] = json.loads(body)
        return {"identity": "ns/a", "role": "project", "host": "h",
                "projects": ["a2a-hub"], "status": "on issue 17"}

    monkeypatch.setattr(socket, "gethostname", lambda: "ns3073844")
    hub = HubClient(_config(), transport=transport)

    assert main(["introduce", "project", "a2a-hub,myinfra", "on", "issue", "17"], hub) == 0

    assert seen["url"].endswith("/agents/register")
    assert seen["body"]["role"] == "project"
    assert seen["body"]["projects"] == ["a2a-hub", "myinfra"]
    assert seen["body"]["status"] == "on issue 17"
    assert seen["body"]["host"] == "ns3073844"
    assert "introduced ns/a" in capsys.readouterr().out


def test_status_moves_only_the_status(capsys):
    def transport(url, body, headers):
        assert url.endswith("/agents/status")
        assert json.loads(body) == {"status": "merging issue 9"}
        return {"identity": "ns/a", "status": "merging issue 9"}

    hub = HubClient(_config(), transport=transport)

    assert main(["status", "merging", "issue", "9"], hub) == 0
    assert "merging issue 9" in capsys.readouterr().out


def test_agents_lists_who_is_connected(capsys):
    def transport(url, body, headers):
        assert url.endswith("/agents")
        assert body is None  # a GET, not a POST
        return {"agents": [
            {"identity": "ns/a", "role": "manager", "projects": ["x"],
             "status": "watching", "declared": True, "last_seen_seconds": 30},
            {"identity": "ns/b", "declared": False, "last_seen_seconds": 10800},
        ]}

    hub = HubClient(_config(), transport=transport)

    assert main(["agents"], hub) == 0
    out = capsys.readouterr().out
    assert "ns/a manager x :: watching" in out
    assert "(undeclared)" in out
    assert "3h ago" in out


def test_register_commands_report_usage_when_incomplete(capsys):
    hub = HubClient(_config(), transport=lambda *a: {})

    assert main(["introduce", "project"], hub) == 1
    assert main(["status"], hub) == 1
    assert "usage" in capsys.readouterr().err


def test_a_register_error_is_reported_not_swallowed(capsys):
    def transport(url, body, headers):
        return {"error": "not_registered", "detail": "register first"}

    hub = HubClient(_config(), transport=transport)

    assert main(["status", "doing things"], hub) == 1
    assert "register first" in capsys.readouterr().err


def test_format_agent_says_never_seen_when_it_never_was():
    """A row with no timestamp must not render as if it were fresh."""
    assert "never seen" in format_agent({"identity": "ns/x", "declared": False})


def test_agents_json_output_is_the_raw_payload(capsys):
    payload = {"agents": [{"identity": "ns/a", "declared": False,
                           "last_seen_seconds": 5}]}
    hub = HubClient(_config(), transport=lambda *a: payload)

    assert main(["agents", "--json"], hub) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_format_agent_shows_minutes_between_two_and_one_hundred_twenty(capsys):
    """The middle band exists so "45m ago" does not print as 2700s or 0h."""
    assert "45m ago" in format_agent(
        {"identity": "ns/x", "declared": False, "last_seen_seconds": 2700}
    )


def test_introduce_refuses_a_session_from_the_shared_config_file(capsys):
    """Where the manager's mistake became someone else's overwritten row."""
    config = ClientConfig(
        url="https://hub.test/", agent="a", token="t", session="claude-main",
        session_from_shared_file=True,
    )
    hub = HubClient(config, transport=lambda *a: {})

    assert main(["introduce", "project", "x", "doing things"], hub) == 1
    err = capsys.readouterr().err
    assert "shared config file" in err
    assert "A2A_HUB_SESSION" in err


def test_introduce_proceeds_when_the_session_was_set_for_this_process():
    sent = {}

    def transport(url, body, headers):
        sent["url"] = url
        return {"identity": "ns/a", "role": "project"}

    hub = HubClient(_config(), transport=transport)

    assert main(["introduce", "project", "x", "doing things"], hub) == 0
    assert sent["url"].endswith("/agents/register")


def test_introduce_prints_warnings_it_gets_back(capsys):
    def transport(url, body, headers):
        return {"identity": "ns/a", "role": "manager",
                "warnings": ["another manager is already registered: ns/b, seen 5s ago"]}

    hub = HubClient(_config(), transport=transport)

    assert main(["introduce", "manager", "x", "doing things"], hub) == 0
    assert "another manager is already registered" in capsys.readouterr().err


def test_retire_withdraws_the_registration(capsys):
    def transport(url, body, headers):
        assert url.endswith("/agents/retire")
        return {"identity": "ns/a", "retired": True}

    hub = HubClient(_config(), transport=transport)

    assert main(["retire"], hub) == 0
    assert "retired ns/a" in capsys.readouterr().out


def test_the_session_source_is_read_from_the_environment(monkeypatch, tmp_path):
    """A session exported for this process is this agent's; one from the file is not."""
    env = {"A2A_HUB_URL": "https://h/", "A2A_HUB_AGENT": "a",
           "A2A_HUB_TOKEN": "t", "A2A_HUB_SESSION": "mine"}
    config = ClientConfig.load(environ=env, config_path=tmp_path / "absent.env")
    assert config.session_from_shared_file is False


def test_a_shared_session_warns_on_every_command_not_just_introduce(capsys):
    """`inbox` on a shared session reads someone else's mailbox, silently."""
    config = ClientConfig(
        url="https://hub.test/", agent="a", token="t", session="claude-main",
        session_from_shared_file=True,
    )
    hub = HubClient(config, transport=lambda *a: {"result": {"totalSize": 0, "tasks": []}})

    assert main(["inbox"], hub) == 0
    assert "NOT this agent's identity" in capsys.readouterr().err


def test_whoami_does_not_warn_because_it_is_how_you_check(capsys):
    config = ClientConfig(
        url="https://hub.test/", agent="a", token="t", session="claude-main",
        session_from_shared_file=True,
    )
    hub = HubClient(config, transport=lambda *a: {})

    assert main(["whoami"], hub) == 0
    assert capsys.readouterr().err == ""

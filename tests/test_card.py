"""Agent Card tests (unit + public endpoint)."""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict

from a2a_hub import __version__
from a2a_hub.card import SECURITY_SCHEME_NAME, build_agent_card


def test_build_card_basic_fields():
    card = build_agent_card("https://a2a.example.com/", "/")
    assert card.name == "a2a-hub"
    # The card now carries "<version>+<tree>" when the build is known, so that a
    # client can tell whether it is running the same code as the hub. The package
    # version is still the prefix, which is the part that is a promise.
    assert card.version.startswith(__version__)
    assert card.capabilities.streaming is False


def test_build_card_endpoint_normalizes_url():
    card = build_agent_card("https://a2a.example.com", "/rpc")
    assert card.supported_interfaces[0].url == "https://a2a.example.com/rpc"


def test_build_card_declares_bearer():
    card = build_agent_card("https://x/")
    d = MessageToDict(card)
    assert SECURITY_SCHEME_NAME in d["securitySchemes"]
    assert (
        d["securitySchemes"][SECURITY_SCHEME_NAME]["httpAuthSecurityScheme"]["scheme"]
        == "bearer"
    )
    assert d["securityRequirements"][0]["schemes"][SECURITY_SCHEME_NAME] == {}


def test_build_card_mailbox_skill():
    card = build_agent_card("https://x/")
    assert card.skills[0].id == "mailbox"


async def test_card_endpoint(client):
    r = await client.get("/.well-known/agent-card.json")
    body = r.json()
    assert body["name"] == "a2a-hub"
    assert body["skills"][0]["id"] == "mailbox"
    assert body["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


# --- which build is this, so a stale client can be spotted -------------------

def test_the_card_names_the_build_it_is_running(monkeypatch):
    """A client cannot detect skew against a version that is 0.1.0 forever."""
    from a2a_hub.build import TREE_ENV
    from a2a_hub.card import build_agent_card

    monkeypatch.setenv(TREE_ENV, "a" * 40)

    card = build_agent_card("https://hub.example/")

    assert card.version.endswith("+" + "a" * 12)
    assert card.version.startswith("0.1.0+")


def test_the_card_falls_back_to_the_plain_version_when_the_build_is_unknown(
    monkeypatch, tmp_path
):
    """Running from a wheel with no git and no stamp must not print 'unknown'."""
    from a2a_hub import build as build_module
    from a2a_hub.build import TREE_ENV
    from a2a_hub.card import build_agent_card

    monkeypatch.delenv(TREE_ENV, raising=False)
    monkeypatch.setattr(build_module, "_git_tree", lambda _start: None)

    card = build_agent_card("https://hub.example/")

    assert card.version == "0.1.0"

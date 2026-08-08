"""Agent Card tests (unit + public endpoint)."""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict

from a2a_hub import __version__
from a2a_hub.card import SECURITY_SCHEME_NAME, build_agent_card


def test_build_card_basic_fields():
    card = build_agent_card("https://a2a.example.com/", "/")
    assert card.name == "a2a-hub"
    assert card.version == __version__
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

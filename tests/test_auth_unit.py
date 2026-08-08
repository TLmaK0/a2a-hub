"""Auth unit tests: token registry and bearer extraction."""

from __future__ import annotations

import pytest

from a2a_hub.auth import TokenRegistry, _extract_bearer


def test_registry_resolves_valid_token():
    reg = TokenRegistry({"tok-a": "agent-a"})
    assert reg.resolve("tok-a") == "agent-a"
    assert len(reg) == 1


def test_registry_unknown_token():
    reg = TokenRegistry({"tok-a": "agent-a"})
    assert reg.resolve("other") is None


def test_registry_empty():
    assert TokenRegistry().resolve("x") is None
    assert len(TokenRegistry()) == 0


def test_registry_rotation():
    reg = TokenRegistry({"old": "agent"})
    reg.replace({"new": "agent"})
    assert reg.resolve("old") is None
    assert reg.resolve("new") == "agent"


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("BEARER   spaced  ", "spaced"),
        (None, None),
        ("", None),
        ("Basic abc123", None),
        ("Bearer", None),
        ("Bearer   ", None),
    ],
)
def test_extract_bearer(header, expected):
    assert _extract_bearer(header) == expected

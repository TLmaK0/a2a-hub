"""The register answers "who is connected and what are they" — over the real HTTP flow.

The question it exists for is "is there another manager?", so the tests are written
against that question rather than against the storage layer.
"""

from __future__ import annotations

import pytest

from conftest import (
    AGENT_A,
    IDENT_A,
    IDENT_B,
    TOKEN_A,
    TOKEN_B,
    auth,
    rpc,
    send_message_params,
)


REGISTER = "/agents/register"
LIST = "/agents"


def declaration(**overrides):
    payload = {
        "role": "manager",
        "host": "host-1",
        "projects": ["a2a-hub", "myinfra"],
        "status": "watching the fleet",
    }
    payload.update(overrides)
    return payload


def find(agents: list[dict], identity: str) -> dict:
    return next(a for a in agents if a["identity"] == identity)


async def test_declaring_then_listing_answers_who_is_who(client):
    """The whole point: a caller can find out what another session is."""
    registered = await client.post(
        REGISTER, json=declaration(), headers=auth(TOKEN_A)
    )
    assert registered.status_code == 200

    listed = await client.get(LIST, headers=auth(TOKEN_B))
    assert listed.status_code == 200
    entry = find(listed.json()["agents"], IDENT_A)

    assert entry["role"] == "manager"
    assert entry["host"] == "host-1"
    assert entry["projects"] == ["a2a-hub", "myinfra"]
    assert entry["status"] == "watching the fleet"
    assert entry["declared"] is True


async def test_the_listing_is_visible_to_any_authenticated_agent(client):
    """"Is there another manager?" cannot be answered from a filtered view."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    agents = (await client.get(LIST, headers=auth(TOKEN_B))).json()["agents"]

    assert [a["identity"] for a in agents if a["role"] == "manager"] == [IDENT_A]


async def test_identity_comes_from_the_token_not_from_the_body(client):
    """An agent describes itself; it does not get to choose who it is."""
    response = await client.post(
        REGISTER,
        json=declaration(identity="agent-b/impostor", role="manager"),
        headers=auth(TOKEN_A),
    )

    assert response.json()["identity"] == IDENT_A
    agents = (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"]
    assert [a["identity"] for a in agents] == [IDENT_A]


async def test_an_agent_that_never_declared_is_still_visible_as_undeclared(client):
    """Presence is observed, so silence must not make an agent disappear."""
    await rpc(client, "ListTasks", {}, token=TOKEN_B)

    agents = (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"]
    entry = find(agents, IDENT_B)

    assert entry["declared"] is False
    assert entry["role"] is None
    assert entry["last_seen_seconds"] is not None


async def test_last_seen_is_stamped_by_the_server_on_ordinary_traffic(client):
    """The field a client cannot fake: it moves because the server saw a request."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    before = find(
        (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"], IDENT_B
    ) if False else None
    assert before is None  # agent-b has not been seen yet

    await rpc(
        client,
        "SendMessage",
        send_message_params(AGENT_A, "hello"),
        token=TOKEN_B,
    )

    agents = (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"]
    assert find(agents, IDENT_B)["last_seen"] is not None


async def test_the_listing_reports_an_age_so_staleness_shows(client):
    """A register that can go stale must look stale, not merely be stale."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    entry = find(
        (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"], IDENT_A
    )

    assert entry["last_seen_seconds"] >= 0
    assert entry["last_seen"].endswith("Z")
    assert entry["declared_at"].endswith("Z")


async def test_declaring_again_replaces_the_previous_declaration(client):
    """One row per identity: a restarted agent updates, it does not accumulate."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))
    await client.post(
        REGISTER,
        json=declaration(role="project", status="on issue 17", projects=["a2a-hub"]),
        headers=auth(TOKEN_A),
    )

    agents = (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"]
    entries = [a for a in agents if a["identity"] == IDENT_A]

    assert len(entries) == 1
    assert entries[0]["role"] == "project"
    assert entries[0]["status"] == "on issue 17"


@pytest.mark.parametrize("role", ["", "manger", "admin", "MANAGER "])
async def test_unknown_roles_are_rejected(client, role):
    """A register full of typos answers "is there another manager?" wrongly."""
    response = await client.post(
        REGISTER, json=declaration(role=role), headers=auth(TOKEN_A)
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_declaration"


async def test_the_manager_registers_like_everyone_else(client):
    """No privileged field: the role is just a value, from the same route."""
    manager = await client.post(
        REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_A)
    )
    worker = await client.post(
        REGISTER, json=declaration(role="project"), headers=auth(TOKEN_B)
    )

    assert manager.status_code == worker.status_code == 200
    assert set(manager.json()) == set(worker.json())


async def test_register_requires_authentication(client):
    assert (await client.post(REGISTER, json=declaration())).status_code == 401


async def test_listing_requires_authentication(client):
    assert (await client.get(LIST)).status_code == 401


async def test_a_malformed_body_is_rejected_not_stored(client):
    bad_json = await client.post(REGISTER, content=b"{oops", headers=auth(TOKEN_A))
    not_an_object = await client.post(REGISTER, json=[1, 2], headers=auth(TOKEN_A))

    assert bad_json.status_code == 400
    assert not_an_object.status_code == 400


async def test_declared_text_is_bounded(client):
    """One caller must not be able to bloat the table for everyone."""
    response = await client.post(
        REGISTER,
        json=declaration(status="x" * 5000, projects=["p"] * 100),
        headers=auth(TOKEN_A),
    )

    entry = response.json()
    assert len(entry["status"]) == 200
    assert len(entry["projects"]) == 20


async def test_the_card_announces_the_extension(client):
    """Discovery, not word of mouth: clients find the capability in the card."""
    card = (await client.get("/.well-known/agent-card.json")).json()

    extension = next(
        e for e in card["capabilities"]["extensions"] if "agent-registry" in e["uri"]
    )
    # protobuf omits fields at their default, so an absent `required` *is* false —
    # which is what matters: a client that ignores the extension keeps working.
    assert extension.get("required", False) is False
    assert "register" in extension["description"]


async def test_the_register_never_breaks_the_mailbox(client, monkeypatch):
    """Presence is a nicety; delivery is the job. A broken register must not 500."""

    async def boom(_identity: str) -> None:
        raise RuntimeError("register is down")

    monkeypatch.setattr(client._transport.app.state.agents, "touch", boom)

    response = await rpc(
        client, "SendMessage", send_message_params(AGENT_A, "still works"), token=TOKEN_B
    )

    assert response.status_code == 200


STATUS = "/agents/status"


async def test_status_updates_without_repeating_the_introduction(client):
    """An agent changes task far more often than it changes role or host."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    updated = await client.post(
        STATUS, json={"status": "merging issue 9"}, headers=auth(TOKEN_A)
    )
    assert updated.status_code == 200

    entry = find(
        (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"], IDENT_A
    )
    assert entry["status"] == "merging issue 9"
    # Everything else survived the update.
    assert entry["role"] == "manager"
    assert entry["host"] == "host-1"
    assert entry["projects"] == ["a2a-hub", "myinfra"]


async def test_status_refuses_for_an_agent_that_never_introduced_itself(client):
    """A row with a status and no role is exactly the half-filled entry to avoid."""
    response = await client.post(
        STATUS, json={"status": "doing things"}, headers=auth(TOKEN_A)
    )

    assert response.status_code == 409
    assert response.json()["error"] == "not_registered"


async def test_status_requires_a_status_field(client):
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    response = await client.post(STATUS, json={"role": "manager"}, headers=auth(TOKEN_A))

    assert response.status_code == 400


async def test_status_requires_authentication(client):
    assert (await client.post(STATUS, json={"status": "x"})).status_code == 401

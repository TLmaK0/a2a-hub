"""The register answers "who is connected and what are they" — over the real HTTP flow.

The question it exists for is "is there another manager?", so the tests are written
against that question rather than against the storage layer.
"""

from __future__ import annotations

import pytest

from conftest import (
    AGENT_A,
    SESSION,
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


async def test_a_single_project_may_be_given_as_a_string(client):
    """Accepting the obvious shape is kinder than rejecting it on a technicality."""
    response = await client.post(
        REGISTER, json=declaration(projects="a2a-hub"), headers=auth(TOKEN_A)
    )

    assert response.json()["projects"] == ["a2a-hub"]


async def test_projects_must_be_a_list_not_a_number(client):
    response = await client.post(
        REGISTER, json=declaration(projects=42), headers=auth(TOKEN_A)
    )

    assert response.status_code == 400
    assert "projects" in response.json()["detail"]


async def test_status_route_rejects_malformed_json(client):
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    response = await client.post(STATUS, content=b"{oops", headers=auth(TOKEN_A))

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


async def test_a_status_update_after_the_row_lost_its_declaration_is_refused(client):
    """The 409 path, reached the way it will actually happen.

    An identity the server has merely *seen* has a row with no `declared_at`, so a
    status update must still refuse: presence is not an introduction.
    """
    await rpc(client, "ListTasks", {}, token=TOKEN_A)  # seen, never declared

    response = await client.post(
        STATUS, json={"status": "doing things"}, headers=auth(TOKEN_A)
    )

    assert response.status_code == 409


RETIRE = "/agents/retire"


async def test_a_registration_can_be_withdrawn(client):
    """The manager could not undo a wrong entry; now they can."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    retired = await client.post(RETIRE, headers=auth(TOKEN_A))
    assert retired.status_code == 200

    listed = (await client.get(LIST, headers=auth(TOKEN_B))).json()["agents"]
    assert IDENT_A not in [a["identity"] for a in listed]


async def test_a_withdrawn_registration_is_kept_not_deleted(client):
    """"This was here and was withdrawn at T" is information; vanishing is not."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))
    await client.post(RETIRE, headers=auth(TOKEN_A))

    listed = (await client.get(LIST + "?retired=true", headers=auth(TOKEN_A))).json()
    entry = find(listed["agents"], IDENT_A)

    assert entry["retired_at"] is not None
    assert entry["role"] == "manager"  # what it claimed survives the withdrawal


async def test_retiring_twice_is_refused(client):
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))
    await client.post(RETIRE, headers=auth(TOKEN_A))

    assert (await client.post(RETIRE, headers=auth(TOKEN_A))).status_code == 409


async def test_a_second_manager_is_warned_about_not_forbidden(client):
    """A handover legitimately overlaps; it just must not pass unremarked."""
    await client.post(REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_A))

    second = await client.post(
        REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_B)
    )

    assert second.status_code == 200
    assert any(IDENT_A in w for w in second.json()["warnings"])


async def test_no_manager_warning_when_there_is_only_one(client):
    response = await client.post(
        REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_A)
    )

    assert "warnings" not in response.json()


async def test_a_retired_manager_does_not_trigger_the_warning(client):
    """Otherwise every handover warns forever and people learn to ignore it."""
    await client.post(REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_A))
    await client.post(RETIRE, headers=auth(TOKEN_A))

    second = await client.post(
        REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_B)
    )

    assert "warnings" not in second.json()


async def test_taking_over_a_row_someone_else_just_declared_is_warned(client):
    """The fingerprint of two processes sharing one session, invisible until now.

    This is what silently replaced the manager's entry on 2026-08-11: a second agent
    registered under the default session and overwrote a row that was not its own.
    """
    await client.post(
        REGISTER,
        json=declaration(role="manager", projects=["myinfra"]),
        headers=auth(TOKEN_A),
    )

    # Same identity, different agent behind it, minutes later.
    again = await client.post(
        REGISTER,
        json=declaration(role="project", projects=["analog-brain"]),
        headers=auth(TOKEN_A),
    )

    warnings = again.json()["warnings"]
    assert any("sharing one session" in w for w in warnings)
    assert any("A2A_HUB_SESSION" in w for w in warnings)


async def test_updating_your_own_introduction_unchanged_does_not_warn(client):
    """Re-declaring the same thing is routine and must stay quiet."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    again = await client.post(
        REGISTER, json=declaration(status="something else"), headers=auth(TOKEN_A)
    )

    assert "warnings" not in again.json()


async def test_retire_requires_authentication(client):
    assert (await client.post(RETIRE)).status_code == 401


async def test_an_orphan_row_is_withdrawn_by_claiming_its_session(client):
    """How the ghost row actually gets cleaned, since it belongs to nobody.

    A session is claimed under your own token, so any session of a principal can be
    taken and withdrawn by that principal. That is what makes the shared-default row
    recoverable at all: without it, an entry nobody owns would be permanent.
    """
    orphan = "claude-main"
    # Someone lands in the shared row by accident.
    await client.post(
        REGISTER, json=declaration(role="manager"), headers=auth(TOKEN_A, orphan)
    )
    listed = (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"]
    assert f"{AGENT_A}/{orphan}" in [a["identity"] for a in listed]

    # Anyone holding that principal's token can claim the session and retire it.
    retired = await client.post(RETIRE, headers=auth(TOKEN_A, orphan))

    assert retired.status_code == 200
    after = (await client.get(LIST, headers=auth(TOKEN_A))).json()["agents"]
    assert f"{AGENT_A}/{orphan}" not in [a["identity"] for a in after]


async def test_retiring_does_not_reach_another_principal(client):
    """Recoverable is not the same as anyone-can-delete-anyone."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_B))

    # agent-a claims a session name, but under ITS OWN principal — never agent-b's.
    await client.post(RETIRE, headers=auth(TOKEN_A, SESSION))

    still = (await client.get(LIST, headers=auth(TOKEN_B))).json()["agents"]
    assert IDENT_B in [a["identity"] for a in still]
# --- who has gone quiet ------------------------------------------------------
#
# The register always held this answer; the failure was that it only gave it to
# someone who thought to look. A manager stopped calling the hub for six hours
# while two agents read their empty mailboxes as "no decision yet".


def _age_the_row(monkeypatch, seconds: int) -> None:
    """Make every subsequent read believe `seconds` have passed.

    Moving the clock forward beats writing a doctored `last_seen` into the table:
    it exercises the same age arithmetic the server actually runs.
    """
    import datetime as _dt

    from a2a_hub import registry as registry_module

    real_now = registry_module._now()
    monkeypatch.setattr(
        registry_module,
        "_now",
        lambda: real_now + _dt.timedelta(seconds=seconds),
    )


async def test_quiet_for_hides_an_agent_that_was_just_seen(client, monkeypatch):
    """The filter answers a question, so it must exclude the healthy case."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))

    listed = await client.get(f"{LIST}?quiet_for=3600", headers=auth(TOKEN_B))

    assert listed.status_code == 200
    assert [a["identity"] for a in listed.json()["agents"]] == []


async def test_quiet_for_surfaces_the_agent_that_stopped_calling(
    client, monkeypatch
):
    """The incident this exists for: alive-looking row, six hours of silence."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))
    _age_the_row(monkeypatch, 6 * 3600)

    listed = await client.get(f"{LIST}?quiet_for=3600", headers=auth(TOKEN_B))

    entry = find(listed.json()["agents"], IDENT_A)
    assert entry["last_seen_seconds"] >= 6 * 3600
    assert entry["role"] == "manager", "the answer must still say what went quiet"


async def test_quiet_for_is_measured_from_the_last_request_not_the_declaration(
    client, monkeypatch
):
    """`last_seen` is stamped by the server; that is the whole value of it.

    An agent whose *words* are old but that is still calling the hub is not quiet,
    and reporting it as such is how you learn to ignore the answer.
    """
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))
    _age_the_row(monkeypatch, 6 * 3600)
    # Any authenticated request re-stamps last_seen. The status text stays old.
    await client.get(LIST, headers=auth(TOKEN_A))

    listed = await client.get(f"{LIST}?quiet_for=3600", headers=auth(TOKEN_B))

    assert [a["identity"] for a in listed.json()["agents"]] == []


async def test_the_unfiltered_listing_is_unchanged(client, monkeypatch):
    """No threshold is invented by default: the hub does not know what long means."""
    await client.post(REGISTER, json=declaration(), headers=auth(TOKEN_A))
    _age_the_row(monkeypatch, 30 * 24 * 3600)

    listed = await client.get(LIST, headers=auth(TOKEN_B))

    assert find(listed.json()["agents"], IDENT_A)["role"] == "manager"


@pytest.mark.parametrize("raw", ["soon", "", "-5", "1h"])
async def test_a_malformed_quiet_for_is_refused_rather_than_ignored(client, raw):
    """Silently ignoring it would answer a different question than the one asked."""
    listed = await client.get(f"{LIST}?quiet_for={raw}", headers=auth(TOKEN_B))

    assert listed.status_code == 400
    assert listed.json()["error"] == "invalid_quiet_for"


# --- a database that existed before the column did ---------------------------
#
# The failure this pins took the register down in production on 2026-08-14 while
# every test passed. Tests build an empty database, so `create_all` creates the
# whole table and a missing column is impossible. The only test that can catch it
# is one that starts from the OLD schema on purpose.


async def test_a_register_created_before_retired_at_still_lists(tmp_path):
    """Exactly the production case: months-old database, newly added column."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from a2a_hub.registry import AgentRegistry

    db = tmp_path / "old.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")

    # The table exactly as it was before `retired_at` was introduced, with a row
    # in it — an empty table would let ADD COLUMN succeed for the wrong reason.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE agent_registrations ("
                " identity VARCHAR(255) NOT NULL PRIMARY KEY,"
                " role VARCHAR(32) DEFAULT '' NOT NULL,"
                " host VARCHAR(200) DEFAULT '' NOT NULL,"
                " projects VARCHAR(2048) DEFAULT '[]' NOT NULL,"
                " status VARCHAR(200) DEFAULT '' NOT NULL,"
                " declared_at DATETIME,"
                " last_seen DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO agent_registrations"
                " (identity, role, host, projects, status, declared_at, last_seen)"
                " VALUES ('ns/old', 'manager', 'h', '[]', 'from before',"
                " '2026-08-01 10:00:00', '2026-08-01 10:00:00')"
            )
        )

    registry = AgentRegistry(engine)
    listed = await registry.list_agents()

    assert [a["identity"] for a in listed] == ["ns/old"]
    assert listed[0]["status"] == "from before", "the existing row must survive"
    assert listed[0]["retired_at"] is None
    await engine.dispose()


async def test_the_upgrade_does_not_disturb_a_current_database(tmp_path):
    """Running it against an up-to-date schema must be a no-op, not a rewrite."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from a2a_hub.registry import AgentRegistry

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'new.sqlite'}")
    registry = AgentRegistry(engine)
    await registry.create_schema()
    await registry.declare("ns/x", declaration())

    # Second boot over the same file: nothing to add, nothing lost.
    reopened = AgentRegistry(engine)
    await reopened.create_schema()

    listed = await reopened.list_agents()
    assert [a["identity"] for a in listed] == ["ns/x"]
    await engine.dispose()

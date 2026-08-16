"""Who is connected, what they are, and when they were last seen.

Identity and role are **declared**, not deduced. Before this existed the only ways to
tell sessions apart were both wrong: sending a probe message to an invented session
returns ``COMPLETED`` exactly like a real one, and inferring a role from Remote
Control is meaningless, because that is the owner's phone access and can be attached
to any conversation.

Two fields with very different trust
------------------------------------
- **Declared** (``role``, ``host``, ``projects``, ``status``): whatever the caller
  says about itself. Useful, and no more trustworthy than the caller.
- **``last_seen``**: written by the server on every authenticated request. A client
  cannot set it, so a dead agent cannot claim to be alive. It is the only field that
  answers "is this still true?".

The identity always comes from the token and the session header, never from the
request body: an agent can describe itself, but it cannot *be* someone else.
"""

from __future__ import annotations

import asyncio
import datetime
import json

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine


#: Roles an agent may declare. Unknown values are rejected rather than stored: a
#: register full of typos answers "is there another manager?" wrongly.
ROLES: frozenset[str] = frozenset({"manager", "project", "watch"})

#: Caps on declared free text, so one caller cannot bloat the table.
MAX_FIELD = 200
MAX_PROJECTS = 20

#: How stale an in-memory last-seen may get before the next request writes it.
#: Presence answers "is this agent still around", a question whose useful precision
#: is minutes, not milliseconds — and every authenticated request would otherwise pay
#: for a database write it does not need. The mailbox is the job; presence is not.
TOUCH_INTERVAL_SECONDS = 60

_metadata = MetaData()

#: One row per identity (``principal/session``).
registrations = Table(
    "agent_registrations",
    _metadata,
    Column("identity", String(255), primary_key=True),
    Column("role", String(32), nullable=False, server_default=""),
    Column("host", String(MAX_FIELD), nullable=False, server_default=""),
    Column("projects", String(2048), nullable=False, server_default="[]"),
    Column("status", String(MAX_FIELD), nullable=False, server_default=""),
    Column("declared_at", DateTime(timezone=True), nullable=True),
    Column("last_seen", DateTime(timezone=True), nullable=True),
    # Retired rather than deleted: "this was here and was withdrawn at T" is
    # information, silently vanishing is not.
    Column("retired_at", DateTime(timezone=True), nullable=True),
)

#: A declaration that replaces a different agent's, made within this window, is the
#: fingerprint of two processes sharing one session. Wide enough to catch the real
#: case (two agents registering minutes apart), short enough that an agent updating
#: its own introduction hours later is not flagged.
REUSE_WINDOW_SECONDS = 900

#: How recently another manager must have been seen to be worth warning about. A
#: manager that has not called in an hour is probably gone, and warning about it
#: would train people to ignore the warning.
MANAGER_WINDOW_SECONDS = 3600


def _add_missing_columns(conn) -> None:
    """Add columns a table gained after it was first created.

    ``create_all`` creates missing *tables*; it does nothing to a table that
    already exists, columns included. That gap took the register down in
    production on 2026-08-14: `retired_at` was added with the retire feature, the
    live database had been created months earlier without it, and every listing
    failed with ``no such column: agent_registrations.retired_at`` while the whole
    test suite passed — because tests always start from an empty database, where
    the column is created and the gap cannot exist.

    Deliberately narrow: it only ever *adds* nullable columns. Dropping, renaming
    or retyping is not something to infer from a schema diff at startup, and a
    column that is NOT NULL without a server default cannot be added to a table
    that already has rows — so that case raises here, loudly and at boot, instead
    of failing later on the first query that mentions it.
    """
    inspector = inspect(conn)
    for table in _metadata.tables.values():
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable and column.server_default is None:
                raise RuntimeError(
                    f"cannot add required column {table.name}.{column.name} to an "
                    "existing table: give it a server_default or migrate by hand"
                )
            ddl = column.type.compile(conn.dialect)
            default = ""
            if column.server_default is not None:
                default = f" DEFAULT {column.server_default.arg.text}"  # type: ignore[union-attr]
            conn.exec_driver_sql(
                f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}{default}"
            )


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _trim(value: object, limit: int = MAX_FIELD) -> str:
    return str(value or "").strip()[:limit]


class RegistrationError(ValueError):
    """A declaration the register refuses to store."""


def clean_declaration(payload: dict) -> dict:
    """Validate and normalise what a caller says about itself.

    Rejects unknown roles instead of storing them: this register exists to answer
    "is there another manager?", and it cannot do that if `manger` is a role.
    """
    role = _trim(payload.get("role"), 32)
    if role not in ROLES:
        raise RegistrationError(
            f"role must be one of {sorted(ROLES)}; got {role!r}"
        )

    raw_projects = payload.get("projects") or []
    if isinstance(raw_projects, str):
        raw_projects = [raw_projects]
    if not isinstance(raw_projects, list):
        raise RegistrationError("projects must be a list of strings")
    projects = [_trim(p) for p in raw_projects[:MAX_PROJECTS] if _trim(p)]

    return {
        "role": role,
        "host": _trim(payload.get("host")),
        "projects": projects,
        "status": _trim(payload.get("status")),
    }


class NotRegisteredError(LookupError):
    """Asked to update the status of an identity that never introduced itself."""


class AgentRegistry:
    """Storage for declarations and for the server-stamped last-seen."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._ready = False
        self._lock = asyncio.Lock()
        self._last_write: dict[str, datetime.datetime] = {}

    async def create_schema(self) -> None:
        """Create the table if absent. Additive: nothing existing is touched.

        Called from the app lifespan *and* lazily before the first write, because
        the register must not depend on a lifespan having run — an ASGI transport
        that skips lifespan (as the tests' does) would otherwise leave the table
        missing until the first request failed.
        """
        async with self._lock:
            if self._ready:
                return
            async with self._engine.begin() as conn:
                await conn.run_sync(_metadata.create_all)
                await conn.run_sync(_add_missing_columns)
            self._ready = True

    async def touch(self, identity: str) -> None:
        """Record that ``identity`` was seen now.

        Called for every authenticated request, whether or not the agent ever
        declared anything — an agent that never registers still has to be visible,
        as "seen, undeclared" rather than absent.
        """
        now = _now()
        written = self._last_write.get(identity)
        if written is not None and (now - written).total_seconds() < TOUCH_INTERVAL_SECONDS:
            return

        await self.create_schema()
        statement = sqlite_insert(registrations).values(identity=identity, last_seen=now)
        statement = statement.on_conflict_do_update(
            index_elements=[registrations.c.identity],
            set_={"last_seen": now},
        )
        async with self._engine.begin() as conn:
            await conn.execute(statement)
        self._last_write[identity] = now

    async def declare(self, identity: str, declaration: dict) -> list[str]:
        """Store what ``identity`` says it is, and report anything suspicious.

        Returns warnings rather than refusing: a handover legitimately overlaps two
        managers, and a session may legitimately be reused. What must never happen is
        that either passes unremarked, because the register is then quietly wrong
        about the one question it exists to answer.
        """
        await self.create_schema()
        now = _now()
        warnings = await self._warnings_for(identity, declaration, now)
        values = {
            "identity": identity,
            "role": declaration["role"],
            "host": declaration["host"],
            "projects": json.dumps(declaration["projects"]),
            "status": declaration["status"],
            "declared_at": now,
            "last_seen": now,
            # Introducing yourself un-retires you. Without this the row keeps the
            # `retired_at` it was given, so a re-introduction returns 200 with the
            # caller's own identity echoed back and leaves them **invisible** in the
            # listing — the caller cannot tell a successful registration from one that
            # silently did not take.
            #
            # It is not a corner case: session names are reused across handovers, so a
            # replacement that introduces itself under the name its predecessor was
            # retired under is born invisible and believes it announced itself. Measured
            # in production on 2026-08-16, on a row whose declaration was two minutes
            # NEWER than its retirement and still absent from `/agents`.
            "retired_at": None,
        }
        statement = sqlite_insert(registrations).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[registrations.c.identity],
            set_={k: v for k, v in values.items() if k != "identity"},
        )
        async with self._engine.begin() as conn:
            await conn.execute(statement)
        return warnings

    async def _warnings_for(
        self, identity: str, declaration: dict, now: datetime.datetime
    ) -> list[str]:
        """Two things that are legal, surprising, and invisible today."""
        warnings: list[str] = []
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(registrations).where(registrations.c.retired_at.is_(None))
                )
            ).mappings().all()

        for row in rows:
            if row["identity"] == identity:
                # Someone else's declaration sitting in the row we are about to take
                # over: two processes sharing one session, which is exactly how the
                # manager's entry was silently replaced on 2026-08-11.
                age = _age_seconds(row["declared_at"], now)
                changed = (
                    row["role"] != declaration["role"]
                    or json.loads(row["projects"] or "[]") != declaration["projects"]
                )
                if changed and age is not None and age < REUSE_WINDOW_SECONDS:
                    warnings.append(
                        f"this identity was declared {age}s ago as "
                        f"{row['role'] or '?'} on {json.loads(row['projects'] or '[]')}; "
                        "two processes may be sharing one session — check A2A_HUB_SESSION"
                    )
                continue

            if declaration["role"] == "manager" and row["role"] == "manager":
                age = _age_seconds(row["last_seen"], now)
                if age is not None and age < MANAGER_WINDOW_SECONDS:
                    warnings.append(
                        f"another manager is already registered: {row['identity']}, "
                        f"seen {age}s ago"
                    )
        return warnings

    async def retire(self, identity: str) -> bool:
        """Withdraw a registration without losing that it existed."""
        await self.create_schema()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                registrations.update()
                .where(registrations.c.identity == identity)
                .where(registrations.c.retired_at.is_(None))
                .values(retired_at=_now())
            )
            return result.rowcount > 0

    async def update_status(self, identity: str, status: str) -> str:
        """Change only "what I am doing", leaving the rest of the introduction.

        The manager asked for this explicitly: an agent changes task far more often
        than it changes role or host, and re-sending the whole introduction to move
        one line invites the other fields to drift or be dropped by accident.

        Refuses if the identity never introduced itself: silently inventing a row
        with a status and no role would produce exactly the half-filled entries the
        register exists to avoid.
        """
        await self.create_schema()
        text = _trim(status)
        now = _now()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                registrations.update()
                .where(registrations.c.identity == identity)
                .where(registrations.c.declared_at.isnot(None))
                .values(status=text, declared_at=now, last_seen=now)
            )
            if result.rowcount == 0:
                raise NotRegisteredError(
                    f"{identity} has not introduced itself yet; register first"
                )
        return text

    async def list_agents(
        self, include_retired: bool = False, quiet_for: int | None = None
    ) -> list[dict]:
        """Every known identity, most recently seen first, with ages.

        Ages are computed at read time and returned alongside the timestamps: a
        register that can go stale must *look* stale. A caller reading
        ``last_seen_seconds: 10800`` cannot mistake it for a live agent, whereas a
        bare list of names looks alive whatever its age.

        ``quiet_for`` keeps only identities not seen for that many seconds. It filters
        on ``last_seen``, which the *server* stamps on every authenticated request —
        not on anything the agent said about itself, which is the only reason the
        answer is worth having. An identity that has somehow never been seen counts as
        quiet: never is longer than any threshold.

        The hub deliberately picks no threshold of its own. What counts as "too long"
        depends on the agent's poll interval, which the hub does not know, and a
        threshold invented here would produce an alert nobody reads.
        """
        await self.create_schema()
        query = select(registrations).order_by(registrations.c.last_seen.desc())
        if not include_retired:
            query = query.where(registrations.c.retired_at.is_(None))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()

        now = _now()
        agents = []
        for row in rows:
            last_seen = row["last_seen"]
            declared_at = row["declared_at"]
            age = _age_seconds(last_seen, now)
            if quiet_for is not None and age is not None and age < quiet_for:
                continue
            agents.append(
                {
                    "identity": row["identity"],
                    "role": row["role"] or None,
                    "host": row["host"] or None,
                    "projects": json.loads(row["projects"] or "[]"),
                    "status": row["status"] or None,
                    # An identity the server has seen but that never declared itself.
                    "declared": declared_at is not None,
                    "declared_at": _isoformat(declared_at),
                    "last_seen": _isoformat(last_seen),
                    "last_seen_seconds": age,
                    "retired_at": _isoformat(row["retired_at"]),
                }
            )
        return agents


def _as_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """SQLite gives naive datetimes back; they were stored as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _isoformat(value: datetime.datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def _age_seconds(value: datetime.datetime | None, now: datetime.datetime) -> int | None:
    value = _as_utc(value)
    return int((now - value).total_seconds()) if value else None

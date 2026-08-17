"""Schema helpers shared by the hub's own tables.

The hub owns two tables beyond the SDK's task store — the agent register and the
message marks — and both face the same problem on a database that predates them.
This module holds that logic once: a second copy of it would be a copy of a subtle
function whose docstring records a production outage, and copies drift.
"""

from __future__ import annotations

from sqlalchemy import MetaData, inspect


def add_missing_columns(conn, metadata: MetaData) -> None:
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
    for table in metadata.tables.values():
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

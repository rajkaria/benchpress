"""Alembic environment: online migrations only, against a `Config` built in code by `engine.py`.

There is no `alembic.ini` — `engine.py` constructs the `Config` in memory (`script_location`,
`sqlalchemy.url`) and, when it already holds an open connection (`Store.open` reusing its own engine so
an in-memory SQLite database survives past the migration), stashes it on `config.attributes["connection"]`
for this env to pick up instead of opening a second, unrelated connection.
"""

from __future__ import annotations

from typing import cast

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from benchpress.gateway.store.models import Base

config = context.config
target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    # render_as_batch: SQLite can't ALTER a table in place, so Alembic must recreate it under a
    # transaction; batch mode is a no-op overhead on backends (Postgres) that don't need it.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        _run_migrations(cast(Connection, shared_connection))
        return
    url = config.get_main_option("sqlalchemy.url")
    assert url is not None, "engine.py always sets sqlalchemy.url"
    connectable = create_engine(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _run_migrations(connection)
    finally:
        connectable.dispose()


run_migrations_online()

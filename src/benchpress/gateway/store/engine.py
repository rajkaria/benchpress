"""Engine construction and the Alembic migration runner for the gateway store.

`make_engine` centralizes every backend quirk (SQLite pragmas, in-memory pooling, the `psycopg` driver
selection) so the rest of the store only ever deals with a plain `sqlalchemy.Engine`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, create_engine, make_url
from sqlalchemy.pool import StaticPool

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def normalize_url(url: str) -> str:
    """`postgresql://` and `postgres://` become `postgresql+psycopg://`; everything else is unchanged."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    """Build the engine for `url`, applying SQLite's threading, journaling and pooling quirks.

    SQLite gets `check_same_thread=False` (the store's synchronous calls run on a worker thread via
    `anyio.to_thread.run_sync`, never the connection's own thread) plus `PRAGMA journal_mode=WAL` and
    `PRAGMA foreign_keys=ON` on every new connection. An in-memory database (`:memory:`, or no path at
    all) gets `StaticPool` so the single underlying connection — and the schema it holds — survives
    across `Engine.connect()` calls instead of vanishing at the end of each one.
    """
    normalized = normalize_url(url)
    parsed = make_url(normalized)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(normalized)

    is_memory = parsed.database in (None, "", ":memory:")
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if is_memory:
        kwargs["poolclass"] = StaticPool
    engine = create_engine(normalized, **kwargs)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        # pysqlite manages its own implicit transactions unless isolation_level is disabled; without
        # this, the explicit SAVEPOINTs that claim_write's atomic claim-or-read step relies on
        # (session.begin_nested()) misbehave. The paired "begin" listener below issues the real BEGIN.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_sqlite_transaction(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    # `%` is ConfigParser interpolation syntax; escape any literal `%` in the URL (e.g. a URL-encoded
    # password) so alembic's env.py reads it back unchanged via `config.get_main_option`.
    cfg.set_main_option("sqlalchemy.url", normalize_url(url).replace("%", "%%"))
    return cfg


def run_upgrade(connection: Connection, url: str, revision: str) -> None:
    cfg = _alembic_config(url)
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, revision)


def upgrade(url: str, revision: str = "head") -> None:
    """Apply migrations up to `revision` (default: head) against a fresh engine for `url`."""
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            run_upgrade(connection, url, revision)
            connection.commit()
    finally:
        engine.dispose()


def current_revision(url: str) -> str | None:
    """The revision applied at `url`, or `None` if the store has never been migrated."""
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

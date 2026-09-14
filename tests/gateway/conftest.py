"""Store fixtures: SQLite always; Postgres too when BENCHPRESS_TEST_POSTGRES_URL is set (CI service)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from benchpress.gateway.store import Store

POSTGRES_ENV = "BENCHPRESS_TEST_POSTGRES_URL"


def _urls() -> list[str]:
    urls = ["sqlite"]
    if os.environ.get(POSTGRES_ENV):
        urls.append("postgres")
    return urls


@pytest.fixture(params=_urls())
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    if request.param == "sqlite":
        opened = Store.open(f"sqlite:///{tmp_path / 'benchpress.db'}")
        yield opened
        opened.engine.dispose()
        return
    from sqlalchemy import text

    base = os.environ[POSTGRES_ENV]
    schema = f"t_{uuid.uuid4().hex[:10]}"
    admin = Store.open(base, migrate=False)
    with admin.engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = f"{base}{'&' if '?' in base else '?'}options=-csearch_path%3D{schema}"
    opened = Store.open(url)
    yield opened
    opened.engine.dispose()
    with admin.engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.engine.dispose()

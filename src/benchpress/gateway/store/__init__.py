"""`benchpress.gateway.store`: the durable state behind the Benchpress gateway (SQLAlchemy 2 + Alembic).

Needs the `server` extra:

    pip install "benchpress-agent[server]"
"""

from __future__ import annotations

try:
    import sqlalchemy as _sqlalchemy_probe  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as exc:
    raise ImportError(
        'benchpress.gateway needs the server extra: pip install "benchpress-agent[server]"'
    ) from exc

from benchpress.gateway.store.engine import current_revision, make_engine, normalize_url, upgrade
from benchpress.gateway.store.repo import (
    ApiKeyRow,
    ApprovalRow,
    ParsedWrite,
    PolicyRow,
    ReceiptRow,
    SessionExists,
    SqlIdempotencyStore,
    Store,
    WorkspaceRow,
    parse_write_line,
)

__all__ = [
    "ApiKeyRow",
    "ApprovalRow",
    "ParsedWrite",
    "PolicyRow",
    "ReceiptRow",
    "SessionExists",
    "SqlIdempotencyStore",
    "Store",
    "WorkspaceRow",
    "current_revision",
    "make_engine",
    "normalize_url",
    "parse_write_line",
    "upgrade",
]

"""The Benchpress gateway: a multi-tenant HTTP front door over `benchpress.verified.VerifiedWrite`.

Needs the `server` extra (`pip install "benchpress-agent[server]"`) — nothing here is imported by
`import benchpress`, so the core library never pulls in FastAPI, SQLAlchemy, or Alembic.
"""

from __future__ import annotations

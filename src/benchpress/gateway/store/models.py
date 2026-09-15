"""SQLAlchemy 2 ORM models for the gateway's durable store.

One `DeclarativeBase` with a naming convention on its `MetaData`, so every constraint and index gets a
deterministic name — the hand-written Alembic migration in `migrations/versions/0001_initial.py` names
them identically, which is what lets `alembic.autogenerate.compare_metadata` certify the two agree.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, MetaData, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[str] = mapped_column(String(32))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[str] = mapped_column(String(32))
    revoked_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SessionModel(Base):
    """Maps the `sessions` table. Named to avoid colliding with `sqlalchemy.orm.Session`."""

    __tablename__ = "sessions"
    __table_args__ = (PrimaryKeyConstraint("workspace_id", "id", name="pk_sessions"),)

    id: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"))
    context: Mapped[str] = mapped_column(Text)
    idempotency_scope: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(32))


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        Index("ix_receipts_workspace_id_id", "workspace_id", "id"),
        Index("ix_receipts_workspace_id_provider", "workspace_id", "provider"),
        Index("ix_receipts_workspace_id_rule", "workspace_id", "rule"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"))
    session_id: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(32))
    at: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rule: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(Text)
    refs: Mapped[str] = mapped_column(Text)
    line: Mapped[str] = mapped_column(Text)


class Write(Base):
    __tablename__ = "writes"

    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    holder: Mapped[str] = mapped_column(String(32))
    claimed_at: Mapped[float] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_workspace_id_status", "workspace_id", "status"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"))
    session_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    rule_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    requested_at: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[float] = mapped_column(Float)
    resolved_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str] = mapped_column(Text)


class Policy(Base):
    __tablename__ = "policies"

    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(32))

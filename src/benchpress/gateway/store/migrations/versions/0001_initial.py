"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-14 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("name", name="uq_workspaces_name"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=12), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_api_keys_workspace_id_workspaces"),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("idempotency_scope", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_sessions_workspace_id_workspaces"),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_sessions"),
    )
    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("at", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("refs", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_receipts_workspace_id_workspaces"),
        sa.PrimaryKeyConstraint("id", name="pk_receipts"),
    )
    op.create_index("ix_receipts_workspace_id_id", "receipts", ["workspace_id", "id"])
    op.create_index("ix_receipts_workspace_id_provider", "receipts", ["workspace_id", "provider"])
    op.create_index("ix_receipts_workspace_id_rule", "receipts", ["workspace_id", "rule"])
    op.create_table(
        "writes",
        sa.Column("scope", sa.String(length=160), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("holder", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("scope", "fingerprint", name="pk_writes"),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("rule_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("resolved_at", sa.String(length=32), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_approvals_workspace_id_workspaces"),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
    )
    op.create_index("ix_approvals_workspace_id_status", "approvals", ["workspace_id", "status"])
    op.create_table(
        "policies",
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_policies_workspace_id_workspaces"),
        sa.PrimaryKeyConstraint("workspace_id", "name", name="pk_policies"),
    )


def downgrade() -> None:
    op.drop_table("policies")
    op.drop_index("ix_approvals_workspace_id_status", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("writes")
    op.drop_index("ix_receipts_workspace_id_rule", table_name="receipts")
    op.drop_index("ix_receipts_workspace_id_provider", table_name="receipts")
    op.drop_index("ix_receipts_workspace_id_id", table_name="receipts")
    op.drop_table("receipts")
    op.drop_table("sessions")
    op.drop_table("api_keys")
    op.drop_table("workspaces")

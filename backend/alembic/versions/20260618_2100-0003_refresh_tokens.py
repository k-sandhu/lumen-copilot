"""refresh_tokens — rotating, revocable refresh tokens (#19, spec 0004 §2.3)

Adds the ``refresh_tokens`` table backing app-managed identity (CC-3). Only the
**hash** of the opaque token is stored (``token_hash``, unique) so a DB read
yields no usable token; ``revoked_at`` makes a token unusable (logout/rotation)
and ``expires_at`` caps its lifetime. Tenant-scoped like every other table
(non-null, indexed ``tenant_id`` FK → ``tenants``, INV-1) and ``user_id`` FK →
``users`` (CASCADE so deleting a user drops their tokens).

Hand-written and reversible (backend/AGENTS.md "Data & migrations"). No
``vector``/JSONB here; portable column types so the model also creates cleanly on
SQLite for the offline repository tests.

Revision ID: 0003_refresh_tokens
Revises: 0002_mvp_schema
Create Date: 2026-06-18 21:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_refresh_tokens"
down_revision: str | None = "0002_mvp_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("refresh_tokens")

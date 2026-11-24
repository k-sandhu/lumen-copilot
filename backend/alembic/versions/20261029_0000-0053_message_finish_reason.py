"""Message finish_reason for cancelled partial answers (#574)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_message_finish_reason"
down_revision = "0052_sandbox_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = ordinary/legacy turn. "cancelled" marks a partial assistant
    # answer persisted at user Stop so history reload renders it as stopped.
    op.add_column("messages", sa.Column("finish_reason", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "finish_reason")

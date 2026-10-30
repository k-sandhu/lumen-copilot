"""Persist the server-selected research limit kind (#833, spec 0028)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_answer_limit"
down_revision = "0054_billed_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("limit_kind", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "limit_kind")

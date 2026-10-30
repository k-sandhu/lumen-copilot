"""Persist effective sandbox limits + termination reason on code runs (#589)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_sandbox_limits"
down_revision = "0051_provider_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: NULL means the run predates bounded admission (#589), which
    # is a different fact from an empty limits record.
    op.add_column(
        "code_runs",
        sa.Column(
            "effective_limits",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.add_column("code_runs", sa.Column("termination_reason", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("code_runs", "termination_reason")
    op.drop_column("code_runs", "effective_limits")

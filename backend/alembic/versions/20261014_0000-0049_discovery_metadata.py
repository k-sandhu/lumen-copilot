"""Nullable public document discovery metadata (#627, PR #643)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049_discovery_metadata"
down_revision = "0048_evidence_handles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing uploads remain readable through filename fallback. Source dates
    # and public metadata stay unknown until resync; never invent provenance.
    op.add_column("documents", sa.Column("title", sa.String(512), nullable=True))
    op.add_column("documents", sa.Column("source_path", sa.Text(), nullable=True))
    op.add_column(
        "documents", sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column(
            "discovery_metadata",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "discovery_metadata")
    op.drop_column("documents", "source_modified_at")
    op.drop_column("documents", "source_path")
    op.drop_column("documents", "title")

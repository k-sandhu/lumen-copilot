"""Retain exact extraction text and source-part maps (#621).

Revision ID: 0044_ingestion_locations
Revises: 0043_code_run_resolved_packages
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_ingestion_locations"
down_revision: str | None = "0043_code_run_resolved_packages"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_text", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("ingestion_metadata", postgresql.JSONB(), nullable=True))
    op.add_column("chunks", sa.Column("source_locations", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "source_locations")
    op.drop_column("documents", "ingestion_metadata")
    op.drop_column("documents", "source_text")

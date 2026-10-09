"""Separate derived context from evidence; tenant-scoped operational generation cache."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_chunk_context"
down_revision = "0046_message_source_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("context_text", sa.Text(), nullable=False, server_default=""))
    op.add_column("chunks", sa.Column("generated_context", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("context_fingerprint", sa.String(64), nullable=True))
    op.add_column("chunks", sa.Column("context_metadata", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for name in ("context_metadata", "context_fingerprint", "generated_context", "context_text"):
        op.drop_column("chunks", name)

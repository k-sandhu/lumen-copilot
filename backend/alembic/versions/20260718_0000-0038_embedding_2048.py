"""widen chunk embeddings to 2,048 dimensions for Nemotron 3 Embed (#449)

The configured default embedding model now returns native 2,048-dimension
vectors. The relational chunk store keeps a fixed-width pgvector column, so
the schema and gateway configuration must move in lockstep.

Upgrade preserves every existing chunk by zero-padding its prior 1,024-value
vector. Those padded vectors are only a continuity bridge: an operational
re-embedding pass can replace them with native Nemotron embeddings. The obsolete
Postgres HNSW index stays dropped because pgvector caps HNSW at 2,000 dimensions
and ADR-0010 made OpenSearch the single retrieval store. Downgrade is reversible
by retaining the leading 1,024 values and restoring the legacy index.

Revision ID: 0038_embedding_2048
Revises: 0037_session_summaries
Create Date: 2026-07-18 00:00:00+00:00
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038_embedding_2048"
down_revision: str | None = "0037_session_summaries"
branch_labels: str | None = None
depends_on: str | None = None

_ANN_INDEX = "ix_chunks_embedding_hnsw"


def upgrade() -> None:
    """Pad existing vectors and widen the now-non-indexed legacy column."""
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {_ANN_INDEX}")
    op.execute(
        "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(2048) "
        "USING (CASE WHEN embedding IS NULL THEN NULL ELSE "
        "((embedding::real[]) || array_fill(0::real, ARRAY[1024]))::vector(2048) END)"
    )


def downgrade() -> None:
    """Restore the original width from the leading 1,024 vector values."""
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {_ANN_INDEX}")
    op.execute(
        "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1024) "
        "USING (CASE WHEN embedding IS NULL THEN NULL ELSE "
        "subvector(embedding, 1, 1024)::vector(1024) END)"
    )
    op.execute(f"CREATE INDEX {_ANN_INDEX} ON chunks USING hnsw " "(embedding vector_cosine_ops)")

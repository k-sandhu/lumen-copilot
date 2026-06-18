"""retrieval indexes — pgvector ANN + full-text GIN on chunks (#45)

Adds the two indexes hybrid retrieval needs (ADR-0003 §3), deferred here from the
#21/#44 schema work as the issue directs:

* an **HNSW ANN index** on ``chunks.embedding`` using ``vector_cosine_ops`` so the
  semantic leg (``embedding <=> :query``, cosine distance) is sublinear instead of
  a full scan. HNSW (not IVFFlat) needs no training/``lists`` tuning and gives
  good recall out of the box for the MVP corpus; the operator class matches the
  cosine distance the retrieval query uses.
* a **GIN index** on ``to_tsvector('english', text)`` so the lexical leg
  (``@@ plainto_tsquery``) is index-backed. The expression in the index matches
  the expression in the query exactly (same language config) so Postgres uses it.

Both are created on the existing ``chunks`` table (0002). Reversible: the
downgrade drops both. Postgres-only by nature (pgvector + full-text); the offline
DDL render asserts both ``CREATE INDEX`` statements appear, and the live apply is
exercised against the compose database.

Note: ``CREATE INDEX`` is run plainly (not ``CONCURRENTLY``) so it participates in
the migration transaction and reverses cleanly. At MVP corpus size this is fine;
a concurrent build for a large live table is a documented operational follow-up.

Revision ID: 0004_retrieval_indexes
Revises: 0003_refresh_tokens
Create Date: 2026-06-18 23:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_retrieval_indexes"
down_revision: str | None = "0003_refresh_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Index names (stable, referenced by the downgrade).
_ANN_INDEX = "ix_chunks_embedding_hnsw"
_FTS_INDEX = "ix_chunks_text_fts"


def upgrade() -> None:
    # pgvector HNSW ANN index for cosine-distance semantic search. The operator
    # class (vector_cosine_ops) must match the distance operator (<=>) the query
    # uses, or the planner will not use the index.
    op.execute(f"CREATE INDEX {_ANN_INDEX} ON chunks USING hnsw (embedding vector_cosine_ops)")
    # Full-text GIN index on the same expression the lexical query matches.
    op.execute(f"CREATE INDEX {_FTS_INDEX} ON chunks USING gin (to_tsvector('english', text))")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_FTS_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_ANN_INDEX}")

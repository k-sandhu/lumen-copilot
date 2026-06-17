"""enable pgvector extension

The first migration. Enables the ``vector`` extension (pgvector) so that
embedding columns can live transactionally beside their metadata/ACLs in
Postgres (ADR-0003 §3) — the basis for permission-aware retrieval as a ``WHERE``
clause. No tables yet; feature migrations add models on top of this base.

Revision ID: 0001_enable_pgvector
Revises:
Create Date: 2026-06-17 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_enable_pgvector"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector;")

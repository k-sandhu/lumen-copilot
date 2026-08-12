"""Native 2,048-dimension embedding contract with lossless rollback (#346).

The old provider produced 1,024-float vectors.  The replacement route produces
2,048 floats natively, and pgvector cannot change a vector column's width without
rewriting its values.  Padding would fabricate a different vector space and
truncation would destroy data, so this migration does neither:

* the old column is renamed ``embedding_legacy_1024`` byte-for-byte;
* a nullable ``embedding vector(2048)`` is added for controlled re-embedding;
* the obsolete 1,024 HNSW index is removed (OpenSearch is the retrieval store);
* durable, content-safe ingestion attempt/failure telemetry is added.

Downgrade parks the new vectors as ``embedding_2048`` before restoring the old
column and index.  A later re-upgrade recognizes that parked column and restores
it, so repeated controlled upgrade/downgrade rehearsals do not discard either
vector set.

Revision ID: 0044_embedding_contract
Revises: 0043_code_run_resolved_packages
Create Date: 2026-08-11 00:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_embedding_contract"
down_revision = "0043_code_run_resolved_packages"
branch_labels = None
depends_on = None

_OLD_ANN_INDEX = "ix_chunks_embedding_hnsw"


def upgrade() -> None:
    # This index has vector(1024) operator metadata and cannot follow a renamed
    # column into the new vector space. OpenSearch is the single retrieval store
    # (ADR-0010), so no replacement pgvector ANN index is created.
    op.execute(f"DROP INDEX IF EXISTS {_OLD_ANN_INDEX}")
    op.execute(
        """
DO $migration$
DECLARE
    active_type text;
    legacy_type text;
    parked_type text;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO active_type
      FROM pg_attribute a
     WHERE a.attrelid = 'chunks'::regclass
       AND a.attname = 'embedding'
       AND NOT a.attisdropped;

    SELECT format_type(a.atttypid, a.atttypmod)
      INTO legacy_type
      FROM pg_attribute a
     WHERE a.attrelid = 'chunks'::regclass
       AND a.attname = 'embedding_legacy_1024'
       AND NOT a.attisdropped;

    SELECT format_type(a.atttypid, a.atttypmod)
      INTO parked_type
      FROM pg_attribute a
     WHERE a.attrelid = 'chunks'::regclass
       AND a.attname = 'embedding_2048'
       AND NOT a.attisdropped;

    IF active_type = 'vector(1024)' AND legacy_type IS NULL THEN
        ALTER TABLE chunks RENAME COLUMN embedding TO embedding_legacy_1024;
        legacy_type := 'vector(1024)';
        active_type := NULL;
    ELSIF active_type IS NOT NULL AND active_type <> 'vector(2048)' THEN
        RAISE EXCEPTION 'embedding contract: bad active vector type %', active_type;
    END IF;

    IF legacy_type IS NULL OR legacy_type <> 'vector(1024)' THEN
        RAISE EXCEPTION 'embedding contract: bad legacy vector type %',
            coalesce(legacy_type, '<missing>');
    END IF;

    IF active_type IS NULL THEN
        IF parked_type IS NULL THEN
            ALTER TABLE chunks ADD COLUMN embedding vector(2048);
        ELSIF parked_type = 'vector(2048)' THEN
            ALTER TABLE chunks RENAME COLUMN embedding_2048 TO embedding;
        ELSE
            RAISE EXCEPTION 'embedding contract: bad parked vector type %', parked_type;
        END IF;
    END IF;
END
$migration$;
"""
    )

    op.add_column(
        "documents",
        sa.Column(
            "ingestion_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Inserts must go through the repository claim seam; do not let the database
    # silently manufacture attempt counts for future rows.
    op.alter_column("documents", "ingestion_attempts", server_default=None)
    op.add_column(
        "documents",
        sa.Column(
            "ingestion_failure",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "ingestion_failure")
    op.drop_column("documents", "ingestion_attempts")
    op.execute(f"DROP INDEX IF EXISTS {_OLD_ANN_INDEX}")
    op.execute(
        """
DO $migration$
DECLARE
    active_type text;
    legacy_type text;
    parked_type text;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO active_type
      FROM pg_attribute a
     WHERE a.attrelid = 'chunks'::regclass
       AND a.attname = 'embedding'
       AND NOT a.attisdropped;

    SELECT format_type(a.atttypid, a.atttypmod)
      INTO legacy_type
      FROM pg_attribute a
     WHERE a.attrelid = 'chunks'::regclass
       AND a.attname = 'embedding_legacy_1024'
       AND NOT a.attisdropped;

    SELECT format_type(a.atttypid, a.atttypmod)
      INTO parked_type
      FROM pg_attribute a
     WHERE a.attrelid = 'chunks'::regclass
       AND a.attname = 'embedding_2048'
       AND NOT a.attisdropped;

    IF active_type <> 'vector(2048)' THEN
        RAISE EXCEPTION 'embedding rollback: bad active vector type %',
            coalesce(active_type, '<missing>');
    END IF;
    IF legacy_type <> 'vector(1024)' THEN
        RAISE EXCEPTION 'embedding rollback: bad legacy vector type %',
            coalesce(legacy_type, '<missing>');
    END IF;
    IF parked_type IS NOT NULL THEN
        RAISE EXCEPTION 'embedding rollback: chunks.embedding_2048 already exists';
    END IF;

    ALTER TABLE chunks RENAME COLUMN embedding TO embedding_2048;
    ALTER TABLE chunks RENAME COLUMN embedding_legacy_1024 TO embedding;
END
$migration$;
"""
    )
    op.execute(f"CREATE INDEX {_OLD_ANN_INDEX} ON chunks USING hnsw (embedding vector_cosine_ops)")

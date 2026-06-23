"""sources table + documents.source_id link (#109, ADR-0009 §4/§5)

The single M2 schema step for the connector framework (ADR-0008 §4, single-head
invariant — ``down_revision`` is the current head ``0005``). It adds exactly what
the Sources surface needs:

* a tenant- and owner-scoped ``sources`` table — ``id``, ``tenant_id``,
  ``owner_id``, ``type`` (connector name, e.g. ``web``), ``config`` (JSONB: the
  connector's opaque ``{url, mode}``), ``status`` (``pending|syncing|ready|error``),
  ``last_synced_at``, ``indexed_count`` (default 0), ``last_error``, plus the
  standard ``created_at``/``updated_at``. Tenant-scoped like every table: a
  non-null ``tenant_id`` FK → ``tenants`` with an index **leading with
  ``tenant_id``** (INV-1, the repository predicate column), and an ``owner_id``
  index for the "user sees only their own" reads.

* a **nullable** ``source_id`` FK on ``documents`` → ``sources``: ingested docs
  link back to the source that produced them; direct uploads leave it null.
  ``ON DELETE CASCADE`` so deleting a source removes the documents it ingested
  (ADR-0009 §5), which in turn cascades to their ``chunks``. An index on
  ``source_id`` keeps "this source's documents" and the cascade cheap.

Hand-written and reversible (backend/AGENTS.md "Data & migrations"): ``downgrade``
drops the ``source_id`` column (and its index) first, then the ``sources`` table.
JSONB is Postgres-only by nature; the offline DDL render asserts the shapes, and
the live apply runs against a disposable throwaway database (never the app DB —
the #70 lesson).

Revision ID: 0006_sources
Revises: 0005_audit_query_indexes
Create Date: 2026-06-23 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_sources"
down_revision: str | None = "0005_audit_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- sources (tenant- and owner-scoped, deny-by-default) ----------------
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Tenant predicate column, leading the index (INV-1).
    op.create_index("ix_sources_tenant_id", "sources", ["tenant_id"])
    # Owner-scoped reads ("user sees only their own").
    op.create_index("ix_sources_owner_id", "sources", ["owner_id"])

    # --- documents.source_id (nullable FK → sources, CASCADE) ----------------
    op.add_column(
        "documents",
        sa.Column(
            "source_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_documents_source_id", "documents", ["source_id"])


def downgrade() -> None:
    # Reverse order: drop the documents link first, then the sources table.
    op.drop_index("ix_documents_source_id", table_name="documents")
    op.drop_column("documents", "source_id")
    op.drop_table("sources")

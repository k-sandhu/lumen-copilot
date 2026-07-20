"""gdrive_acl — mirrored source ACLs + incremental-sync identity (#453)

ADR-0019 §2/§3 (F-CB-2):

* ``documents`` gains the mirrored-ACL surface: ``acl_enforced`` (the exclusive
  enforcement mode — ``server_default false`` exists SOLELY to backfill the
  pre-existing upload/``web`` rows and is **dropped again in this same
  migration**, so no insert path can ever default the mode; it is a mandatory
  no-default argument at the write seam),
  ``acl_principals`` (the mirrored principal-set), ``acl_synced_at`` (freshness;
  NULL ⇒ deny), ``acl_scope_ids`` (the container scope chain the cascade
  stale-stamp matches on), and ``external_id`` (the provider's stable id) with a
  **partial unique index** on ``(source_id, external_id)`` — identity-based
  reconcile; direct uploads (NULL ``external_id``) are unconstrained.
* ``sources`` gains the ACL-mirror health surface the wire's ``GdriveSource``
  reports: ``acl_synced_at`` + ``unmapped_acl_count``, plus
  ``acl_resync_required`` — ADR-0019 §3's durable full-resync-required state.

No new tables; the RLS backstop on ``documents``/``sources`` (0007) covers the
new columns. Reversible (backend/AGENTS.md): ``downgrade`` drops the index and
the columns.

Revision ID: 0040_gdrive_acl
Revises: 0039_connector_oauth
Create Date: 2026-07-18 19:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0040_gdrive_acl"
down_revision: str | None = "0039_connector_oauth"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # --- documents: the mirrored-ACL surface (ADR-0019 §2/§3) ---------------
    op.add_column(
        "documents",
        sa.Column(
            "acl_enforced",
            sa.Boolean(),
            nullable=False,
            # Backfill-only default: every pre-existing row is an upload/`web`
            # document (owner-or-grant mode). Never a code path (ADR-0019 §2).
            server_default=sa.false(),
        ),
    )
    # ...and DROP it immediately, in this same migration. ADR-0019 §2's
    # write-time discipline is "the mode is never defaulted": with the default
    # left in place, any future insert path that forgets the column would
    # silently create an owner/grant-governed document out of connector
    # content. Post-drop, omitting the mode is a NOT NULL violation — the
    # failure direction we want. The ORM carries no ``default`` either; the one
    # write seam (``DocumentRepository.create``) takes it as a mandatory
    # argument.
    op.alter_column("documents", "acl_enforced", server_default=None)
    op.add_column("documents", sa.Column("acl_principals", _JSON, nullable=True))
    op.add_column(
        "documents",
        sa.Column("acl_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("documents", sa.Column("acl_scope_ids", _JSON, nullable=True))
    op.add_column("documents", sa.Column("external_id", sa.Text(), nullable=True))
    op.create_index(
        "uq_documents_source_external_id",
        "documents",
        ["source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )

    # --- sources: ACL-mirror health surface (ADR-0019 §2) -------------------
    op.add_column(
        "sources",
        sa.Column("acl_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("sources", sa.Column("unmapped_acl_count", sa.Integer(), nullable=True))
    # ADR-0019 §3's full-resync-required state. An ``integrity=incomplete`` page
    # stale-stamps EVERY mirrored document of the source, and only a full
    # re-examination can restore them — so the requirement is durable (it must
    # survive a crash) and sticky (it outlives any number of incremental
    # retries). Cleared only by a completed full sync. NULL = false.
    op.add_column("sources", sa.Column("acl_resync_required", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "acl_resync_required")
    op.drop_column("sources", "unmapped_acl_count")
    op.drop_column("sources", "acl_synced_at")

    op.drop_index("uq_documents_source_external_id", table_name="documents")
    op.drop_column("documents", "external_id")
    op.drop_column("documents", "acl_scope_ids")
    op.drop_column("documents", "acl_synced_at")
    op.drop_column("documents", "acl_principals")
    op.drop_column("documents", "acl_enforced")

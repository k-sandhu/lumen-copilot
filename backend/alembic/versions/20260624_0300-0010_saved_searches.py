"""saved_searches — per-user saved /search queries (spec 0005, epic #144)

The persistence half of the saved-searches slice (spec 0005 §4): one row per
saved search, owner- and tenant-scoped. ``query`` + the nullable
``collection_id`` / ``source`` / ``type`` capture exactly what ``/search``
accepts, so applying a saved search re-runs the same query.

Shape:

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed, INV-1) and
  ``owner_id`` FK → ``users`` (non-null, indexed — deny-by-default ownership);
* ``name`` (≤200), ``query`` (≤1000);
* ``collection_id`` (nullable uuid — the /search collection filter; a plain
  column, not an FK: a deleted collection should not cascade-delete a saved
  search, and the search itself re-validates access at run time),
  ``source`` (nullable, ≤20 — the ResultSource filter), ``type`` (nullable, ≤100);
* ``created_at`` / ``updated_at`` (server-defaulted UTC).

Indexes: a composite ``(tenant_id, owner_id)`` for the owner-scoped list (the hot
path) plus the tenant-leading FK index.

RLS: tenant-scoped, so this migration also ``ENABLE`` + ``FORCE`` row-level
security with the same fail-closed ``app.tenant_id`` GUC policy the ``0007``
backstop uses (spec 0004 §2.1, #17) — additive.

Reversible: ``downgrade`` drops the policy, disables RLS, the indexes, and the
table.

Revision ID: 0010_saved_searches
Revises: 0009_user_preferences
Create Date: 2026-06-24 03:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_saved_searches"
down_revision: str | None = "0009_user_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC = "app.tenant_id"
_BYPASS = "bypass"
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        "saved_searches",
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
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("query", sa.String(length=1000), nullable=False),
        # The /search filters. collection_id is a plain column (not an FK): a
        # deleted collection must not cascade-delete a saved search, and /search
        # re-validates access at run time anyway.
        sa.Column("collection_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("type", sa.String(length=100), nullable=True),
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
    op.create_index("ix_saved_searches_tenant_id", "saved_searches", ["tenant_id"])
    op.create_index("ix_saved_searches_owner_id", "saved_searches", ["owner_id"])
    # The owner-scoped list hot path.
    op.create_index("ix_saved_searches_tenant_owner", "saved_searches", ["tenant_id", "owner_id"])

    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE saved_searches ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE saved_searches FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_saved_searches ON saved_searches "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_saved_searches ON saved_searches")
        op.execute("ALTER TABLE saved_searches NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE saved_searches DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_saved_searches_tenant_owner", table_name="saved_searches")
    op.drop_index("ix_saved_searches_owner_id", table_name="saved_searches")
    op.drop_index("ix_saved_searches_tenant_id", table_name="saved_searches")
    op.drop_table("saved_searches")

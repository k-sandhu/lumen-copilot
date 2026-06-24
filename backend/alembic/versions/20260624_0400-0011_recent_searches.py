"""recent_searches — per-user recent /search history (spec 0005, epic #144)

The persistence half of the recent-search history (spec 0005 §4): one row per
distinct (normalized) query a user ran, with ``last_used_at`` bumped on re-run.
Powers the typeahead's recent group and ``GET /search/recent``.

Shape:

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed, INV-1) and
  ``user_id`` FK → ``users`` (non-null, indexed);
* ``query`` (≤1000, the latest display form) + ``normalized_query`` (≤1000, the
  trimmed/lower-cased dedupe key);
* ``last_used_at`` (server-defaulted UTC; the repository bumps it on re-run).

A UNIQUE on ``(tenant_id, user_id, normalized_query)`` makes re-running a query an
upsert (one row, not a pile); a ``(tenant_id, user_id, last_used_at)`` index serves
the newest-first list + the oldest-eviction cap.

RLS: tenant-scoped, so this migration also ``ENABLE`` + ``FORCE`` row-level
security with the fail-closed ``app.tenant_id`` GUC policy the ``0007`` backstop
uses (spec 0004 §2.1, #17).

Reversible: ``downgrade`` drops the policy, disables RLS, the indexes, and the table.

Revision ID: 0011_recent_searches
Revises: 0010_saved_searches
Create Date: 2026-06-24 04:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_recent_searches"
down_revision: str | None = "0010_saved_searches"
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
        "recent_searches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query", sa.String(length=1000), nullable=False),
        sa.Column("normalized_query", sa.String(length=1000), nullable=False),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "normalized_query", name="uq_recent_searches_tenant_user_norm"
        ),
    )
    op.create_index("ix_recent_searches_tenant_id", "recent_searches", ["tenant_id"])
    op.create_index("ix_recent_searches_user_id", "recent_searches", ["user_id"])
    op.create_index(
        "ix_recent_searches_tenant_user_used",
        "recent_searches",
        ["tenant_id", "user_id", "last_used_at"],
    )

    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE recent_searches ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE recent_searches FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_recent_searches ON recent_searches "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_recent_searches ON recent_searches")
        op.execute("ALTER TABLE recent_searches NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE recent_searches DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_recent_searches_tenant_user_used", table_name="recent_searches")
    op.drop_index("ix_recent_searches_user_id", table_name="recent_searches")
    op.drop_index("ix_recent_searches_tenant_id", table_name="recent_searches")
    op.drop_table("recent_searches")

"""user_preferences — per-user account preferences (spec 0005, epic #144)

The persistence half of the preferences slice (spec 0005 §4): one row per user
holding a ``default_model`` override (a ``/models`` id, nullable). Created lazily
on the first ``PATCH /preferences`` — a fresh user has no row and ``GET`` returns
the implicit server-default state without writing (read-before-write).

Shape:

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed — every row
  is tenant-scoped, INV-1) and ``user_id`` FK → ``users`` (non-null, indexed);
* ``default_model`` (nullable string — the override, or NULL for the server
  default; validated against the model registry at write time, fail-closed at
  chat time);
* ``created_at`` / ``updated_at`` (server-defaulted UTC; ``updated_at`` is bumped
  app-side via the ORM ``onupdate``).

A UNIQUE on ``(tenant_id, user_id)`` makes the row a per-user singleton (the
upsert target — re-setting a preference updates the same row, never duplicates).

RLS: ``user_preferences`` is tenant-scoped, so this migration also ``ENABLE`` +
``FORCE`` row-level security with the same fail-closed ``app.tenant_id`` GUC
policy the ``0007`` backstop uses for every other tenant-scoped table (spec 0004
§2.1, #17) — additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, the
indexes, and the table, restoring the pre-#144 state.

Revision ID: 0009_user_preferences
Revises: 0008_grants
Create Date: 2026-06-24 02:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_user_preferences"
down_revision: str | None = "0008_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The GUC + bypass sentinel the RLS policy reads — in lockstep with the ``0007``
# backstop and ``app.db.tenant_context`` (TENANT_GUC / BYPASS_SENTINEL).
_GUC = "app.tenant_id"
_BYPASS = "bypass"
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        "user_preferences",
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
        # The default-model override, or NULL to use the server default.
        sa.Column("default_model", sa.String(length=255), nullable=True),
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
        # One preferences row per user within a tenant — the upsert target.
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_user_preferences_tenant_user"),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_user_preferences_tenant_id", "user_preferences", ["tenant_id"])
    op.create_index("ix_user_preferences_user_id", "user_preferences", ["user_id"])

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE user_preferences FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_user_preferences ON user_preferences "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_user_preferences ON user_preferences")
        op.execute("ALTER TABLE user_preferences NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE user_preferences DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_user_preferences_user_id", table_name="user_preferences")
    op.drop_index("ix_user_preferences_tenant_id", table_name="user_preferences")
    op.drop_table("user_preferences")

"""groups + group membership — the group principal behind grants (ADR-0022)

The persistence half of the group access model (ADR-0022,
``docs/architecture/0022-group-access-model.md``), which claims the
``group``/``role`` principal follow-up spec 0004 §2.2 marked
``revisit-at-implementation`` (#18). A **group** is a tenant-scoped set of users
an admin manages; a group **grant** (``grants.principal_type='group'``, already
admitted by the ``0008`` check constraint) makes a resource visible to every
member. No migration was needed on ``grants`` — that was the point of modelling
``principal_type`` wide from the start.

Shape:

* ``groups`` — ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null,
  indexed — INV-1), ``name``, ``kind`` ∈ {``user``, ``system``},
  ``created_by`` FK → ``users`` (SET NULL), ``created_at``/``updated_at``.
  UNIQUE on ``(tenant_id, lower(name))`` so a group name is a per-tenant
  singleton **case-insensitively** — "Tax Team" and "tax team" are the same
  group, which is what an admin typing into a box expects.
* ``group_members`` — ``group_id`` FK → ``groups`` (CASCADE: deleting a group
  removes its membership), ``user_id`` FK → ``users`` (CASCADE: deleting a user
  removes them from every group), ``tenant_id`` (denormalized so the table is
  tenant-scoped like every other and can carry the same RLS policy),
  ``added_by``, ``added_at``. UNIQUE on ``(group_id, user_id)`` so adding a
  member twice is idempotent; indexed on ``(tenant_id, user_id)`` because the
  hot path is "which groups is *this requester* in?" — read once per request by
  the retrieval allow-set.

``kind='system'`` marks the per-tenant "All members" group that expresses
tenant-wide visibility. Its membership is **derived, never materialized**
(ADR-0022 §3): no ``group_members`` rows are written for it, and the membership
resolver adds it to every user in the tenant. A partial UNIQUE index enforces
**at most one** system group per tenant. Materializing it would mean
back-filling every existing user and hooking user creation forever — a missed
hook silently denies access, which a derived membership cannot do.

RLS: both tables are tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security with the same fail-closed ``app.tenant_id`` GUC policy the
``0007`` backstop uses (spec 0004 §2.1, #17) — additive, never relaxing an
existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policies, disables RLS,
and drops both tables, restoring the pre-ADR-0022 state.

Revision ID: 0041_groups
Revises: 0040_gdrive_acl
Create Date: 2026-07-30 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041_groups"
down_revision: str | None = "0040_gdrive_acl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The GUC + bypass sentinel the RLS policy reads — kept in lockstep with the
# ``0007`` backstop and ``app.db.tenant_context`` (TENANT_GUC / BYPASS_SENTINEL).
_GUC = "app.tenant_id"
_BYPASS = "bypass"
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)

_RLS_TABLES = ("groups", "group_members")


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        # ``system`` is the per-tenant "All members" group (ADR-0022 §3): it
        # cannot be renamed, deleted, or explicitly populated, and its
        # membership is derived rather than stored.
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column(
            "created_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.CheckConstraint("kind in ('user', 'system')", name="ck_groups_kind"),
    )
    op.create_index("ix_groups_tenant_id", "groups", ["tenant_id"])
    # Case-insensitive per-tenant uniqueness: "Tax Team" == "tax team".
    op.create_index(
        "uq_groups_tenant_name_lower",
        "groups",
        ["tenant_id", sa.text("lower(name)")],
        unique=True,
    )
    # At most one system ("All members") group per tenant — a partial unique
    # index, so ordinary groups are unaffected.
    op.create_index(
        "uq_groups_tenant_system",
        "groups",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'system'"),
        sqlite_where=sa.text("kind = 'system'"),
    )

    op.create_table(
        "group_members",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        # Denormalized so this table is tenant-scoped like every other one and
        # can carry the identical RLS policy (INV-1).
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "added_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Adding a member twice is idempotent, never a duplicate row.
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_members_group_user"),
    )
    op.create_index("ix_group_members_tenant_id", "group_members", ["tenant_id"])
    # The hot path, read once per request: "which groups is this requester in?"
    op.create_index("ix_group_members_tenant_user", "group_members", ["tenant_id", "user_id"])
    # The admin path: "who is in this group?"
    op.create_index("ix_group_members_group", "group_members", ["group_id"])

    if op.get_context().dialect.name == "postgresql":
        for table in _RLS_TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_{table} ON {table} "
                f"USING ({_PREDICATE}) "
                f"WITH CHECK ({_PREDICATE})"
            )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        for table in _RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_group_members_group", table_name="group_members")
    op.drop_index("ix_group_members_tenant_user", table_name="group_members")
    op.drop_index("ix_group_members_tenant_id", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index("uq_groups_tenant_system", table_name="groups")
    op.drop_index("uq_groups_tenant_name_lower", table_name="groups")
    op.drop_index("ix_groups_tenant_id", table_name="groups")
    op.drop_table("groups")

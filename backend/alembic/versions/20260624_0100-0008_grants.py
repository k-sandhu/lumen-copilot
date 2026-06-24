"""explicit ACL grants — tenant/owner-scoped sharing table (#18, CC-1)

The persistence half of the permission/ACL model (spec 0004 §2.2): "Sharing is
**explicit**, via a ``grants`` table (``principal → resource → role``)". A grant
records that some principal (MVP: a ``user``) may access a resource (a
``collection`` or a ``document``) the requester does not own. The retrieval
permission filter (``app.retrieval.queries._permission_filter`` + the inline owner
predicates) widens from ``owner_id IN owners`` to "owner OR an explicit grant", so
a granted document/collection becomes retrievable **and** citable by the grantee
(INV-2). A grant on a collection **cascades** to its documents (the filter joins on
``documents.collection_id``); deny-by-default is preserved — absence of ownership
AND grant excludes the row.

Shape (spec 0004 §2.2 / §4 — refined for the MVP):

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed — every
  grant is tenant-scoped, INV-1; cross-tenant grants are impossible because the
  filter binds the requester's tenant and the grant's tenant to the same value);
* ``resource_type`` ∈ {``collection``, ``document``} + ``resource_id`` (the
  granted resource, by id — not an FK so a single column pair serves both
  resource kinds; the grant service validates the resource exists and is owned);
* ``principal_type`` (modelled to extend to ``group``/``role`` later, MVP only
  emits ``user``) + ``principal_id`` (the grantee — for ``user`` this is a
  ``users.id``);
* ``role`` (the access level the grant confers; MVP: ``viewer``);
* ``granted_by`` FK → ``users`` (who created the grant — the resource owner or a
  tenant admin) and ``created_at``.

Constraints/indexes:

* a UNIQUE on ``(tenant_id, resource_type, resource_id, principal_type,
  principal_id)`` so a (resource, principal) pair has at most one grant
  (re-granting is idempotent / an upsert target, never a duplicate);
* an index on ``(tenant_id, principal_id)`` — the retrieval filter's hot path
  ("which resources is *this requester* granted?");
* an index on ``(tenant_id, resource_type, resource_id)`` — the grant service's
  "list/revoke grants on this resource" path.

RLS: ``grants`` is tenant-scoped, so this migration also ``ENABLE`` + ``FORCE``
row-level security on it with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1,
#17) — additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#18 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (#70 lesson).

Revision ID: 0008_grants
Revises: 0007_tenancy_rls
Create Date: 2026-06-24 01:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_grants"
down_revision: str | None = "0007_tenancy_rls"
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


def upgrade() -> None:
    op.create_table(
        "grants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The granted resource: a (type, id) pair so one column pair serves both
        # collection and document grants. Not an FK (the kind varies); the grant
        # service validates the resource exists + is owned before inserting.
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.Uuid(as_uuid=True), nullable=False),
        # The grantee principal. ``principal_type`` is modelled wide (user|group|
        # role) though the MVP only emits ``user``; ``principal_id`` is the
        # grantee's id (a ``users.id`` for the ``user`` type).
        sa.Column("principal_type", sa.String(length=20), nullable=False),
        sa.Column("principal_id", sa.Uuid(as_uuid=True), nullable=False),
        # The access level the grant confers (MVP: ``viewer``).
        sa.Column("role", sa.String(length=20), nullable=False),
        # Who created the grant (the resource owner or a tenant admin). SET NULL
        # so removing a granting user does not cascade-delete the grants they made.
        sa.Column(
            "granted_by",
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
        # At most one grant per (resource, principal) within a tenant — re-granting
        # is idempotent, never duplicated.
        sa.UniqueConstraint(
            "tenant_id",
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            name="uq_grants_resource_principal",
        ),
        sa.CheckConstraint(
            "resource_type in ('collection', 'document')",
            name="ck_grants_resource_type",
        ),
        sa.CheckConstraint(
            "principal_type in ('user', 'group', 'role')",
            name="ck_grants_principal_type",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_grants_tenant_id", "grants", ["tenant_id"])
    # The retrieval filter's hot path: "which resources is this requester granted?"
    op.create_index("ix_grants_tenant_principal", "grants", ["tenant_id", "principal_id"])
    # The grant service's "list/revoke grants on this resource" path.
    op.create_index(
        "ix_grants_tenant_resource",
        "grants",
        ["tenant_id", "resource_type", "resource_id"],
    )

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE grants ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE grants FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_grants ON grants "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_grants ON grants")
        op.execute("ALTER TABLE grants NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE grants DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_grants_tenant_resource", table_name="grants")
    op.drop_index("ix_grants_tenant_principal", table_name="grants")
    op.drop_index("ix_grants_tenant_id", table_name="grants")
    op.drop_table("grants")

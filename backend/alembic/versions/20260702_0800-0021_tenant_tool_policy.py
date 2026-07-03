"""per-tenant tool governance policy — tenant_tool_policy (#223)

The persistence half of admin tool governance (issue #223): a tenant-scoped
``tenant_tool_policy`` row is an admin's override of one registered tool's
governance within one tenant — is it ``enabled`` for the tenant, and does an
invocation still ``requires_approval``. **Deny-by-default: the absence of a row
means the tool's built-in default applies** — a ``requires_approval`` tool (e.g.
``run_python``) stays denied until an admin writes a row that enables AND
pre-approves it (``enabled=true`` + ``requires_approval=false``). That row is the
switch the policy-driven ``PolicyApprovalGate`` consults at invoke time.
``down_revision`` is the current head ``0020_mcp_servers`` (single-head
invariant, ADR-0008 §4).

One new tenant-scoped table:

* ``tenant_tool_policy`` — the override record. Non-null ``tenant_id`` (INV-1);
  ``tool_name`` (validated against the registry in the service — no free-text
  tool can be stored); ``enabled`` / ``requires_approval`` booleans;
  ``updated_by`` FK → ``users`` (**SET NULL** so a deprovisioned admin does not
  cascade-delete the tenant's live policy); ``created_at`` / ``updated_at``. A
  unique ``(tenant_id, tool_name)`` constraint makes the override a per-tenant
  upsert (one row per tool per tenant); the constraint's implicit index also
  serves the tenant-leading lookup.

RLS: ``tenant_tool_policy`` is tenant-scoped, so this migration ``ENABLE`` +
``FORCE`` row-level security with the same fail-closed ``app.tenant_id`` GUC
policy the ``0007`` backstop uses for every other tenant-scoped table (spec 0004
§2.1) — additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#223 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (the #70
lesson).

Revision ID: 0021_tenant_tool_policy
Revises: 0020_mcp_servers
Create Date: 2026-07-02 08:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_tenant_tool_policy"
down_revision: str | None = "0020_mcp_servers"
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

# The tenant-scoped table that gets the RLS backstop.
_SCOPED_TABLES: tuple[str, ...] = ("tenant_tool_policy",)


def upgrade() -> None:
    op.create_table(
        "tenant_tool_policy",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        # The admin who last set this override (INV-6 corroboration). SET NULL so a
        # deprovisioned admin does not cascade-delete the tenant's live policy.
        sa.Column(
            "updated_by",
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
        # One override per tool per tenant (a per-tenant upsert). The implicit
        # index also serves the tenant-leading policy lookup (INV-1).
        sa.UniqueConstraint(
            "tenant_id", "tool_name", name="uq_tenant_tool_policy_tenant_tool"
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_tenant_tool_policy_tenant_id", "tenant_tool_policy", ["tenant_id"])

    # --- RLS backstop, consistent with the 0007 per-table policy ------------
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_{table} ON {table} "
                f"USING ({_PREDICATE}) "
                f"WITH CHECK ({_PREDICATE})"
            )


def downgrade() -> None:
    # Reverse order. Drop RLS first, then the index, then the table.
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_tenant_tool_policy_tenant_id", table_name="tenant_tool_policy")
    op.drop_table("tenant_tool_policy")

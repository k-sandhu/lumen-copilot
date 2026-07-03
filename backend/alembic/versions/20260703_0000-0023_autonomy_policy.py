"""per-tenant assistant autonomy cap — tenant_autonomy_policy (#218)

The persistence half of admin autonomy governance (ADR-0011 §3, issue #218): a
tenant-scoped ``tenant_autonomy_policy`` row is an admin's ceiling on how far ANY
assistant in the tenant may effectively act — ``max_autonomy`` is the maximum
``AutonomyLevel`` an assistant's EFFECTIVE autonomy is ``min``'d to. **Absence of a
row means no ceiling** — an assistant runs at its own configured level (the
permissive default, mirroring how ``tenant_tool_policy`` / ``tenant_sandbox_policy``
fall back to their built-in defaults). Enforcement is two-sided: publishing an
assistant above the cap is rejected, and the run-time tool gate uses the clamped
level to decide whether a T1 side-effecting tool may execute. ``down_revision`` is
the current head ``0022_sandbox_policy`` (single-head invariant, ADR-0008 §4).

One new tenant-scoped table:

* ``tenant_autonomy_policy`` — the per-tenant cap record. Non-null ``tenant_id``
  (INV-1); ``max_autonomy`` string constrained by CHECK to the four enum values;
  ``updated_by`` FK → ``users`` (**SET NULL** so a deprovisioned admin does not
  cascade-delete the tenant's live cap); ``created_at`` / ``updated_at``. A unique
  ``(tenant_id)`` constraint makes the cap a per-tenant singleton (one row per
  tenant); the constraint's implicit index also serves the tenant-leading lookup.

RLS: ``tenant_autonomy_policy`` is tenant-scoped, so this migration ``ENABLE`` +
``FORCE`` row-level security with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1) —
additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#218 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (the #70
lesson).

Revision ID: 0023_autonomy_policy
Revises: 0022_sandbox_policy
Create Date: 2026-07-03 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_autonomy_policy"
down_revision: str | None = "0022_sandbox_policy"
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
_SCOPED_TABLES: tuple[str, ...] = ("tenant_autonomy_policy",)


def upgrade() -> None:
    op.create_table(
        "tenant_autonomy_policy",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The autonomy ceiling — one of the four AutonomyLevel enum values. Absence
        # of a row means no ceiling (the permissive default); a stored row narrows.
        sa.Column("max_autonomy", sa.String(length=20), nullable=False),
        # The admin who last set this cap (INV-6 corroboration). SET NULL so a
        # deprovisioned admin does not cascade-delete the tenant's live cap.
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
        # One cap per tenant (a per-tenant singleton / upsert). The implicit index
        # also serves the tenant-leading policy lookup (INV-1).
        sa.UniqueConstraint("tenant_id", name="uq_tenant_autonomy_policy_tenant"),
        # Only a real AutonomyLevel value can be stored — no free-text ceiling.
        sa.CheckConstraint(
            "max_autonomy in ('suggest', 'draft', 'act_with_approval', 'act_auto')",
            name="ck_tenant_autonomy_policy_max_autonomy",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index(
        "ix_tenant_autonomy_policy_tenant_id", "tenant_autonomy_policy", ["tenant_id"]
    )

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

    op.drop_index("ix_tenant_autonomy_policy_tenant_id", table_name="tenant_autonomy_policy")
    op.drop_table("tenant_autonomy_policy")

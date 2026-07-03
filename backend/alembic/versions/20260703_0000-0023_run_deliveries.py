"""scheduled-run delivery — run_deliveries in-app inbox (#238)

The persistence half of scheduled-run delivery (issue #238, ADR-0015 §6): a
``run_deliveries`` row is one in-app delivery of a completed run's output to the
owner's inbox — the T0/T1-safe default (external channels email/Slack are deferred
T2-ish egress, INV-7, out of scope here). ``down_revision`` is the current head
``0022_sandbox_policy`` (single-head invariant, ADR-0008 §4). The orchestrator may
renumber this on merge to keep a single linear head.

One new tenant/owner-scoped table:

* ``run_deliveries`` — the in-app delivery record. Non-null ``tenant_id`` (INV-1) +
  ``recipient_id`` (the owner the run ran *as*; SET NULL so a delivery survives
  deprovisioning, never cascade-deleted); the ``run_id`` FK (CASCADE — a delivery is
  meaningless without its run) links to the full cited transcript; the nullable
  ``schedule_id`` FK (SET NULL — a delivery outlives the schedule that fired it, and
  null for a manual run); a CHECK-pinned ``kind`` (``inbox``/``digest``) + a
  CHECK-pinned ``status`` (``pending``/``delivered``/``read``/``failed``); the inbox
  ``summary``; ``created_at`` / a nullable ``read_at`` (when the owner opened it).
  Three tenant-leading indexes back the owner inbox read, the pending-digest sweep,
  and the per-run lookup.

RLS: ``run_deliveries`` is tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security with the same fail-closed ``app.tenant_id`` GUC policy the
``0007`` backstop uses (spec 0004 §2.1) — additive, never relaxing an existing
policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the RLS policy, disables RLS,
and drops the table, restoring the pre-#238 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (the #70 lesson).

Revision ID: 0023_run_deliveries
Revises: 0022_sandbox_policy
Create Date: 2026-07-03 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_run_deliveries"
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


def upgrade() -> None:
    # --- run_deliveries (one in-app delivery of a completed run) -------------
    op.create_table(
        "run_deliveries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The owner the run ran as — the delivery recipient (INV-2/§2.2). SET NULL on
        # user delete so a delivery record survives deprovisioning.
        sa.Column(
            "recipient_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=False,
        ),
        # CASCADE: a delivery is meaningless without its run.
        sa.Column(
            "run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Null ⇒ a manual run's delivery. SET NULL so the delivery outlives its
        # schedule (like ``runs.schedule_id``).
        sa.Column(
            "schedule_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("schedules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        # Pin the enum domains at the DB so a bad value can never be stored.
        sa.CheckConstraint("kind in ('inbox', 'digest')", name="ck_run_deliveries_kind"),
        sa.CheckConstraint(
            "status in ('pending', 'delivered', 'read', 'failed')",
            name="ck_run_deliveries_status",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_run_deliveries_tenant_id", "run_deliveries", ["tenant_id"])
    op.create_index("ix_run_deliveries_recipient_id", "run_deliveries", ["recipient_id"])
    # The owner inbox read, the pending-digest sweep, and the per-run lookup — each
    # tenant-leading so the equality filter uses one index.
    op.create_index(
        "ix_run_deliveries_tenant_recipient", "run_deliveries", ["tenant_id", "recipient_id"]
    )
    op.create_index(
        "ix_run_deliveries_tenant_status", "run_deliveries", ["tenant_id", "status"]
    )
    op.create_index("ix_run_deliveries_run_id", "run_deliveries", ["run_id"])

    # --- RLS backstop, consistent with the 0007 per-table policy ------------
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE run_deliveries ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE run_deliveries FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_run_deliveries ON run_deliveries "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    # Reverse order. Drop RLS first, then the table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_run_deliveries ON run_deliveries")
        op.execute("ALTER TABLE run_deliveries NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE run_deliveries DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_run_deliveries_run_id", table_name="run_deliveries")
    op.drop_index("ix_run_deliveries_tenant_status", table_name="run_deliveries")
    op.drop_index("ix_run_deliveries_tenant_recipient", table_name="run_deliveries")
    op.drop_index("ix_run_deliveries_recipient_id", table_name="run_deliveries")
    op.drop_index("ix_run_deliveries_tenant_id", table_name="run_deliveries")
    op.drop_table("run_deliveries")

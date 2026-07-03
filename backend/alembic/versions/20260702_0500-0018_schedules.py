"""dynamic per-tenant scheduler — schedules table + runs.schedule_id FK (#236)

The scheduler half of the scheduled-runs capability (ADR-0015 §1/§2): a
``schedules`` row is a user-defined recurring run of a saved assistant, read by the
dynamic Beat (celery-redbeat) to fire at ``next_run_at``. ``down_revision`` is the
current head ``0017_runs`` (single-head invariant, ADR-0008 §4).

One new tenant/owner-scoped table + the #235 residual FK:

* ``schedules`` — the recurring-run definition. Non-null ``tenant_id`` (INV-1) +
  ``owner_id`` (the run's principal — retrieval runs *as* them, INV-2, SET NULL so a
  schedule survives deprovisioning); the ``assistant_id`` FK (CASCADE — a schedule
  is meaningless without its assistant); the normalized ``cadence_cron`` + the
  optional original ``cadence_structured`` jsonb; the IANA ``timezone`` (drives the
  DST-correct ``next_run_at``); ``input_params`` + ``delivery`` jsonb; a
  CHECK-pinned ``overlap_policy``; ``enabled``; ``next_run_at`` / ``last_run_at`` /
  a CHECK-pinned nullable ``last_status``. Three tenant-leading indexes back the
  owner/assistant/enabled reads (the list view + the Beat reconcile sweep).
* ``runs.schedule_id`` — the #235 residual: #235 shipped ``schedule_id`` as a plain
  nullable uuid so it did not depend on this table; now that ``schedules`` exists,
  this migration adds the real FK → ``schedules.id`` ``ON DELETE SET NULL`` (a run's
  history outlives the schedule that fired it).

RLS: ``schedules`` is tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security with the same fail-closed ``app.tenant_id`` GUC policy the
``0007`` backstop uses (spec 0004 §2.1) — additive, never relaxing an existing
policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the ``runs.schedule_id`` FK,
drops the RLS policy, disables RLS, and drops the table, restoring the pre-#236
state. Offline DDL render asserts the shape; the live apply runs against a
disposable throwaway database (the #70 lesson).

Revision ID: 0018_schedules
Revises: 0017_runs
Create Date: 2026-07-02 05:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0018_schedules"
down_revision: str | None = "0017_runs"
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

# JSON-typed columns: JSONB on Postgres (indexable), generic JSON on SQLite (the
# offline test engine). Mirrors ``app.db.models._JSON``.
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")  # type: ignore[no-untyped-call]

# The FK constraint name for the #235 residual runs.schedule_id → schedules.id.
_RUNS_SCHEDULE_FK = "fk_runs_schedule_id"


def upgrade() -> None:
    # --- schedules (a user-defined recurring run) ---------------------------
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The run's principal (INV-2). SET NULL on user delete so a schedule survives
        # deprovisioning (admin reassignment) rather than cascade-deleting.
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=False,
        ),
        # CASCADE: a schedule is meaningless without its assistant.
        sa.Column(
            "assistant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("assistants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The normalized canonical 5-field cron string + the optional original
        # structured cadence (null for a raw-cron schedule).
        sa.Column("cadence_cron", sa.String(length=100), nullable=False),
        sa.Column("cadence_structured", _JSON, nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("input_params", _JSON, nullable=False),
        sa.Column("delivery", _JSON, nullable=False),
        sa.Column("overlap_policy", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=20), nullable=True),
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
        # Pin the enum domains at the DB so a bad value can never be stored.
        sa.CheckConstraint(
            "overlap_policy in ('skip', 'queue', 'allow')",
            name="ck_schedules_overlap_policy",
        ),
        sa.CheckConstraint(
            "last_status is null or "
            "last_status in ('queued', 'running', 'succeeded', 'failed', 'escalated')",
            name="ck_schedules_last_status",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_schedules_tenant_id", "schedules", ["tenant_id"])
    op.create_index("ix_schedules_owner_id", "schedules", ["owner_id"])
    # The list-view + Beat reconcile reads (owner inbox, by assistant, enabled sweep).
    op.create_index("ix_schedules_tenant_owner", "schedules", ["tenant_id", "owner_id"])
    op.create_index("ix_schedules_tenant_assistant", "schedules", ["tenant_id", "assistant_id"])
    op.create_index("ix_schedules_tenant_enabled", "schedules", ["tenant_id", "enabled"])

    # --- runs.schedule_id → schedules.id FK (the #235 residual) -------------
    # #235 shipped ``schedule_id`` as a plain nullable uuid so it did not depend on
    # this table; now the FK lands, SET NULL so a deleted schedule leaves its runs.
    op.create_foreign_key(
        _RUNS_SCHEDULE_FK,
        "runs",
        "schedules",
        ["schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- RLS backstop, consistent with the 0007 per-table policy ------------
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE schedules ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE schedules FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_schedules ON schedules "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    # Reverse order. Drop RLS first, then the FK, then the table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_schedules ON schedules")
        op.execute("ALTER TABLE schedules NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE schedules DISABLE ROW LEVEL SECURITY")

    op.drop_constraint(_RUNS_SCHEDULE_FK, "runs", type_="foreignkey")

    op.drop_index("ix_schedules_tenant_enabled", table_name="schedules")
    op.drop_index("ix_schedules_tenant_assistant", table_name="schedules")
    op.drop_index("ix_schedules_tenant_owner", table_name="schedules")
    op.drop_index("ix_schedules_owner_id", table_name="schedules")
    op.drop_index("ix_schedules_tenant_id", table_name="schedules")
    op.drop_table("schedules")

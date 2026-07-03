"""headless agent-run records — runs + run_steps transcript (#235)

The persistence half of the headless-run runtime (issue #235, ADR-0015 §2): a
``runs`` row is one execution of an assistant — scheduled or manual — that runs
with **no WebSocket client present**, and ``run_steps`` is the durable, ordered
transcript (the analogue of the WS envelope stream). ``down_revision`` is the
current head ``0016_assistants`` (single-head invariant, ADR-0008 §4).

Two new tenant/owner-scoped tables:

* ``runs`` — the run record. Non-null ``tenant_id`` (INV-1) + ``owner_id`` (the
  run's principal — retrieval runs *as* them, INV-2); the ``assistant_id`` (CASCADE)
  + the pinned ``assistant_version_id`` (SET NULL, reproducible after edits); a
  nullable ``schedule_id`` (the FK lands with the scheduler #236's ``schedules``
  table — a plain nullable uuid here so #235 ships independently); a nullable
  ``session_id`` (SET NULL — the internal chat session the transcript's message/
  citations hang off, reusing the shared chain); a nullable ``message_id`` (SET
  NULL — the assistant message the run produced); ``trigger`` + ``status`` enums
  (CHECK-pinned domains); ``inputs`` jsonb (the snapshotted params); ``summary``;
  ``error`` jsonb ({code, message} — never a raw vendor string); ``started_at`` /
  ``finished_at``. Four tenant-leading indexes back the ``/runs`` filters (owner,
  assistant, schedule, status). Both enum domains are pinned at the DB.
* ``run_steps`` — the append-only transcript. Non-null ``tenant_id`` (INV-1) +
  ``run_id`` FK (CASCADE), a monotonic ``seq`` (UNIQUE within a run — same
  ordering guarantee as the WS ``seq``), a CHECK-pinned ``kind``, and the envelope
  ``payload`` jsonb.

RLS: both new tables are tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security on each with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses (spec 0004 §2.1) — additive, never relaxing an existing
policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policies, disables RLS,
and drops the two tables in FK order (children → parents), restoring the pre-#235
state. Offline DDL render asserts the shape; the live apply runs against a
disposable throwaway database (the #70 lesson).

Revision ID: 0017_runs
Revises: 0016_assistants
Create Date: 2026-07-02 04:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0017_runs"
down_revision: str | None = "0016_assistants"
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

# The two new tenant-scoped tables that get the RLS backstop.
_SCOPED_TABLES: tuple[str, ...] = ("runs", "run_steps")


def upgrade() -> None:
    # --- runs (one execution of an assistant) -------------------------------
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The run's principal (INV-2). SET NULL on user delete so a completed run
        # record survives deprovisioning rather than cascade-deleting.
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=False,
        ),
        sa.Column(
            "assistant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("assistants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The pinned version executed — reproducible after edits. SET NULL.
        sa.Column(
            "assistant_version_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("assistant_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Null ⇒ a manual/run-now run. The FK lands with the scheduler (#236 owns
        # the ``schedules`` table); a plain nullable uuid here so #235 is standalone.
        sa.Column("schedule_id", sa.Uuid(as_uuid=True), nullable=True),
        # The internal chat session the transcript's message/citations hang off.
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("inputs", _JSON, nullable=False),
        # The assistant message the transcript produced (the shared messages chain).
        sa.Column(
            "message_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        # Typed failure/escalation reason {code, message} — never a raw vendor string.
        sa.Column("error", _JSON, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Pin the enum domains at the DB so a bad value can never be stored.
        sa.CheckConstraint("trigger in ('schedule', 'manual')", name="ck_runs_trigger"),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed', 'escalated')",
            name="ck_runs_status",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_runs_tenant_id", "runs", ["tenant_id"])
    op.create_index("ix_runs_owner_id", "runs", ["owner_id"])
    # The four ``/runs`` filter paths (owner inbox, by assistant, by schedule,
    # by status) — each tenant-leading so the equality filter uses one index.
    op.create_index("ix_runs_tenant_owner", "runs", ["tenant_id", "owner_id"])
    op.create_index("ix_runs_tenant_assistant", "runs", ["tenant_id", "assistant_id"])
    op.create_index("ix_runs_tenant_schedule", "runs", ["tenant_id", "schedule_id"])
    op.create_index("ix_runs_tenant_status", "runs", ["tenant_id", "status"])

    # --- run_steps (the append-only transcript) -----------------------------
    op.create_table(
        "run_steps",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Monotonic per run; UNIQUE within a run (same ordering as the WS seq).
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq"),
        sa.CheckConstraint("seq >= 0", name="ck_run_steps_seq_nonneg"),
        sa.CheckConstraint(
            "kind in ('delta', 'tool_call', 'tool_result', 'citation', 'error')",
            name="ck_run_steps_kind",
        ),
    )
    op.create_index("ix_run_steps_tenant_id", "run_steps", ["tenant_id"])
    op.create_index("ix_run_steps_run_id", "run_steps", ["run_id"])
    op.create_index("ix_run_steps_tenant_run", "run_steps", ["tenant_id", "run_id"])

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
    # Reverse order. Drop RLS first, then the tables (children → parents).
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_run_steps_tenant_run", table_name="run_steps")
    op.drop_index("ix_run_steps_run_id", table_name="run_steps")
    op.drop_index("ix_run_steps_tenant_id", table_name="run_steps")
    op.drop_table("run_steps")

    op.drop_index("ix_runs_tenant_status", table_name="runs")
    op.drop_index("ix_runs_tenant_schedule", table_name="runs")
    op.drop_index("ix_runs_tenant_assistant", table_name="runs")
    op.drop_index("ix_runs_tenant_owner", table_name="runs")
    op.drop_index("ix_runs_owner_id", table_name="runs")
    op.drop_index("ix_runs_tenant_id", table_name="runs")
    op.drop_table("runs")

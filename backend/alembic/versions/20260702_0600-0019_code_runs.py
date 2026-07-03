"""sandbox code-run records — code_runs (#230, ADR-0013 §4)

The persistence half of the isolated Python code-execution sandbox (issue #230,
ADR-0013 §4): a ``code_runs`` row is one execution of agent-authored, adversarial-
by-assumption Python in the ephemeral sandbox container — captured stdout/stderr,
exit code, timing, measured resource usage, the pinned image digest, and the ids of
any output files collected as artifacts. ``down_revision`` is the current head
``0018_schedules`` (single-head invariant, ADR-0008 §4).

One new tenant/owner-scoped table:

* ``code_runs`` — the run record. Non-null ``tenant_id`` (INV-1) + ``owner_id`` (the
  run's principal — the sandbox runs code on their behalf, SET NULL so a completed
  run survives deprovisioning); a nullable ``session_id`` (SET NULL — the chat session
  the run fired inside, the only real link FK); nullable ``run_id`` / ``trace_id``
  (plain uuids — their referent tables are the run/agent-trace framework, FK-less
  until they land, mirroring ``artifacts``); a CHECK-pinned ``status`` enum
  (queued|running|succeeded|failed|timeout|killed|denied); the exact ``code``;
  output-size-capped ``stdout``/``stderr``; ``exit_code`` (nonneg CHECK) +
  ``duration_ms`` (nonneg CHECK); ``resource_usage`` jsonb; ``image_digest``;
  ``artifact_ids`` (a portable string array, default empty); ``started_at`` /
  ``finished_at`` / ``created_at``. Three tenant-leading indexes back the owner
  history, status filter, and session link.

RLS: ``code_runs`` is tenant-scoped, so this migration ``ENABLE`` + ``FORCE`` row-
level security with the same fail-closed ``app.tenant_id`` GUC policy the ``0007``
backstop uses (spec 0004 §2.1) — additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, drops
the indexes, and drops the table, restoring the pre-#230 state. Offline DDL render
asserts the shape; the live apply runs against a disposable throwaway database (the
#70 lesson).

Revision ID: 0019_code_runs
Revises: 0018_schedules
Create Date: 2026-07-02 06:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.db.types import StringArray

# revision identifiers, used by Alembic.
revision: str = "0019_code_runs"
down_revision: str | None = "0018_schedules"
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

# The tenant-scoped table that gets the RLS backstop.
_SCOPED_TABLES: tuple[str, ...] = ("code_runs",)


def upgrade() -> None:
    op.create_table(
        "code_runs",
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
        # The chat session the run fired inside — the only real link FK (SET NULL).
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # The parent agent run / trace — plain nullable uuids until those tables land.
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("trace_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        # Captured, output-size-capped (G7); default empty (never null).
        sa.Column("stdout", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr", sa.Text(), nullable=False, server_default=""),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("resource_usage", _JSON, nullable=True),
        sa.Column("image_digest", sa.String(length=255), nullable=True),
        # Ids of artifacts the run emitted (a portable string array). Native TEXT[]
        # on Postgres / JSON-encoded TEXT on SQLite per ``app.db.types.StringArray``
        # (the same type the model uses, so autogenerate sees no drift). No server
        # default — the repository always writes an explicit empty list at create
        # (mirroring ``users.roles``), so a row never carries a NULL array.
        sa.Column("artifact_ids", StringArray(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Pin the status enum domain at the DB so a bad value can never be stored.
        sa.CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed', 'timeout', "
            "'killed', 'denied')",
            name="ck_code_runs_status",
        ),
        sa.CheckConstraint(
            "exit_code is null or exit_code >= 0", name="ck_code_runs_exit_code_nonneg"
        ),
        sa.CheckConstraint(
            "duration_ms is null or duration_ms >= 0", name="ck_code_runs_duration_nonneg"
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_code_runs_tenant_id", "code_runs", ["tenant_id"])
    op.create_index("ix_code_runs_owner_id", "code_runs", ["owner_id"])
    # The owner history + status filter + session link (each tenant-leading so the
    # equality filter uses one index).
    op.create_index("ix_code_runs_tenant_owner", "code_runs", ["tenant_id", "owner_id"])
    op.create_index("ix_code_runs_tenant_status", "code_runs", ["tenant_id", "status"])
    op.create_index("ix_code_runs_session_id", "code_runs", ["session_id"])

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
    # Reverse order. Drop RLS first, then the indexes, then the table.
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_code_runs_session_id", table_name="code_runs")
    op.drop_index("ix_code_runs_tenant_status", table_name="code_runs")
    op.drop_index("ix_code_runs_tenant_owner", table_name="code_runs")
    op.drop_index("ix_code_runs_owner_id", table_name="code_runs")
    op.drop_index("ix_code_runs_tenant_id", table_name="code_runs")
    op.drop_table("code_runs")

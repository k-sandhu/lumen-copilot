"""governed tool platform — tool_invocations trace table (CC-7 #207)

The persistence half of the governed tool registry (issue #207 §4): a durable
record **per tool invocation** so the run trace (WS trace now, AgentOps later,
E14) and the audit correlation never have a silent gap. One row is written for a
success, an off-allow-list/unapproved **denial**, and a tool **failure** alike
(``ok`` + ``error``).

This is a **trace/analytics** table, distinct from the append-only ``audit_events``
log (spec 0004 §2.4): the ``tool.invoked``/``tool.result`` audit events still go
to that sink; this table carries the structured per-call record. So — unlike
``audit_events`` — the app role keeps normal write access here (no UPDATE/DELETE
revoke); it is an ordinary tenant-scoped table.

Shape (issue #207 §4):

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed — INV-1);
* ``session_id`` FK → ``chat_sessions`` (nullable, ``ON DELETE SET NULL`` so a
  deleted session keeps its trace) and ``message_id`` FK → ``messages`` (nullable,
  same) — the run + turn the call belongs to;
* ``run_id`` (nullable uuid, no FK) — reserved for a future multi-step agent-run
  grouping (E14), MVP unused;
* ``tool_name``; ``args_hash`` (a stable, non-reversible hash of the args — never
  the raw args, mirroring the audit query-hash discipline, spec 0004 §2.4);
* ``ok`` (bool) + ``error`` (the ``ERROR_*`` code when not ok; null on success);
* ``duration_ms`` (>= 0, checked) + ``created_at``.

Indexes: ``(tenant_id, session_id)`` (the per-chat trace read) and
``(tenant_id, tool_name)`` (per-tool analytics), each tenant-leading (INV-1).

RLS: tenant-scoped, so this migration ``ENABLE`` + ``FORCE`` row-level security
with the same fail-closed ``app.tenant_id`` GUC policy the ``0007`` backstop uses
for every other tenant-scoped table (spec 0004 §2.1, #17) — additive, never
relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#207 state. Offline DDL render asserts the
shape; a live apply runs against a disposable throwaway database (#70 lesson).

Revision ID: 0013_tool_invocations
Revises: 0012_tenant_max_tool_turns
Create Date: 2026-07-02 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_tool_invocations"
down_revision: str | None = "0012_tenant_max_tool_turns"
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
        "tool_invocations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The run + turn the call belongs to — nullable, SET NULL so deleting a
        # session/message keeps the trace rows (the trace outlives its subjects).
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Reserved for a future multi-step agent-run grouping (E14); no FK yet.
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        # A stable non-reversible hash of the args (never the raw args — §2.4).
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        # The ERROR_* code when ok is False (denial or failure); null on success.
        sa.Column("error", sa.String(length=50), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_tool_invocations_duration_nonneg"),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_tool_invocations_tenant_id", "tool_invocations", ["tenant_id"])
    # The per-chat trace read: "the tool calls in this session".
    op.create_index(
        "ix_tool_invocations_tenant_session",
        "tool_invocations",
        ["tenant_id", "session_id"],
    )
    # Per-tool analytics: "how often / how did this tool run" (E14).
    op.create_index(
        "ix_tool_invocations_tenant_tool",
        "tool_invocations",
        ["tenant_id", "tool_name"],
    )

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE tool_invocations ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE tool_invocations FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_tool_invocations ON tool_invocations "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_tool_invocations ON tool_invocations")
        op.execute("ALTER TABLE tool_invocations NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE tool_invocations DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_tool_invocations_tenant_tool", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_tenant_session", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_tenant_id", table_name="tool_invocations")
    op.drop_table("tool_invocations")

"""reusable root-capable sandbox sessions (ADR-0020, issue #457)

Revision ID: 0038_sandbox_sessions
Revises: 0037_session_summaries
Create Date: 2026-07-18 00:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import StringArray

revision: str = "0038_sandbox_sessions"
down_revision: str | None = "0037_session_summaries"
branch_labels: str | None = None
depends_on: str | None = None

_GUC = "app.tenant_id"
_BYPASS = "bypass"
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        "sandbox_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("image_digest", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "chat_session_id", name="uq_sandbox_sessions_tenant_chat"
        ),
        sa.CheckConstraint(
            "status in ('active', 'closed', 'error')",
            name="ck_sandbox_sessions_status",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_sandbox_sessions_generation"),
    )
    op.create_index("ix_sandbox_sessions_tenant_id", "sandbox_sessions", ["tenant_id"])
    op.create_index("ix_sandbox_sessions_owner_id", "sandbox_sessions", ["owner_id"])
    op.create_index(
        "ix_sandbox_sessions_tenant_owner",
        "sandbox_sessions",
        ["tenant_id", "owner_id"],
    )

    op.add_column(
        "code_runs",
        sa.Column(
            "requested_packages",
            StringArray(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Historical run identity deliberately remains after chat/sandbox deletion, like
    # code_runs.trace_id. It is tenant-scoped but not an FK to the ephemeral row.
    op.add_column(
        "code_runs", sa.Column("sandbox_session_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "code_runs", sa.Column("sandbox_generation", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "ck_code_runs_sandbox_generation",
        "code_runs",
        "sandbox_generation is null or sandbox_generation >= 1",
    )
    op.create_index(
        "ix_code_runs_sandbox_session_id", "code_runs", ["sandbox_session_id"]
    )

    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE sandbox_sessions ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE sandbox_sessions FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY rls_sandbox_sessions ON sandbox_sessions "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_sandbox_sessions ON sandbox_sessions")
        op.execute("ALTER TABLE sandbox_sessions NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE sandbox_sessions DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_code_runs_sandbox_session_id", table_name="code_runs")
    op.drop_constraint("ck_code_runs_sandbox_generation", "code_runs", type_="check")
    op.drop_column("code_runs", "sandbox_generation")
    op.drop_column("code_runs", "sandbox_session_id")
    op.drop_column("code_runs", "requested_packages")
    op.drop_index("ix_sandbox_sessions_tenant_owner", table_name="sandbox_sessions")
    op.drop_index("ix_sandbox_sessions_owner_id", table_name="sandbox_sessions")
    op.drop_index("ix_sandbox_sessions_tenant_id", table_name="sandbox_sessions")
    op.drop_table("sandbox_sessions")

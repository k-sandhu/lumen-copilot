"""Conversation evidence handles and inline-selected citations (#436)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_evidence_handles"
down_revision = "0045_embedding_contract"
branch_labels = None
depends_on = None

_PREDICATE = (
    "current_setting('app.tenant_id', true) = 'bypass' "
    "OR tenant_id = current_setting('app.tenant_id', true)::uuid"
)


def upgrade() -> None:
    op.add_column(
        "chat_sessions", sa.Column("handle_next", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("citations", sa.Column("handle", sa.String(32), nullable=True))
    op.create_table(
        "source_handles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("handle", sa.String(32), nullable=False),
        sa.Column("evidence", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.UniqueConstraint("tenant_id", "session_id", "handle", name="uq_source_handles_identity"),
    )
    op.create_table(
        "web_citations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("handle", sa.String(32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
    )
    op.create_index("ix_web_citations_message_id", "web_citations", ["message_id"])
    for table in ("source_handles", "web_citations"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        if op.get_context().dialect.name == "postgresql":
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_{table} ON {table} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
            )


def downgrade() -> None:
    op.drop_table("web_citations")
    op.drop_table("source_handles")
    op.drop_column("citations", "handle")
    op.drop_column("chat_sessions", "handle_next")

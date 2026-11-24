"""Durable assistant-bound marker on chat sessions (#581).

``chat_sessions.assistant_id`` / ``assistant_version_id`` are
``ON DELETE SET NULL``: deleting an assistant erased the only evidence that a
session was governed by one, and the send path silently continued as
unrestricted ad-hoc chat. The non-FK ``assistant_bound`` flag survives that
deletion so the send path can fail closed. Backfilled from any live pin;
sessions whose assistant was already deleted before this migration cannot be
recovered (their FKs are already NULL) and remain ad-hoc.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_assistant_bound"
down_revision = "0049_discovery_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "assistant_bound",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    chat_sessions = sa.table(
        "chat_sessions",
        sa.column("assistant_bound", sa.Boolean()),
        sa.column("assistant_id"),
        sa.column("assistant_version_id"),
    )
    # Like migration 0042: the table owner must see every tenant during backfill.
    # Transactional DDL restores FORCE automatically if the migration rolls back.
    is_postgres = op.get_bind().dialect.name == "postgresql"
    if is_postgres:
        op.execute("ALTER TABLE chat_sessions NO FORCE ROW LEVEL SECURITY")
    op.execute(
        chat_sessions.update()
        .where(
            sa.or_(
                chat_sessions.c.assistant_id.is_not(None),
                chat_sessions.c.assistant_version_id.is_not(None),
            )
        )
        .values(assistant_bound=True)
    )
    if is_postgres:
        op.execute("ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_column("chat_sessions", "assistant_bound")
